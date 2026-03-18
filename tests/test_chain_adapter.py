# tests/test_chain_adapter.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from decimal import Decimal
from services.chain_adapter import DepositResult, PayoutResult, RefundResult


class TestDepositResult:
    def test_defaults(self):
        r = DepositResult(valid=False)
        assert r.valid is False
        assert r.depositor == ""
        assert r.amount == Decimal(0)
        assert r.error == ""
        assert r.overpayment == Decimal(0)

    def test_valid_deposit(self):
        r = DepositResult(valid=True, depositor="0xABC", amount=Decimal("50.0"))
        assert r.valid is True
        assert r.depositor == "0xABC"
        assert r.amount == Decimal("50.0")


class TestPayoutResult:
    def test_defaults(self):
        r = PayoutResult()
        assert r.payout_tx == ""
        assert r.fee_tx == ""
        assert r.pending is False

    def test_success(self):
        r = PayoutResult(payout_tx="0x123", fee_tx="0x456")
        assert r.payout_tx == "0x123"
        assert r.fee_tx == "0x456"


class TestRefundResult:
    def test_defaults(self):
        r = RefundResult()
        assert r.tx_hash == ""
        assert r.error == ""


from unittest.mock import MagicMock
from services.base_adapter import BaseAdapter


class TestBaseAdapter:
    def _make_adapter(self, connected=True, ops_address="0xOPS"):
        ws = MagicMock()
        ws.is_connected.return_value = connected
        ws.usdc_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        ws.ops_address = ops_address
        return BaseAdapter(ws), ws

    def test_chain_metadata(self):
        adapter, _ = self._make_adapter()
        assert adapter.chain_id() == 8453
        assert adapter.chain_name() == "Base"
        assert adapter.caip2() == "eip155:8453"
        assert adapter.usdc_address() == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert adapter.ops_address() == "0xOPS"

    def test_is_connected(self):
        adapter, _ = self._make_adapter(connected=True)
        assert adapter.is_connected() is True
        adapter2, _ = self._make_adapter(connected=False)
        assert adapter2.is_connected() is False

    def test_verify_deposit(self):
        adapter, ws = self._make_adapter()
        ws.verify_deposit.return_value = {
            "valid": True, "depositor": "0xBUYER", "amount": Decimal("50.0"),
        }
        result = adapter.verify_deposit("0xtx", Decimal("50.0"))
        assert result.valid is True
        assert result.depositor == "0xBUYER"
        ws.verify_deposit.assert_called_once_with("0xtx", Decimal("50.0"))

    def test_verify_deposit_with_overpayment(self):
        adapter, ws = self._make_adapter()
        ws.verify_deposit.return_value = {
            "valid": True, "depositor": "0xBUYER",
            "amount": Decimal("55.0"), "overpayment": 5.0,
        }
        result = adapter.verify_deposit("0xtx", Decimal("50.0"))
        assert result.valid is True
        assert result.overpayment == Decimal("5.0")

    def test_payout(self):
        adapter, ws = self._make_adapter()
        ws.payout.return_value = {"payout_tx": "0xPAY", "fee_tx": "0xFEE"}
        result = adapter.payout("0xWORKER", Decimal("50.0"), 2000)
        assert result.payout_tx == "0xPAY"
        assert result.fee_tx == "0xFEE"
        assert result.pending is False

    def test_payout_pending(self):
        adapter, ws = self._make_adapter()
        ws.payout.return_value = {
            "payout_tx": "0xPAY", "fee_tx": None,
            "pending": True, "error": "timeout",
        }
        result = adapter.payout("0xWORKER", Decimal("50.0"), 2000)
        assert result.pending is True
        assert result.payout_tx == "0xPAY"

    def test_refund(self):
        """WalletService.refund returns a str, not a dict."""
        adapter, ws = self._make_adapter()
        ws.refund.return_value = "0xREFUND"
        result = adapter.refund("0xBUYER", Decimal("50.0"))
        assert result.tx_hash == "0xREFUND"
        assert result.error == ""


import pytest
from services.chain_registry import ChainRegistry


class TestChainRegistry:
    def _make_adapter(self, cid=8453, name="Base"):
        a = MagicMock()
        a.chain_id.return_value = cid
        a.chain_name.return_value = name
        a.caip2.return_value = f"eip155:{cid}"
        a.usdc_address.return_value = "0xUSDC"
        return a

    def test_register_and_get(self):
        reg = ChainRegistry()
        adapter = self._make_adapter(8453, "Base")
        reg.register(adapter)
        assert reg.get(8453) is adapter

    def test_get_unknown_raises(self):
        reg = ChainRegistry()
        with pytest.raises(ValueError, match="Unsupported chain"):
            reg.get(999)

    def test_default(self):
        reg = ChainRegistry(default_chain_id=8453)
        adapter = self._make_adapter(8453)
        reg.register(adapter)
        assert reg.default() is adapter

    def test_default_not_registered_raises(self):
        reg = ChainRegistry(default_chain_id=8453)
        with pytest.raises(RuntimeError, match="Default chain"):
            reg.default()

    def test_adapters_list(self):
        reg = ChainRegistry()
        a1 = self._make_adapter(8453, "Base")
        a2 = self._make_adapter(196, "X Layer")
        reg.register(a1)
        reg.register(a2)
        adapters = reg.adapters()
        assert len(adapters) == 2

    def test_supported_chains(self):
        reg = ChainRegistry()
        a = self._make_adapter(8453, "Base")
        reg.register(a)
        chains = reg.supported_chains()
        assert len(chains) == 1
        assert chains[0]["chain_id"] == 8453
        assert chains[0]["name"] == "Base"

    def test_get_or_default_with_none(self):
        """NULL chain_id should return default adapter (Base)."""
        reg = ChainRegistry(default_chain_id=8453)
        adapter = self._make_adapter(8453)
        reg.register(adapter)
        assert reg.get_or_default(None) is adapter

    def test_get_or_default_with_valid_id(self):
        reg = ChainRegistry(default_chain_id=8453)
        a1 = self._make_adapter(8453)
        a2 = self._make_adapter(196, "X Layer")
        reg.register(a1)
        reg.register(a2)
        assert reg.get_or_default(196) is a2


