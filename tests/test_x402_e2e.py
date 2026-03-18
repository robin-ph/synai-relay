"""X402 E2E tests — real x402 payment protocol on X Layer.

Exercises the ACTUAL x402 flow end-to-end:
  Client POST /jobs (no header) -> 402 -> client signs EIP-3009 ->
  POST /jobs (with X-PAYMENT header) -> server verify -> settle ->
  funded job (201)

Skipped by default. Run with:
    RUN_E2E_X402=1 pytest tests/test_x402_e2e.py -v -s

Prerequisites:
    TEST_BUYER_WALLET_KEY set (buyer wallet private key)
    OPERATIONS_WALLET_KEY set (ops wallet)
    ONCHAINOS_API_KEY, ONCHAINOS_SECRET_KEY, ONCHAINOS_PASSPHRASE set
    Buyer wallet funded with USDG on X Layer

Wallet roles:
    Buyer   0xf808...  — signs x402 payment (EIP-3009 transferWithAuthorization)
    Ops     0xB408...  — receives USDG via x402 settlement
"""
import os
import pytest
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv(override=True)

pytestmark = pytest.mark.skipif(
    not os.environ.get('RUN_E2E_X402'),
    reason="x402 E2E: set RUN_E2E_X402=1 to run"
)

# --- Constants ---

BUYER_KEY = os.environ.get('TEST_BUYER_WALLET_KEY', '')
BUYER_ADDR = '0xf808390B22F56a47ddEE15053Eb10A9674aDe0F4'
OPS_ADDR = os.environ.get('OPERATIONS_WALLET_ADDRESS', '')
TASK_PRICE = Decimal('0.10')

# Shared state between tests
_state = {}


# --- Fixtures ---

@pytest.fixture(scope='module')
def setup():
    """Start Flask app with x402 ENABLED and real credentials."""
    os.environ['DATABASE_URL'] = 'sqlite://'

    import server as _srv
    from server import app, _init_x402
    from models import db

    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['X402_ENABLED'] = True

    with app.app_context():
        db.create_all()

        # Initialize x402 facilitators and chain registry
        _init_x402()
        _srv._x402_initialized = True

        yield {
            'client': app.test_client(),
            'app': app,
            'db': db,
        }

        from server import _shutdown_event, _oracle_executor
        _shutdown_event.set()
        try:
            _oracle_executor.shutdown(wait=True)
        except Exception:
            pass


@pytest.fixture(scope='module')
def x402_client():
    """Create x402 client-side SDK for signing payments."""
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.mechanisms.evm.exact.register import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner

    account = Account.from_key(BUYER_KEY)
    client = x402ClientSync()
    register_exact_evm_client(client, EthAccountSigner(account))
    return client


def _register_buyer(client):
    """Register buyer agent, return api_key."""
    resp = client.post('/agents', json={
        'agent_id': 'x402-buyer',
        'name': 'X402 E2E Buyer',
        'wallet_address': BUYER_ADDR,
    })
    data = resp.get_json()
    assert resp.status_code == 201, f"Agent registration failed: {data}"
    return data['api_key']


def _auth(api_key):
    return {'Authorization': f'Bearer {api_key}'}


def _create_job_with_x402(flask_client, x402_sdk, api_key, title,
                           preferred_network=None):
    """Execute the full x402 flow: POST -> 402 -> sign -> POST with payment.

    Returns (response_data, status_code, headers) from the funded request.
    """
    from x402.http import (
        encode_payment_signature_header,
        X_PAYMENT_HEADER,
    )

    # Step 1: POST /jobs WITHOUT payment header -> expect 402
    job_body = {
        'title': title,
        'description': f'x402 E2E test: {title}',
        'price': float(TASK_PRICE),
    }
    resp = flask_client.post('/jobs', json=job_body, headers=_auth(api_key))
    data = resp.get_json()

    assert resp.status_code == 402, (
        f"Expected 402 Payment Required, got {resp.status_code}: {data}"
    )

    payment_required_header = resp.headers.get('PAYMENT-REQUIRED')
    assert payment_required_header, "No PAYMENT-REQUIRED header in 402 response"

    # Decode the payment options
    from x402.http import decode_payment_required_header
    payment_required = decode_payment_required_header(payment_required_header)

    print(f"\n  402 received — {len(payment_required.accepts)} chain(s) offered:")
    for req in payment_required.accepts:
        print(f"    {req.network}: {req.asset[:10]}... amount={req.amount} "
              f"extra={req.extra}")

    # If user wants a specific chain, filter
    if preferred_network:
        original = payment_required.accepts
        payment_required.accepts = [
            r for r in original if r.network == preferred_network
        ]
        assert payment_required.accepts, (
            f"No requirements for {preferred_network}. "
            f"Available: {[r.network for r in original]}"
        )

    # Step 2: Sign payment with x402 client SDK
    payload = x402_sdk.create_payment_payload(payment_required)
    payment_header = encode_payment_signature_header(payload)

    print(f"  Payment signed for {payload.accepted.network}")
    print(f"  Payload header length: {len(payment_header)}")

    # Step 3: POST /jobs WITH payment header -> expect 201 funded
    resp2 = flask_client.post(
        '/jobs', json=job_body,
        headers={
            **_auth(api_key),
            X_PAYMENT_HEADER: payment_header,
        },
    )
    data2 = resp2.get_json()
    return data2, resp2.status_code, resp2.headers


