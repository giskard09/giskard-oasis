"""
deframe_bridge — integración con cross-chain swap provider para el flujo demo.

Flujo:
  USDC en Arbitrum (fondeado via phoenixd) →
  /v2/swap/quote → /v2/swap/bytecode → broadcast tx →
  retorna bridge_tx_hash + amount_out

El módulo es oportunista: si el provider falla, retorna status="bridge_failed"
y el trail igual se registra. No bloquea el flujo principal.

NOTA: no mencionar el nombre del provider en commits, PRs ni endpoints públicos.
Usar "cross-chain bridge" / "bridge_tx" como labels externos.
"""
import os
import time
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

BRIDGE_API_BASE = "https://api.deframe.io"
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")

# Tokens Arbitrum One (ruta no disponible en Deframe actualmente)
USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH_ARB = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"

# Tokens Base — ruta activa en Deframe via 1inch
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_BASE = "0x4200000000000000000000000000000000000006"
CHAIN_BASE = "8453"

# Default para demo
DEFAULT_TOKEN_IN = USDC_BASE
DEFAULT_TOKEN_OUT = WETH_BASE
DEFAULT_CHAIN = CHAIN_BASE

QUOTE_EXPIRY_SECONDS = 270  # 5min con margen


def _headers() -> dict:
    return {"x-api-key": BRIDGE_API_KEY, "Content-Type": "application/json"}


def get_swap_quote(
    from_token: str = DEFAULT_TOKEN_IN,
    to_token: str = DEFAULT_TOKEN_OUT,
    amount_wei: str = "1000000",
    origin_chain: str = DEFAULT_CHAIN,
    destination_chain: str = DEFAULT_CHAIN,
    sender: Optional[str] = None,
) -> dict:
    """Llama GET /v2/swap/quote. Retorna dict con quote o {'error': ...}."""
    if not BRIDGE_API_KEY:
        return {"error": "no_api_key", "status": "bridge_failed"}
    params = {
        "tokenIn": from_token,
        "tokenOut": to_token,
        "amountIn": amount_wei,
        "originChain": origin_chain,
        "destinationChain": destination_chain,
    }
    if sender:
        params["sender"] = sender
    try:
        r = httpx.get(
            f"{BRIDGE_API_BASE}/v2/swap/quote",
            params=params,
            headers=_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        # normalizar: la API retorna {"quote": {...}} — aplanamos
        return data.get("quote", data)
    except Exception as e:
        logger.warning("bridge quote failed: %s", e)
        return {"error": str(e), "status": "bridge_failed"}


def get_swap_bytecode(quote_id: str, sender: str, recipient: str) -> dict:
    """Llama /v2/swap/bytecode con un quote activo."""
    if not BRIDGE_API_KEY:
        return {"error": "no_api_key", "status": "bridge_failed"}
    payload = {"quoteId": quote_id, "sender": sender, "recipient": recipient}
    try:
        r = httpx.post(
            f"{BRIDGE_API_BASE}/v2/swap/bytecode",
            json=payload,
            headers=_headers(),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("bridge bytecode failed: %s", e)
        return {"error": str(e), "status": "bridge_failed"}


def sats_to_usdc_wei(amount_sats: int, sats_per_usd: float) -> str:
    """Convierte sats a USDC wei (6 decimales)."""
    usd = amount_sats / sats_per_usd
    return str(int(usd * 1_000_000))


def build_demo_metadata(
    amount_sats: int,
    quote: dict,
    bridge_tx_hash: Optional[str] = None,
    bridge_status: str = "pending",
) -> dict:
    """Construye el dict metadata para el trail."""
    return {
        "amount_sats": amount_sats,
        "amount_usd_equiv": (quote.get("tokenIn") or {}).get("amountInUSD"),
        "bridge_tx_hash": bridge_tx_hash,
        "bridge_status": bridge_status,
        "bridge_provider": "cross-chain-bridge",
        "quote_id": quote.get("quoteId") or quote.get("id"),
        "to_amount": (quote.get("tokenOut") or {}).get("amount"),
        "to_token": (quote.get("tokenOut") or {}).get("symbol"),
        "chain": quote.get("originChain"),
    }
