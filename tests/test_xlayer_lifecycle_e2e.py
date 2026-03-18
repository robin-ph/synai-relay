"""X Layer full lifecycle E2E — real on-chain settlement & refund through server code paths.

Skipped by default. Run with:
    RUN_E2E_XLAYER=1 pytest tests/test_xlayer_lifecycle_e2e.py -v -s

Flow 1 — Settlement (skip oracle, direct payout):
    Buyer deposits USDG on-chain → funded job → worker claims → submits →
    skip oracle → resolve → adapter.payout() → verify worker balance

Flow 2 — Expiry refund:
    Buyer deposits USDG on-chain → funded job with past expiry →
    _auto_refund() → verify buyer balance

Uses real XLayerAdapter (RPC fallback, no OnchainOS needed).
"""
import os
import time
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch

from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account

pytestmark = pytest.mark.skipif(
    not os.environ.get('RUN_E2E_XLAYER'),
    reason="X Layer lifecycle E2E: set RUN_E2E_XLAYER=1 to run"
)

# --- On-chain constants ---
XLAYER_RPC = os.environ.get('XLAYER_RPC_URL', 'https://rpc.xlayer.tech')
USDC_CONTRACT = '0x4ae46a509f6b1d9056937ba4500cb143933d2dc8'
CHAIN_ID = 196
FEE_BPS = 2000

# Buyer (arc_solver) — sends deposits
BUYER_KEY = os.environ.get('TEST_BUYER_WALLET_KEY', '')
BUYER_ADDR = '0xf808390B22F56a47ddEE15053Eb10A9674aDe0F4'

# Worker (byte.runner) — receives payouts
WORKER_KEY = os.environ.get('TEST_WORKER_WALLET_KEY', '')
WORKER_ADDR = '0xbAE26E65D1C1246D7B7f2574980C1d93C31Eae6F'

# OPS wallet (from .env)
OPS_KEY = os.environ.get('OPERATIONS_WALLET_KEY', '')
OPS_ADDR = os.environ.get('OPERATIONS_WALLET_ADDRESS', '')

# ERC-20 ABI
_ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


# --- Helpers ---

def _w3():
    return Web3(Web3.HTTPProvider(XLAYER_RPC))


def _usdc_contract(w3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(USDC_CONTRACT), abi=_ERC20_ABI
    )


def _usdc_balance(w3, addr: str) -> Decimal:
    usdc = _usdc_contract(w3)
    raw = usdc.functions.balanceOf(Web3.to_checksum_address(addr)).call()
    return Decimal(raw) / Decimal(10 ** 6)


