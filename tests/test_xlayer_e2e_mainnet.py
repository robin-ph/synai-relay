"""X Layer MAINNET E2E tests — real on-chain USDG transactions with small amounts.

Skipped by default. Run with:
    RUN_E2E_XLAYER=1 pytest tests/test_xlayer_e2e_mainnet.py -v -s

Prerequisites:
    OPERATIONS_WALLET_KEY set (ops wallet with OKB for gas)
    XLAYER_RPC_URL=https://rpc.xlayer.tech (default)
    Buyer (arc_solver) funded with OKB + USDG on X Layer
    Worker (byte.runner) funded with OKB on X Layer

Optional (for verify_deposit tests):
    ONCHAINOS_API_KEY, ONCHAINOS_SECRET_KEY, ONCHAINOS_PASSPHRASE set

Wallet roles:
    Operator  0xB408...  — receives deposits, sends payouts/refunds
    Buyer     0xf808...  — arc_solver, sends USDG deposits
    Worker    0xbAE2...  — byte.runner, receives payouts
"""
import os
import time
import pytest
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account

pytestmark = pytest.mark.skipif(
    not os.environ.get('RUN_E2E_XLAYER'),
    reason="X Layer mainnet E2E: set RUN_E2E_XLAYER=1 to run"
)

# --- Constants ---
XLAYER_RPC = os.environ.get('XLAYER_RPC_URL', 'https://rpc.xlayer.tech')
USDC_CONTRACT = '0x4ae46a509f6b1d9056937ba4500cb143933d2dc8'
CHAIN_ID = 196
FEE_BPS = 2000  # 20%

# Test amounts (keep small!)
DEPOSIT_AMOUNT = Decimal('0.10')
PAYOUT_AMOUNT = Decimal('0.10')
REFUND_AMOUNT = Decimal('0.01')

# Buyer (arc_solver)
BUYER_KEY = os.environ.get('TEST_BUYER_WALLET_KEY', '')
BUYER_ADDR = '0xf808390B22F56a47ddEE15053Eb10A9674aDe0F4'

# Worker (byte.runner)
WORKER_ADDR = '0xbAE26E65D1C1246D7B7f2574980C1d93C31Eae6F'

_ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

# Shared state between tests
_state = {}


@pytest.fixture(scope='module')
def w3():
    w = Web3(Web3.HTTPProvider(XLAYER_RPC))
    assert w.is_connected(), f"Cannot connect to {XLAYER_RPC}"
    return w


@pytest.fixture(scope='module')
def usdc(w3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(USDC_CONTRACT), abi=_ERC20_ABI
    )


@pytest.fixture(scope='module')
def adapter():
    """Create XLayerAdapter with real ops key, optional OnchainOS client."""
    from services.xlayer_adapter import XLayerAdapter

    ops_key = os.environ.get('OPERATIONS_WALLET_KEY', '')
    if not ops_key:
        pytest.skip("OPERATIONS_WALLET_KEY not set")

    # OnchainOS client — optional, needed for verify_deposit
    onchainos_client = None
    api_key = os.environ.get('ONCHAINOS_API_KEY', '')
    if api_key:
        from services.onchainos_client import OnchainOSClient
        onchainos_client = OnchainOSClient(
            api_key=api_key,
            secret_key=os.environ['ONCHAINOS_SECRET_KEY'],
            passphrase=os.environ['ONCHAINOS_PASSPHRASE'],
            project_id=os.environ.get('ONCHAINOS_PROJECT_ID', ''),
        )

    return XLayerAdapter(
        onchainos_client=onchainos_client,
        ops_private_key=ops_key,
        rpc_url=XLAYER_RPC,
        usdc_addr=USDC_CONTRACT,
    )


def _usdc_balance(usdc, addr: str) -> Decimal:
    raw = usdc.functions.balanceOf(Web3.to_checksum_address(addr)).call()
    return Decimal(raw) / Decimal(10 ** 6)


