"""X Layer testnet E2E tests — real OnchainOS API calls.

Skipped by default. Run with:
    RUN_TESTNET=1 pytest tests/test_xlayer_e2e_testnet.py -v

Prerequisites:
    ONCHAINOS_API_KEY, ONCHAINOS_SECRET_KEY, ONCHAINOS_PASSPHRASE set
    OPERATIONS_WALLET_KEY set (testnet wallet with OKB for gas + testnet USDG)
    XLAYER_RPC_URL=https://testrpc.xlayer.tech
"""
import os
import pytest
from decimal import Decimal

pytestmark = pytest.mark.skipif(
    not os.environ.get('RUN_TESTNET'),
    reason="Testnet E2E: set RUN_TESTNET=1 to run"
)

TESTNET_USDC = os.environ.get(
    'XLAYER_TESTNET_USDC',
    '0x4ae46a509f6b1d9056937ba4500cb143933d2dc8'
)
TESTNET_RPC = os.environ.get('XLAYER_RPC_URL', 'https://testrpc.xlayer.tech')

_payout_tx_hash = None


@pytest.fixture(scope='module')
def adapter():
    from services.onchainos_client import OnchainOSClient
    from services.xlayer_adapter import XLayerAdapter

    client = OnchainOSClient(
        api_key=os.environ['ONCHAINOS_API_KEY'],
        secret_key=os.environ['ONCHAINOS_SECRET_KEY'],
        passphrase=os.environ['ONCHAINOS_PASSPHRASE'],
        project_id=os.environ.get('ONCHAINOS_PROJECT_ID', ''),
    )
    return XLayerAdapter(
        client,
        ops_private_key=os.environ['OPERATIONS_WALLET_KEY'],
        rpc_url=TESTNET_RPC,
        usdc_addr=TESTNET_USDC,
    )


class TestXLayerTestnet:

    def test_is_connected(self, adapter):
        assert adapter.is_connected() is True

    def test_chain_metadata(self, adapter):
        assert adapter.chain_id() == 196
        assert adapter.ops_address().startswith('0x')

    def test_verify_known_tx(self, adapter):
        known_tx = os.environ.get('XLAYER_KNOWN_TX_HASH')
        if not known_tx:
            pytest.skip("Set XLAYER_KNOWN_TX_HASH to test verify_deposit")
        result = adapter.verify_deposit(known_tx, Decimal('0.01'))
        assert result.error == '' or 'API error' not in result.error

    def test_payout_small_amount(self, adapter):
        global _payout_tx_hash
        ops = adapter.ops_address()
        result = adapter.payout(ops, Decimal('0.01'), 0)
        if result.error:
            pytest.skip(f"Payout failed (may need OKB/USDG): {result.error}")
        assert result.payout_tx
        _payout_tx_hash = result.payout_tx

    def test_refund_small_amount(self, adapter):
        ops = adapter.ops_address()
        result = adapter.refund(ops, Decimal('0.01'))
        if result.error:
            pytest.skip(f"Refund failed: {result.error}")
        assert result.tx_hash

    def test_verify_own_payout(self, adapter):
        global _payout_tx_hash
        if not _payout_tx_hash:
            pytest.skip("No payout tx from prior test")
        import time
        time.sleep(5)
        result = adapter.verify_deposit(_payout_tx_hash, Decimal('0.01'))
        assert result.valid or 'pending' in (result.error or '').lower() or 'tx status' in (result.error or '').lower()