def _send_usdc_onchain(w3, from_key: str, to: str, amount: Decimal) -> str:
    """Send USDG via direct ERC-20 transfer on X Layer. Returns tx hash.
    Uses 'pending' nonce to handle back-to-back sends without waiting."""
    usdc = _usdc_contract(w3)
    account = Account.from_key(from_key)
    amount_atomic = int(amount * 10 ** 6)
    nonce = w3.eth.get_transaction_count(account.address, 'pending')
    tx = usdc.functions.transfer(
        Web3.to_checksum_address(to), amount_atomic
    ).build_transaction({
        'from': account.address,
        'gas': 100_000,
        'gasPrice': w3.eth.gas_price,
        'nonce': nonce,
        'chainId': CHAIN_ID,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    assert receipt.status == 1, f"USDG transfer failed on-chain: {tx_hash.hex()}"
    return '0x' + tx_hash.hex()


def _wait_balance(w3, addr: str, min_bal: Decimal, timeout: int = 15) -> Decimal:
    """Poll until balance >= min_bal."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        bal = _usdc_balance(w3, addr)
        if bal >= min_bal:
            return bal
        time.sleep(2)
    return _usdc_balance(w3, addr)


# --- Flask app fixture ---

@pytest.fixture(scope='module')
def setup():
    """Set up Flask app with real XLayerAdapter, return (client, adapter, app)."""
    # Force in-memory DB
    os.environ['DATABASE_URL'] = 'sqlite://'

    import server as _srv
    from server import app, _init_x402
    from models import db
    from services.xlayer_adapter import XLayerAdapter

    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['X402_ENABLED'] = False  # bypass x402 for job creation

    # Create real adapter with a sentinel client (not None) so is_connected() works.
    # The adapter only uses the client for verify_deposit and broadcast (which falls
    # back to RPC anyway), so a simple object is sufficient.
    class _StubClient:
        """Non-None stub so adapter.is_connected() returns True."""
        pass

    adapter = XLayerAdapter(
        onchainos_client=_StubClient(),
        ops_private_key=OPS_KEY,
        rpc_url=XLAYER_RPC,
        usdc_addr=USDC_CONTRACT,
    )

    with app.app_context():
        db.create_all()

        # Ensure chain registry is initialized (normally happens on first request)
        _init_x402()
        _srv._x402_initialized = True

        # Register our real XLayerAdapter in the chain registry
        _srv._chain_registry.register(adapter)

        yield {
            'client': app.test_client(),
            'adapter': adapter,
            'app': app,
            'db': db,
        }

        # Teardown — stop background threads
        from server import _shutdown_event, _oracle_executor
        _shutdown_event.set()
        try:
            _oracle_executor.shutdown(wait=True)
        except Exception:
            pass


def _register_agent(client, agent_id, name, wallet_addr):
    """Register an agent and return (agent_id, api_key)."""
    resp = client.post('/agents', json={
        'agent_id': agent_id,
        'name': name,
        'wallet_address': wallet_addr,
    })
    data = resp.get_json()
    assert resp.status_code == 201, f"Agent registration failed: {data}"
    return data['agent_id'], data['api_key']


def _auth(api_key):
    return {'Authorization': f'Bearer {api_key}'}


# =============================================================================
# Flow 1: Settlement — deposit → funded job → claim → submit → payout
# =============================================================================

class TestSettlementFlow:
    """Full settlement flow with real on-chain USDG payout."""

    TASK_PRICE = Decimal('0.10')

    def test_01_register_agents(self, setup):
        """Register buyer and worker agents."""
        client = setup['client']

        buyer_id, buyer_key = _register_agent(client, 'e2e-buyer', 'E2E Buyer', BUYER_ADDR)
        worker_id, worker_key = _register_agent(client, 'e2e-worker', 'E2E Worker', WORKER_ADDR)

        setup['buyer_id'] = buyer_id
        setup['buyer_api_key'] = buyer_key
        setup['worker_id'] = worker_id
        setup['worker_api_key'] = worker_key

        print(f"\n  Buyer:  {buyer_id}")
        print(f"  Worker: {worker_id}")

    def test_02_deposit_and_create_funded_job(self, setup):
        """Buyer deposits USDG on-chain, then create funded job via API + DB update."""
        client = setup['client']
        w3 = _w3()

        # Step 1: Buyer sends USDG to operator on-chain
        print(f"\n  Depositing {self.TASK_PRICE} USDG on X Layer...")
        deposit_tx = _send_usdc_onchain(w3, BUYER_KEY, OPS_ADDR, self.TASK_PRICE)
        print(f"  Deposit tx: {deposit_tx}")

        # Step 2: Create job via API (x402 disabled → status='open')
        resp = client.post('/jobs', json={
            'title': 'E2E Settlement Test',
            'description': 'Write a haiku about blockchain',
            'rubric': 'Must be exactly 3 lines with 5-7-5 syllable pattern',
            'price': float(self.TASK_PRICE),
            'expiry': int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }, headers=_auth(setup['buyer_api_key']))

        data = resp.get_json()
        assert resp.status_code == 201, f"Job creation failed: {data}"
        task_id = data['task_id']
        setup['task_id'] = task_id
        print(f"  Job created: {task_id}")

        # Step 3: Manually fund the job (simulates x402 settlement)
        from models import Job, db
        job = db.session.get(Job, task_id)
        job.status = 'funded'
        job.deposit_tx_hash = deposit_tx
        job.depositor_address = BUYER_ADDR
        job.deposit_amount = self.TASK_PRICE
        job.chain_id = CHAIN_ID
        db.session.commit()
        print(f"  Job funded: status={job.status}, chain_id={job.chain_id}")

    def test_03_worker_claims(self, setup):
        """Worker claims the funded job."""
        client = setup['client']
        task_id = setup['task_id']

        resp = client.post(f'/jobs/{task_id}/claim', json={
            'worker_id': setup['worker_id'],
        }, headers=_auth(setup['worker_api_key']))

        data = resp.get_json()
        assert resp.status_code == 200, f"Claim failed: {data}"
        print(f"\n  Worker claimed job {task_id}")

    def test_04_worker_submits(self, setup):
        """Worker submits work (oracle will be mocked)."""
        client = setup['client']
        task_id = setup['task_id']

        # Patch oracle launch to be a no-op (we'll settle manually)
        with patch('server._launch_oracle_with_timeout'):
            resp = client.post(f'/jobs/{task_id}/submit', json={
                'content': {
                    'text': 'Blocks chain together\nConsensus nodes verify\nTrust without a face'
                },
            }, headers=_auth(setup['worker_api_key']))

        data = resp.get_json()
        assert resp.status_code in (201, 202), f"Submit failed ({resp.status_code}): {data}"
        setup['submission_id'] = data['submission_id']
        print(f"\n  Submission created: {data['submission_id']}")
        print(f"  Oracle skipped (mocked)")

    def test_05_resolve_and_payout(self, setup):
        """Manually resolve job and trigger on-chain payout (simulates oracle RESOLVED)."""
        from models import Job, Submission, Agent, db
        from config import Config

        w3 = _w3()
        adapter = setup['adapter']
        task_id = setup['task_id']
        sub_id = setup['submission_id']

        # Record worker balance before payout
        worker_before = _usdc_balance(w3, WORKER_ADDR)
        print(f"\n  Worker USDG before: {worker_before}")

        # Step 1: Resolve job (what oracle would do)
        job = db.session.get(Job, task_id)
        sub = db.session.get(Submission, sub_id)
        job.status = 'resolved'
        job.winner_id = setup['worker_id']
        job.result_data = sub.content
        sub.status = 'passed'
        sub.oracle_score = 95
        sub.oracle_reason = 'E2E test — oracle skipped, manual resolve'
        db.session.commit()
        print(f"  Job resolved: winner={job.winner_id}")

        # Step 2: Trigger payout (what _run_oracle's payout section does)
        worker = db.session.get(Agent, setup['worker_id'])
        fee_bps = job.fee_bps if job.fee_bps is not None else Config.PLATFORM_FEE_BPS
        expected_share = (job.price * (Decimal(1) - Decimal(fee_bps) / Decimal(10000)))

        print(f"  Payout: {job.price} USDG at {fee_bps} bps → {expected_share} to worker")

        job.payout_status = 'pending'
        db.session.flush()

        payout_result = adapter.payout(worker.wallet_address, job.price, fee_bps)
        assert not payout_result.error, f"Payout failed: {payout_result.error}"

        job.payout_tx_hash = payout_result.payout_tx
        job.payout_status = 'success'
        db.session.commit()
        print(f"  Payout tx: {payout_result.payout_tx}")

        # Step 3: Verify on-chain balance
        receipt = w3.eth.wait_for_transaction_receipt(
            bytes.fromhex(payout_result.payout_tx.replace('0x', '')), timeout=30
        )
        assert receipt.status == 1, "Payout tx reverted on-chain"

        expected_bal = worker_before + expected_share
        worker_after = _wait_balance(w3, WORKER_ADDR, expected_bal)
        actual_received = worker_after - worker_before

        print(f"  Worker USDG after: {worker_after} (+{actual_received})")
        assert actual_received == expected_share, \
            f"Worker received {actual_received}, expected {expected_share}"

        setup['payout_tx'] = payout_result.payout_tx

    def test_06_verify_job_state(self, setup):
        """Verify final job state in DB matches expected settlement outcome."""
        from models import Job, Submission, db

        job = db.session.get(Job, setup['task_id'])
        sub = db.session.get(Submission, setup['submission_id'])

        print(f"\n  === Settlement Result ===")
        print(f"  Job status:      {job.status}")
        print(f"  Payout status:   {job.payout_status}")
        print(f"  Payout tx:       {job.payout_tx_hash}")
        print(f"  Winner:          {job.winner_id}")
        print(f"  Sub status:      {sub.status}")
        print(f"  Oracle score:    {sub.oracle_score}")

        assert job.status == 'resolved'
        assert job.payout_status == 'success'
        assert job.payout_tx_hash and job.payout_tx_hash != 'pending'
        assert job.winner_id == setup['worker_id']
        assert sub.status == 'passed'


# =============================================================================
# Flow 2: Expiry refund — deposit → funded job (expired) → auto-refund
# =============================================================================

class TestExpiryRefundFlow:
    """Expiry refund flow with real on-chain USDG refund."""

    TASK_PRICE = Decimal('0.10')

    def test_01_deposit_and_create_expired_job(self, setup):
        """Buyer deposits USDG, create job with past expiry."""
        client = setup['client']
        w3 = _w3()

        # Buyer deposits USDG
        print(f"\n  Depositing {self.TASK_PRICE} USDG for expiry test...")
        deposit_tx = _send_usdc_onchain(w3, BUYER_KEY, OPS_ADDR, self.TASK_PRICE)
        print(f"  Deposit tx: {deposit_tx}")

        # Create job
        resp = client.post('/jobs', json={
            'title': 'E2E Expiry Test',
            'description': 'This job will expire immediately',
            'price': float(self.TASK_PRICE),
            'expiry': int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }, headers=_auth(setup['buyer_api_key']))

        data = resp.get_json()
        assert resp.status_code == 201, f"Job creation failed: {data}"
        task_id = data['task_id']
        setup['expiry_task_id'] = task_id

        # Fund and set past expiry
        from models import Job, db
        job = db.session.get(Job, task_id)
        job.status = 'funded'
        job.deposit_tx_hash = deposit_tx
        job.depositor_address = BUYER_ADDR
        job.deposit_amount = self.TASK_PRICE
        job.chain_id = CHAIN_ID
        job.expiry = datetime.now(timezone.utc) - timedelta(minutes=5)  # already expired
        db.session.commit()
        print(f"  Job created & funded: {task_id} (expiry: 5 min ago)")

    def test_02_trigger_auto_refund(self, setup):
        """Trigger auto-refund via server's _auto_refund (same as expiry checker)."""
        from models import Job, db
        from server import _auto_refund

        w3 = _w3()
        task_id = setup['expiry_task_id']
        job = db.session.get(Job, task_id)

        # Record buyer balance before refund
        buyer_before = _usdc_balance(w3, BUYER_ADDR)
        print(f"\n  Buyer USDG before: {buyer_before}")

        # Mark as expired (what expiry checker does)
        job.status = 'expired'
        db.session.commit()
        print(f"  Job status → expired")

        # Trigger auto-refund (what expiry checker calls)
        refund_tx = _auto_refund(job, label="e2e-test")
        assert refund_tx, "Auto-refund returned None — check adapter connection"
        print(f"  Refund tx: {refund_tx}")

        setup['refund_tx'] = refund_tx

        # Wait for on-chain confirmation
        receipt = w3.eth.wait_for_transaction_receipt(
            bytes.fromhex(refund_tx.replace('0x', '')), timeout=30
        )
        assert receipt.status == 1, "Refund tx reverted on-chain"

        # Verify buyer balance increased
        expected_bal = buyer_before + self.TASK_PRICE
        buyer_after = _wait_balance(w3, BUYER_ADDR, expected_bal)
        actual_received = buyer_after - buyer_before

        print(f"  Buyer USDG after: {buyer_after} (+{actual_received})")
        assert actual_received == self.TASK_PRICE, \
            f"Buyer received {actual_received}, expected {self.TASK_PRICE}"

    def test_03_verify_job_state(self, setup):
        """Verify final job state shows refund completed."""
        from models import Job, db

        job = db.session.get(Job, setup['expiry_task_id'])

        print(f"\n  === Refund Result ===")
        print(f"  Job status:      {job.status}")
        print(f"  Refund tx:       {job.refund_tx_hash}")
        print(f"  Depositor:       {job.depositor_address}")
        print(f"  Deposit amount:  {job.deposit_amount}")

        assert job.status == 'expired'
        assert job.refund_tx_hash and job.refund_tx_hash != 'pending'
        assert job.depositor_address == BUYER_ADDR


# =============================================================================
# Summary
# =============================================================================

class TestFinalSummary:
    """Print final on-chain state."""

    def test_summary(self, setup):
        w3 = _w3()
        op_bal = _usdc_balance(w3, OPS_ADDR)
        buyer_bal = _usdc_balance(w3, BUYER_ADDR)
        worker_bal = _usdc_balance(w3, WORKER_ADDR)

        print(f"\n  ╔══════════════════════════════════════╗")
        print(f"  ║   X Layer Lifecycle E2E — Summary    ║")
        print(f"  ╠══════════════════════════════════════╣")
        print(f"  ║  Operator USDG:  {str(op_bal):>18} ║")
        print(f"  ║  Buyer USDG:     {str(buyer_bal):>18} ║")
        print(f"  ║  Worker USDG:    {str(worker_bal):>18} ║")
        print(f"  ╠══════════════════════════════════════╣")
        print(f"  ║  Settlement tx:  {setup.get('payout_tx', 'N/A')[:18]}║")
        print(f"  ║  Refund tx:      {setup.get('refund_tx', 'N/A')[:18]}║")
        print(f"  ╚══════════════════════════════════════╝")