def _wait_for_balance(usdc, addr: str, min_balance: Decimal,
                      timeout: int = 15) -> Decimal:
    """Poll until balance >= min_balance or timeout. Handles RPC staleness."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        bal = _usdc_balance(usdc, addr)
        if bal >= min_balance:
            return bal
        time.sleep(2)
    return _usdc_balance(usdc, addr)


def _wait_for_tx(w3, tx_hash_hex: str, timeout: int = 30):
    """Wait for tx receipt and assert success."""
    receipt = w3.eth.wait_for_transaction_receipt(
        bytes.fromhex(tx_hash_hex.replace('0x', '')), timeout=timeout
    )
    assert receipt.status == 1, f"Tx reverted on-chain: {tx_hash_hex}"
    return receipt


def _send_usdc(w3, usdc, from_key: str, to: str, amount: Decimal) -> str:
    """Send USDG via direct ERC-20 transfer. Returns tx hash hex."""
    account = Account.from_key(from_key)
    amount_atomic = int(amount * 10 ** 6)
    tx = usdc.functions.transfer(
        Web3.to_checksum_address(to), amount_atomic
    ).build_transaction({
        'from': account.address,
        'gas': 100_000,
        'gasPrice': w3.eth.gas_price,
        'nonce': w3.eth.get_transaction_count(account.address),
        'chainId': CHAIN_ID,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    assert receipt.status == 1, f"USDG transfer failed: {tx_hash.hex()}"
    return tx_hash.hex()


class TestXLayerE2EMainnet:
    """Sequential E2E tests — run in order."""

    def test_01_connectivity(self, adapter, w3):
        """Adapter connects to X Layer mainnet."""
        assert adapter.chain_id() == 196
        assert adapter.chain_name() == "X Layer"
        assert adapter.ops_address().startswith('0x')
        print(f"\n  Operator: {adapter.ops_address()}")
        print(f"  Buyer:    {BUYER_ADDR}")
        print(f"  Worker:   {WORKER_ADDR}")

    def test_02_initial_balances(self, usdc):
        """Record initial balances before any transfers."""
        _state['op_before'] = _usdc_balance(usdc, os.environ['OPERATIONS_WALLET_ADDRESS'])
        _state['buyer_before'] = _usdc_balance(usdc, BUYER_ADDR)
        _state['worker_before'] = _usdc_balance(usdc, WORKER_ADDR)

        print(f"\n  Operator USDG: {_state['op_before']}")
        print(f"  Buyer USDG:    {_state['buyer_before']}")
        print(f"  Worker USDG:   {_state['worker_before']}")

        assert _state['buyer_before'] >= DEPOSIT_AMOUNT, \
            f"Buyer needs at least {DEPOSIT_AMOUNT} USDG, has {_state['buyer_before']}"

    def test_03_buyer_deposits_to_operator(self, w3, usdc, adapter):
        """Buyer sends USDG to operator (simulating task escrow deposit)."""
        ops = adapter.ops_address()
        print(f"\n  Depositing {DEPOSIT_AMOUNT} USDG: {BUYER_ADDR[:10]}... -> {ops[:10]}...")

        tx_hash = _send_usdc(w3, usdc, BUYER_KEY, ops, DEPOSIT_AMOUNT)
        _state['deposit_tx'] = tx_hash
        print(f"  Deposit tx: {tx_hash}")

        # Verify operator balance increased (RPC may need a moment)
        expected = _state['op_before'] + DEPOSIT_AMOUNT
        op_after = _wait_for_balance(usdc, ops, expected)
        assert op_after >= expected, \
            f"Operator balance didn't increase: {_state['op_before']} -> {op_after}"
        print(f"  Operator USDG: {_state['op_before']} -> {op_after}")

    def test_04_verify_deposit(self, adapter):
        """verify_deposit confirms the buyer's USDG transfer (requires OnchainOS)."""
        if not os.environ.get('ONCHAINOS_API_KEY'):
            pytest.skip("OnchainOS credentials not set — skipping verify_deposit")

        tx_hash = _state.get('deposit_tx')
        assert tx_hash, "No deposit tx from prior test"

        # OnchainOS may need time to index the tx — retry on transient errors
        # "no transaction data" = tx not indexed yet
        # "no token transfer" = tx indexed but token transfers not parsed yet
        result = None
        transient_errors = ('pending', 'no transaction data', 'no token transfer')
        for attempt in range(10):
            time.sleep(5)
            result = adapter.verify_deposit(tx_hash, DEPOSIT_AMOUNT)
            if result.valid:
                break
            err_lower = (result.error or '').lower()
            if any(t in err_lower for t in transient_errors):
                print(f"  Attempt {attempt + 1}: {result.error} (retrying...)")
                continue
            break  # non-transient error, stop retrying

        print(f"  verify_deposit result: valid={result.valid}, amount={result.amount}")
        assert result.valid, f"verify_deposit failed: {result.error}"
        assert result.amount >= DEPOSIT_AMOUNT

    def test_05_payout_to_worker(self, w3, adapter, usdc):
        """Operator pays out to worker with platform fee."""
        worker_before = _usdc_balance(usdc, WORKER_ADDR)
        expected_share = PAYOUT_AMOUNT * (Decimal(1) - Decimal(FEE_BPS) / Decimal(10000))

        print(f"\n  Payout {PAYOUT_AMOUNT} USDG at {FEE_BPS} bps to {WORKER_ADDR[:10]}...")
        print(f"  Expected worker share: {expected_share} USDG")

        result = adapter.payout(WORKER_ADDR, PAYOUT_AMOUNT, FEE_BPS)
        assert not result.error, f"Payout failed: {result.error}"
        assert result.payout_tx, "No payout tx hash returned"
        _state['payout_tx'] = result.payout_tx
        print(f"  Payout tx: {result.payout_tx}")

        # Wait for on-chain confirmation then verify balance
        _wait_for_tx(w3, result.payout_tx)
        expected_bal = worker_before + expected_share
        worker_after = _wait_for_balance(usdc, WORKER_ADDR, expected_bal)
        actual_received = worker_after - worker_before

        print(f"  Worker USDG: {worker_before} -> {worker_after} (+{actual_received})")
        assert actual_received == expected_share, \
            f"Worker received {actual_received}, expected {expected_share}"

    def test_06_refund_to_buyer(self, w3, adapter, usdc):
        """Operator refunds USDG to buyer."""
        buyer_before = _usdc_balance(usdc, BUYER_ADDR)

        print(f"\n  Refunding {REFUND_AMOUNT} USDG to {BUYER_ADDR[:10]}...")

        result = adapter.refund(BUYER_ADDR, REFUND_AMOUNT)
        assert not result.error, f"Refund failed: {result.error}"
        assert result.tx_hash, "No refund tx hash returned"
        _state['refund_tx'] = result.tx_hash
        print(f"  Refund tx: {result.tx_hash}")

        # Wait for on-chain confirmation then verify balance
        _wait_for_tx(w3, result.tx_hash)
        expected_bal = buyer_before + REFUND_AMOUNT
        buyer_after = _wait_for_balance(usdc, BUYER_ADDR, expected_bal)
        actual_received = buyer_after - buyer_before

        print(f"  Buyer USDG: {buyer_before} -> {buyer_after} (+{actual_received})")
        assert actual_received == REFUND_AMOUNT, \
            f"Buyer received {actual_received}, expected {REFUND_AMOUNT}"

    def test_07_final_balances(self, adapter, usdc):
        """Report final balances and net flow."""
        ops = adapter.ops_address()
        op_final = _usdc_balance(usdc, ops)
        buyer_final = _usdc_balance(usdc, BUYER_ADDR)
        worker_final = _usdc_balance(usdc, WORKER_ADDR)

        fee_kept = PAYOUT_AMOUNT - PAYOUT_AMOUNT * (Decimal(1) - Decimal(FEE_BPS) / Decimal(10000))
        op_net = op_final - _state['op_before']

        print(f"\n  === Final Balances ===")
        print(f"  Operator: {op_final} USDG (net: +{op_net}, fee kept: {fee_kept})")
        print(f"  Buyer:    {buyer_final} USDG")
        print(f"  Worker:   {worker_final} USDG")
        print(f"\n  === Transaction Hashes ===")
        print(f"  Deposit: {_state.get('deposit_tx', 'N/A')}")
        print(f"  Payout:  {_state.get('payout_tx', 'N/A')}")
        print(f"  Refund:  {_state.get('refund_tx', 'N/A')}")

        # Operator net: deposit(0.10) - worker_share(0.08) - refund(0.01) = +0.01
        # Fee kept from payout: 0.02, minus refund: 0.01 → net +0.01
        assert op_final >= 0, "Operator USDG balance went negative somehow"