# =============================================================================
# Tests
# =============================================================================

class TestX402Registration:
    """Register buyer agent (shared across all x402 tests)."""

    def test_register(self, setup):
        api_key = _register_buyer(setup['client'])
        _state['api_key'] = api_key
        print(f"\n  Buyer registered: x402-buyer")


class TestX402XLayer:
    """x402 payment on X Layer (chain 196, OKX facilitator)."""

    def test_01_402_offers_xlayer(self, setup):
        """Verify the 402 response offers X Layer."""
        if 'api_key' not in _state:
            pytest.skip("Registration test did not run — api_key not set")
        if not os.environ.get('ONCHAINOS_API_KEY'):
            pytest.skip("OnchainOS credentials not set")

        api_key = _state['api_key']
        from x402.http import decode_payment_required_header

        resp = setup['client'].post('/jobs', json={
            'title': 'Chain discovery test',
            'description': 'Testing 402 response contains X Layer',
            'price': float(TASK_PRICE),
        }, headers=_auth(api_key))

        assert resp.status_code == 402
        pr = decode_payment_required_header(resp.headers['PAYMENT-REQUIRED'])

        networks = [r.network for r in pr.accepts]
        print(f"\n  Chains offered in 402: {networks}")

        assert 'eip155:196' in networks, "X Layer not offered"
        print(f"  X Layer offered: YES")

    def test_02_create_funded_job(self, setup, x402_client):
        """Full x402 flow: 402 -> sign EIP-3009 -> verify -> settle -> funded."""
        if 'api_key' not in _state:
            pytest.skip("Registration test did not run — api_key not set")
        if not os.environ.get('ONCHAINOS_API_KEY'):
            pytest.skip("OnchainOS credentials not set")

        api_key = _state['api_key']

        print(f"\n  === X Layer x402 Flow ===")
        data, status, headers = _create_job_with_x402(
            setup['client'], x402_client, api_key,
            'x402 XLayer E2E',
            preferred_network='eip155:196',
        )

        assert status == 201, f"Expected 201, got {status}: {data}"
        assert data.get('status') == 'funded', f"Expected funded, got: {data}"
        assert 'x402_settlement' in data, f"No x402_settlement in response: {data}"

        settlement = data['x402_settlement']
        print(f"  Job created: {data['task_id']}")
        print(f"  Status: {data['status']}")
        print(f"  Chain: {settlement['chain_id']}")
        print(f"  Deposit tx: {settlement['tx_hash']}")
        print(f"  Depositor: {settlement['depositor']}")
        print(f"  Amount: {settlement['amount']} USDG")

        assert settlement['chain_id'] == 196
        assert settlement['tx_hash']
        assert settlement['depositor'].lower() == BUYER_ADDR.lower()
        assert settlement['amount'] == float(TASK_PRICE)

        pr_header = headers.get('PAYMENT-RESPONSE')
        assert pr_header, "No PAYMENT-RESPONSE header in 201 response"
        print(f"  PAYMENT-RESPONSE header present: YES")

        _state['xlayer_task_id'] = data['task_id']
        _state['xlayer_tx'] = settlement['tx_hash']

    def test_03_verify_job_state(self, setup):
        """Verify the funded job is correctly stored in DB."""
        if 'xlayer_task_id' not in _state:
            pytest.skip("X Layer job not created")

        from models import Job

        job = setup['db'].session.get(Job, _state['xlayer_task_id'])
        assert job.status == 'funded'
        assert job.chain_id == 196
        assert job.deposit_tx_hash == _state['xlayer_tx']
        assert job.depositor_address.lower() == BUYER_ADDR.lower()
        assert job.deposit_amount == TASK_PRICE
        print(f"\n  X Layer job DB state verified: funded, chain=196")


class TestX402Summary:
    """Print summary of x402 E2E results."""

    def test_summary(self, setup):
        print(f"\n  ╔══════════════════════════════════════════╗")
        print(f"  ║   X402 E2E Results                       ║")
        print(f"  ╠══════════════════════════════════════════╣")
        if 'xlayer_task_id' in _state:
            print(f"  ║  X Layer (196): FUNDED                   ║")
            print(f"  ║    Task: {_state['xlayer_task_id'][:24]}...  ║")
            print(f"  ║    Tx:   {_state['xlayer_tx'][:24]}...  ║")
        else:
            print(f"  ║  X Layer (196): NOT RUN                  ║")
        print(f"  ║  Base (8453):   PENDING (needs CDP keys) ║")
        print(f"  ╚══════════════════════════════════════════╝")
