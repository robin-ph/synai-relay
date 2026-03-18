# tests/test_xlayer_adapter.py
"""Comprehensive XLayerAdapter unit tests — 25 cases."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from services.xlayer_adapter import XLayerAdapter, USDC_DECIMALS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_KEY = '0x' + 'ab' * 32
# Deterministic address derived from TEST_KEY
# (computed once so assertions can reference it)
from eth_account import Account as _Account
OPS_ADDRESS = _Account.from_key(TEST_KEY).address

USDC_CONTRACT = '0x4ae46a509F6b1D9056937BA4500cb143933D2dc8'
WORKER_ADDR = '0x' + '11' * 20   # valid-length hex address for tests
BUYER_ADDR = '0x' + '22' * 20


def _make_adapter(mock_client=None, with_key=True):
    """Return (adapter, mock_w3) with Web3 fully mocked."""
    if mock_client is None:
        mock_client = MagicMock()

    with patch('services.xlayer_adapter.Web3') as MockWeb3:
        mock_w3_instance = MagicMock()
        MockWeb3.return_value = mock_w3_instance
        MockWeb3.HTTPProvider = MagicMock()
        # Web3.to_checksum_address — just return the input (or uppercase version)
        MockWeb3.to_checksum_address = lambda addr: addr

        key = TEST_KEY if with_key else ''
        adapter = XLayerAdapter(
            onchainos_client=mock_client,
            ops_private_key=key,
            rpc_url='https://rpc.xlayer.tech',
            usdc_addr=USDC_CONTRACT,
        )
        # When with_key=True, the real Account.from_key runs (not mocked),
        # but Web3 is mocked so _w3 is mock_w3_instance.
        adapter._w3 = mock_w3_instance

    return adapter, mock_w3_instance


def _make_tx_response(status='2', transfers=None):
    """Build a mock OnchainOS transaction-detail response dict."""
    tx_data = {'txStatus': status}
    if transfers is not None:
        tx_data['tokenTransferDetails'] = transfers
    else:
        tx_data['tokenTransferDetails'] = []
    return {'code': '0', 'data': [tx_data]}


def _usdc_transfer(from_addr, to_addr, amount, token=USDC_CONTRACT):
    """Build a single tokenTransferDetails entry."""
    return {
        'tokenContractAddress': token,
        'from': from_addr,
        'to': to_addr,
        'amount': str(amount),
    }


def _setup_signing_chain(adapter, mock_client=None):
    """Wire up the mock objects needed for payout / refund signing chain."""
    mock_build_tx = MagicMock(return_value={
        'from': adapter._account.address,
        'gas': 100_000,
        'gasPrice': 1_000_000_000,
        'nonce': 0,
        'chainId': 196,
    })
    mock_transfer_fn = MagicMock()
    mock_transfer_fn.build_transaction = mock_build_tx
    adapter._usdc = MagicMock()
    adapter._usdc.functions.transfer.return_value = mock_transfer_fn

    adapter._w3.eth.gas_price = 1_000_000_000
    adapter._w3.eth.get_transaction_count.return_value = 0

    mock_signed = MagicMock()
    mock_signed.raw_transaction = b'\xde\xad\xbe\xef'
    adapter._account.sign_transaction = MagicMock(return_value=mock_signed)

    # Default broadcast success via OnchainOS
    if mock_client is None:
        mock_client = adapter._client
    mock_client.post.return_value = {
        'data': [{'txHash': '0xBROADCAST_TX_HASH', 'orderId': '0xORDER_ID'}]
    }


# ---------------------------------------------------------------------------
# TestVerifyDeposit — 11 tests
# ---------------------------------------------------------------------------

class TestVerifyDeposit:

    def test_success(self):
        adapter, _ = _make_adapter()
        ops = adapter.ops_address()
        adapter._client.get.return_value = _make_tx_response(
            transfers=[_usdc_transfer('0xBUYER', ops, '50')]
        )
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is True
        assert result.amount == Decimal('50')
        assert result.overpayment == Decimal('0')
        assert result.depositor == '0xBUYER'

    def test_overpayment(self):
        adapter, _ = _make_adapter()
        ops = adapter.ops_address()
        adapter._client.get.return_value = _make_tx_response(
            transfers=[_usdc_transfer('0xBUYER', ops, '60')]
        )
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is True
        assert result.overpayment == Decimal('10')

    def test_insufficient_amount(self):
        adapter, _ = _make_adapter()
        ops = adapter.ops_address()
        adapter._client.get.return_value = _make_tx_response(
            transfers=[_usdc_transfer('0xBUYER', ops, '30')]
        )
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert 'Insufficient' in result.error
        assert result.amount == Decimal('30')

    def test_wrong_token(self):
        adapter, _ = _make_adapter()
        ops = adapter.ops_address()
        adapter._client.get.return_value = _make_tx_response(
            transfers=[_usdc_transfer('0xBUYER', ops, '50', token='0xOTHER_TOKEN')]
        )
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert 'No token transfer' in result.error

    def test_wrong_recipient(self):
        adapter, _ = _make_adapter()
        adapter._client.get.return_value = _make_tx_response(
            transfers=[_usdc_transfer('0xBUYER', '0xWRONG_RECIPIENT', '50')]
        )
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert 'No token transfer' in result.error

    def test_pending_status(self):
        adapter, _ = _make_adapter()
        adapter._client.get.return_value = _make_tx_response(status='1')
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert '1' in result.error

    def test_failed_status(self):
        adapter, _ = _make_adapter()
        adapter._client.get.return_value = _make_tx_response(status='3')
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert '3' in result.error

    def test_no_transfers(self):
        adapter, _ = _make_adapter()
        adapter._client.get.return_value = _make_tx_response(transfers=[])
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert 'No token transfer' in result.error

    def test_api_error(self):
        adapter, _ = _make_adapter()
        adapter._client.get.side_effect = ConnectionError('timeout')
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert 'API error' in result.error

    def test_empty_data(self):
        adapter, _ = _make_adapter()
        adapter._client.get.return_value = {'data': []}
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert 'No transaction data' in result.error

    def test_missing_data_key(self):
        adapter, _ = _make_adapter()
        adapter._client.get.return_value = {'code': '0'}
        result = adapter.verify_deposit('0xTX', Decimal('50'))
        assert result.valid is False
        assert 'No transaction data' in result.error


# ---------------------------------------------------------------------------
# TestPayout — 6 tests
# ---------------------------------------------------------------------------

class TestPayout:

    def test_success(self):
        mock_client = MagicMock()
        adapter, _ = _make_adapter(mock_client=mock_client)
        _setup_signing_chain(adapter, mock_client)

        result = adapter.payout(WORKER_ADDR, Decimal('100'), 2000)
        assert result.payout_tx == '0xBROADCAST_TX_HASH'
        assert result.error == ''

        # Verify fee calculation: 100 USDC * (1 - 2000/10000) = 80 USDC = 80_000_000 atomic
        call_args = adapter._usdc.functions.transfer.call_args
        _to, amount_atomic = call_args[0]
        assert amount_atomic == 80_000_000

    def test_fee_calculation_precision(self):
        mock_client = MagicMock()
        adapter, _ = _make_adapter(mock_client=mock_client)
        _setup_signing_chain(adapter, mock_client)

        # 33.33 USDC with 1500 bps (15%) fee
        # worker_share = 33.33 * (1 - 0.15) = 33.33 * 0.85 = 28.3305 USDC
        # atomic = int(28.3305 * 10^6) = 28_330_500
        result = adapter.payout(WORKER_ADDR, Decimal('33.33'), 1500)
        assert result.error == ''

        call_args = adapter._usdc.functions.transfer.call_args
        _to, amount_atomic = call_args[0]
        assert amount_atomic == 28_330_500

    def test_broadcast_fallback(self):
        mock_client = MagicMock()
        adapter, mock_w3 = _make_adapter(mock_client=mock_client)
        _setup_signing_chain(adapter, mock_client)

        # OnchainOS broadcast fails
        mock_client.post.side_effect = ConnectionError('OnchainOS down')
        # RPC fallback succeeds
        mock_w3.eth.send_raw_transaction.return_value = b'\xca\xfe'

        result = adapter.payout(WORKER_ADDR, Decimal('100'), 2000)
        assert result.error == ''
        assert result.payout_tx  # should have a hash from RPC fallback
        mock_w3.eth.send_raw_transaction.assert_called_once()

    def test_total_failure(self):
        mock_client = MagicMock()
        adapter, mock_w3 = _make_adapter(mock_client=mock_client)
        _setup_signing_chain(adapter, mock_client)

        # Both broadcast paths fail
        mock_client.post.side_effect = ConnectionError('OnchainOS down')
        mock_w3.eth.send_raw_transaction.side_effect = Exception('RPC down')

        result = adapter.payout(WORKER_ADDR, Decimal('100'), 2000)
        assert result.error  # non-empty error

    def test_invalid_fee_bps_negative(self):
        adapter, _ = _make_adapter()
        result = adapter.payout(WORKER_ADDR, Decimal('100'), -100)
        assert 'Invalid fee_bps' in result.error

    def test_invalid_fee_bps_over_10000(self):
        adapter, _ = _make_adapter()
        result = adapter.payout(WORKER_ADDR, Decimal('100'), 10001)
        assert 'Invalid fee_bps' in result.error


# ---------------------------------------------------------------------------
# TestRefund — 2 tests
# ---------------------------------------------------------------------------

class TestRefund:

    def test_success(self):
        mock_client = MagicMock()
        adapter, _ = _make_adapter(mock_client=mock_client)
        _setup_signing_chain(adapter, mock_client)

        result = adapter.refund(BUYER_ADDR, Decimal('50'))
        assert result.tx_hash == '0xBROADCAST_TX_HASH'
        assert result.error == ''

        # Verify full amount — no fee deduction: 50 * 10^6 = 50_000_000
        call_args = adapter._usdc.functions.transfer.call_args
        _to, amount_atomic = call_args[0]
        assert amount_atomic == 50_000_000

    def test_failure(self):
        mock_client = MagicMock()
        adapter, mock_w3 = _make_adapter(mock_client=mock_client)
        _setup_signing_chain(adapter, mock_client)

        # Both broadcast paths fail
        mock_client.post.side_effect = ConnectionError('OnchainOS down')
        mock_w3.eth.send_raw_transaction.side_effect = Exception('RPC down')

        result = adapter.refund(BUYER_ADDR, Decimal('50'))
        assert result.error  # non-empty error


# ---------------------------------------------------------------------------
# TestMetadata — 6 tests
# ---------------------------------------------------------------------------

class TestMetadata:

    def test_chain_id(self):
        adapter, _ = _make_adapter()
        assert adapter.chain_id() == 196

    def test_chain_name(self):
        adapter, _ = _make_adapter()
        assert adapter.chain_name() == "X Layer"

    def test_caip2(self):
        adapter, _ = _make_adapter()
        assert adapter.caip2() == "eip155:196"

    def test_ops_address_from_key(self):
        adapter, _ = _make_adapter(with_key=True)
        addr = adapter.ops_address()
        assert addr  # non-empty
        assert addr.startswith('0x')

    def test_is_connected(self):
        adapter, mock_w3 = _make_adapter()
        mock_w3.is_connected.return_value = True
        assert adapter.is_connected() is True

    def test_is_connected_false(self):
        adapter, mock_w3 = _make_adapter()
        mock_w3.is_connected.return_value = False
        assert adapter.is_connected() is False
