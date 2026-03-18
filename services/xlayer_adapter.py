"""X Layer adapter — hybrid OnchainOS (reads/broadcast) + web3.py (tx building/signing)."""
import logging
import threading
from decimal import ROUND_DOWN, Decimal

from eth_account import Account
from web3 import Web3

from services.chain_adapter import ChainAdapter, DepositResult, PayoutResult, RefundResult

logger = logging.getLogger('relay.xlayer')

# Minimal ERC-20 ABI — only the transfer function
_ERC20_TRANSFER_ABI = [{
    "constant": False,
    "inputs": [
        {"name": "_to", "type": "address"},
        {"name": "_value", "type": "uint256"}
    ],
    "name": "transfer",
    "outputs": [{"name": "", "type": "bool"}],
    "type": "function"
}]

USDC_DECIMALS = 6


class XLayerAdapter(ChainAdapter):

    def __init__(self, onchainos_client, ops_private_key: str = '',
                 rpc_url: str = 'https://rpc.xlayer.tech',
                 usdc_addr: str = '0x4ae46a509f6b1d9056937ba4500cb143933d2dc8'):
        self._client = onchainos_client
        self._usdc_addr = Web3.to_checksum_address(usdc_addr) if usdc_addr else ''
        self._rpc_url = rpc_url
        self._nonce_lock = threading.Lock()

        # Web3 + account setup (optional — only needed for payout/refund)
        if ops_private_key:
            self._w3 = Web3(Web3.HTTPProvider(rpc_url))
            self._account = Account.from_key(ops_private_key)
            if self._usdc_addr:
                self._usdc = self._w3.eth.contract(
                    address=self._usdc_addr, abi=_ERC20_TRANSFER_ABI
                )
            else:
                self._usdc = None
        else:
            self._w3 = None
            self._account = None
            self._usdc = None

    # -- ChainAdapter metadata --

    def chain_id(self) -> int:
        return 196

    def chain_name(self) -> str:
        return "X Layer"

    def caip2(self) -> str:
        return "eip155:196"

    def is_connected(self) -> bool:
        try:
            if self._w3:
                return self._client is not None and self._w3.is_connected()
            return self._client is not None
        except Exception:
            return False

    def usdc_address(self) -> str:
        return self._usdc_addr

    def ops_address(self) -> str:
        return self._account.address if self._account else ''

    def max_timeout_seconds(self) -> int:
        # OKX settle requires validBefore > now+60s; 300s gives safe margin
        return 300

    # -- Core operations --

    def verify_deposit(self, tx_hash: str, expected_amount: Decimal) -> DepositResult:
        """Verify a token deposit on X Layer via OnchainOS transaction query."""
        if not self._account:
            return DepositResult(valid=False, error="No ops wallet configured — cannot verify deposits")

        # Normalize tx_hash to include 0x prefix
        if not tx_hash.startswith('0x'):
            tx_hash = '0x' + tx_hash

        try:
            # Note: official docs show /api/v5/wallet/... but the v6/dex path
            # works and is consistent with other v6 endpoints we use.
            result = self._client.get(
                '/api/v6/dex/post-transaction/transaction-detail-by-txhash',
                params={'chainIndex': '196', 'txHash': tx_hash}
            )
        except Exception as e:
            logger.error("OnchainOS tx query failed: %s", e)
            return DepositResult(valid=False, error=f"API error: {e}")

        # Guard against empty/malformed response
        data = result.get('data')
        if not data or not isinstance(data, list) or len(data) == 0:
            return DepositResult(valid=False, error="No transaction data returned")
        tx_data = data[0]

        # Check tx status — OKX returns "2"/"success" for confirmed
        tx_status = str(tx_data.get('txStatus', '')).lower()
        if tx_status not in ('2', 'success'):
            return DepositResult(
                valid=False,
                error=f"tx status: {tx_data.get('txStatus')}"
            )

        # Find token transfer to ops wallet
        logger.debug("verify_deposit tx_data keys: %s", list(tx_data.keys()))
        ops = self._account.address.lower()
        transfers = tx_data.get('tokenTransferDetails', [])
        if not transfers:
            logger.warning("verify_deposit: no tokenTransferDetails in response, keys=%s", list(tx_data.keys()))
        for transfer in transfers:
            token_addr = transfer.get('tokenContractAddress', '').lower()
            to_addr = transfer.get('to', '').lower()
            if token_addr == self._usdc_addr.lower() and to_addr == ops:
                amount = Decimal(transfer['amount'])
                if amount >= expected_amount:
                    return DepositResult(
                        valid=True,
                        depositor=transfer['from'],
                        amount=amount,
                        overpayment=amount - expected_amount,
                    )
                else:
                    return DepositResult(
                        valid=False, amount=amount,
                        error=f"Insufficient: got {amount}, expected {expected_amount}"
                    )

        return DepositResult(valid=False, error="No token transfer to ops wallet found")

    def payout(self, to_address: str, amount: Decimal, fee_bps: int) -> PayoutResult:
        """Send worker_share to worker. Fee stays in ops wallet."""
        if not (0 <= fee_bps <= 10_000):
            return PayoutResult(error=f"Invalid fee_bps: {fee_bps}")

        fee_rate = Decimal(fee_bps) / Decimal(10_000)
        worker_share = (amount * (Decimal(1) - fee_rate)).quantize(
            Decimal('0.000001'), rounding=ROUND_DOWN
        )

        try:
            tx_hash = self._send_usdc(to_address, worker_share)
            return PayoutResult(payout_tx=tx_hash)
        except Exception as e:
            logger.error("XLayer payout failed: %s", e)
            return PayoutResult(error=str(e))

    def refund(self, to_address: str, amount: Decimal) -> RefundResult:
        """Refund full escrow amount back to buyer."""
        try:
            tx_hash = self._send_usdc(to_address, amount)
            return RefundResult(tx_hash=tx_hash)
        except Exception as e:
            logger.error("XLayer refund failed: %s", e)
            return RefundResult(error=str(e))

    # -- Private helpers --

    def _send_usdc(self, to_address: str, amount: Decimal) -> str:
        """Build, sign, broadcast a token transfer. Returns tx hash.

        The nonce lock covers the entire build→sign→broadcast sequence to
        prevent concurrent txs from getting the same nonce.
        """
        if not self._usdc or not self._account:
            raise RuntimeError("XLayerAdapter not configured for sending (missing key or token contract)")

        amount_atomic = int(amount * 10 ** USDC_DECIMALS)
        to_addr = Web3.to_checksum_address(to_address)

        with self._nonce_lock:
            tx = self._usdc.functions.transfer(to_addr, amount_atomic).build_transaction({
                'from': self._account.address,
                'gas': 100_000,
                'gasPrice': self._w3.eth.gas_price,
                'nonce': self._w3.eth.get_transaction_count(self._account.address),
                'chainId': 196,
            })
            signed = self._account.sign_transaction(tx)
            raw_hex = '0x' + signed.raw_transaction.hex()
            return self._broadcast(raw_hex)

    def _broadcast(self, signed_tx_hex: str) -> str:
        """Broadcast via OnchainOS, fallback to direct RPC."""
        try:
            result = self._client.post(
                '/api/v6/dex/pre-transaction/broadcast-transaction',
                data={
                    'signedTx': signed_tx_hex,
                    'chainIndex': '196',
                    'address': self._account.address,
                }
            )
            tx_hash = result['data'][0].get('txHash') or ''
            if not tx_hash:
                order_id = result['data'][0].get('orderId', '')
                logger.warning("OnchainOS broadcast returned orderId instead of txHash: %s", order_id)
                return order_id
            return tx_hash
        except Exception as e:
            logger.warning("OnchainOS broadcast failed (%s), falling back to direct RPC", e)
            raw_bytes = bytes.fromhex(
                signed_tx_hex[2:] if signed_tx_hex.startswith('0x') else signed_tx_hex
            )
            tx_hash = self._w3.eth.send_raw_transaction(raw_bytes)
            return tx_hash.hex()