from services.onchainos_client import OnchainOSClient


class TestOnchainOSClient:
    def test_sign(self):
        """HMAC signature must be deterministic for same input."""
        client = OnchainOSClient(
            api_key="test-key",
            secret_key="test-secret",
            passphrase="test-pass",
        )
        sig1 = client._sign("2026-03-15T00:00:00.000Z", "POST", "/api/v6/x402/verify", '{"foo":"bar"}')
        sig2 = client._sign("2026-03-15T00:00:00.000Z", "POST", "/api/v6/x402/verify", '{"foo":"bar"}')
        assert sig1 == sig2
        assert len(sig1) > 0

    def test_headers(self):
        client = OnchainOSClient(
            api_key="test-key",
            secret_key="test-secret",
            passphrase="test-pass",
            project_id="proj-1",
        )
        headers = client._headers("POST", "/api/test", "body")
        assert headers["OK-ACCESS-KEY"] == "test-key"
        assert headers["OK-ACCESS-PASSPHRASE"] == "test-pass"
        assert headers["OK-ACCESS-PROJECT"] == "proj-1"
        assert "OK-ACCESS-SIGN" in headers
        assert "OK-ACCESS-TIMESTAMP" in headers


from services.okx_facilitator import OKXFacilitatorClient, _network_to_chain_index


class TestOKXFacilitator:
    def test_network_to_chain_index(self):
        assert _network_to_chain_index("eip155:196") == "196"
        assert _network_to_chain_index("eip155:8453") == "8453"

    def test_verify_translates_response(self):
        """Mock OnchainOS client and verify response translation."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "code": "0",
            "data": [{"isValid": True, "payer": "0xPAYER"}],
        }
        fac = OKXFacilitatorClient.__new__(OKXFacilitatorClient)
        fac._client = mock_client

        # Create mock payload and requirements
        payload = MagicMock()
        payload.model_dump.return_value = {"test": "payload"}
        requirements = MagicMock()
        requirements.scheme = "exact"
        requirements.network = "eip155:196"
        requirements.amount = "50000000"
        requirements.pay_to = "0xOPS"
        requirements.asset = "0xUSDC"

        result = fac.verify(payload, requirements)
        assert result.is_valid is True
        assert result.payer == "0xPAYER"

    def test_settle_translates_response(self):
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "code": "0",
            "data": [{
                "success": True,
                "txHash": "0xTX123",
                "chainIndex": "196",
                "payer": "0xPAYER",
            }],
        }
        fac = OKXFacilitatorClient.__new__(OKXFacilitatorClient)
        fac._client = mock_client

        payload = MagicMock()
        payload.model_dump.return_value = {}
        requirements = MagicMock()
        requirements.scheme = "exact"
        requirements.network = "eip155:196"
        requirements.amount = "50000000"
        requirements.pay_to = "0xOPS"
        requirements.asset = "0xUSDC"

        result = fac.settle(payload, requirements)
        assert result.success is True
        assert result.transaction == "0xTX123"
        assert result.network == "eip155:196"


from services.xlayer_adapter import XLayerAdapter


class TestXLayerAdapter:
    def test_chain_metadata(self):
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client)
        assert adapter.chain_id() == 196
        assert adapter.chain_name() == "X Layer"
        assert adapter.caip2() == "eip155:196"

    def test_is_connected_with_client_only(self):
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client)
        assert adapter.is_connected() is True

    def test_usdc_address(self):
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client, usdc_addr='0x4ae46a509f6b1d9056937ba4500cb143933d2dc8')
        assert adapter.usdc_address() == '0x4ae46a509F6b1D9056937BA4500cb143933D2dc8'

    def test_ops_address_without_key(self):
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client)
        assert adapter.ops_address() == ''

    def test_verify_deposit_without_key(self):
        """verify_deposit should fail early without ops key."""
        from decimal import Decimal
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client)
        result = adapter.verify_deposit("0x" + "ab" * 32, Decimal("50"))
        assert result.valid is False
        assert "No ops wallet configured" in result.error

    def test_payout_without_key(self):
        """payout should fail gracefully without ops key."""
        from decimal import Decimal
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client)
        result = adapter.payout("0xWORKER", Decimal("50"), 2000)
        assert result.error

    def test_refund_without_key(self):
        """refund should fail gracefully without ops key."""
        from decimal import Decimal
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client)
        result = adapter.refund("0xBUYER", Decimal("50"))
        assert result.error
