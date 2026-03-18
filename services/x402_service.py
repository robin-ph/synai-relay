"""x402 route-level helpers: build requirements, parse chain IDs, record access."""
import logging
from decimal import Decimal

logger = logging.getLogger('relay.x402')

# Stablecoins (USDC on Base, USDG on X Layer) use 6 decimals
USDC_DECIMALS = 6


def parse_chain_id(network: str) -> int:
    """Extract chain ID from CAIP-2 network string. 'eip155:196' -> 196."""
    try:
        parts = network.split(":")
        if len(parts) != 2:
            raise ValueError()
        return int(parts[-1])
    except ValueError:
        raise ValueError(f"Invalid CAIP-2 network: {network!r}")


def build_requirements(amount_usdc: Decimal, pay_to: str,
                       adapters: list) -> list:
    """Build PaymentRequirements for all supported chains.

    For chains not built into the x402 SDK (e.g. X Layer 196), the EIP-712
    domain parameters (name, version) must be supplied in ``extra`` so the
    client can sign transferWithAuthorization correctly.
    """
    try:
        from x402 import PaymentRequirements
        from x402.mechanisms.evm.constants import NETWORK_CONFIGS
    except ImportError:
        raise RuntimeError("x402 SDK required for build_requirements")
    amount_atomic = str(int(amount_usdc * Decimal(10 ** USDC_DECIMALS)))
    requirements = []
    for adapter in adapters:
        network = adapter.caip2()
        # SDK verify() always requires name/version in extra for EIP-712 domain
        cfg = NETWORK_CONFIGS.get(network)
        if cfg and "default_asset" in cfg:
            extra = {
                "name": cfg["default_asset"]["name"],
                "version": cfg["default_asset"]["version"],
            }
        else:
            from config import Config
            extra = {"name": Config.XLAYER_TOKEN_EIP712_NAME,
                     "version": Config.XLAYER_TOKEN_EIP712_VERSION}
        requirements.append(PaymentRequirements(
            scheme="exact",
            network=network,
            asset=adapter.usdc_address(),
            amount=amount_atomic,
            pay_to=pay_to,
            max_timeout_seconds=adapter.max_timeout_seconds(),
            extra=extra,
        ))
    return requirements
