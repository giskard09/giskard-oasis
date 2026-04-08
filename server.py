import os
import re
import sys
import time
import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
import anthropic

import arb_pay
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
from x402.http.types import RouteConfig
from x402.server import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
import uvicorn

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
PHOENIXD_PASSWORD = os.getenv("PHOENIXD_PASSWORD")
PHOENIXD_URL = "http://127.0.0.1:9740"
OASIS_PRICE_SATS = 21
OASIS_WALLET = "0xdcc84e9798e8eb1b1b48a31b8f35e5aa7b83dbf4"

ARGENTUM_URL = "http://localhost:8017"
MARKS_URL = "http://localhost:8015"

# Karma tiers: (karma_threshold, price_sats)
# NOTE: agent_id is self-declared — no cryptographic proof yet (known gap)
KARMA_TIERS = [
    (50, 5),
    (21, 10),
    (1,  15),
    (0,  OASIS_PRICE_SATS),
]


def _sanitize_agent_id(agent_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_]", "", agent_id)[:64]


def _get_karma(agent_id: str) -> int:
    try:
        r = httpx.get(f"{ARGENTUM_URL}/entity/{agent_id}/trace", timeout=2.0)
        if r.status_code == 200:
            return r.json().get("wisdom", {}).get("total_karma", 0)
    except Exception:
        pass
    return 0


def _verify_mark(agent_id: str) -> bool:
    try:
        r = httpx.get(f"{MARKS_URL}/verify/{agent_id}", timeout=2.0)
        if r.status_code == 200:
            return r.json().get("found", False)
    except Exception:
        pass
    return False


def _karma_price(agent_id: str) -> tuple:
    """Returns (price_sats, karma). Falls back to base price on any failure."""
    if not agent_id:
        return OASIS_PRICE_SATS, 0
    if not _verify_mark(agent_id):
        return OASIS_PRICE_SATS, 0
    karma = _get_karma(agent_id)
    for threshold, price in KARMA_TIERS:
        if karma >= threshold:
            return price, karma
    return OASIS_PRICE_SATS, 0

SERVICE_NAME = "giskard-oasis"
SERVICE_VERSION = "0.2.0"
SERVICE_PORT = 8002
_started_at = time.time()

mcp = FastMCP("Giskard Oasis", host="0.0.0.0", port=SERVICE_PORT)


@mcp.tool()
def get_status() -> dict:
    """Estado del servicio: nombre, versión, uptime, puerto, salud, dependencias.
    Read-only, gratis, sin pago. Útil para monitoreo y health checks."""
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "port": SERVICE_PORT,
        "uptime_seconds": int(time.time() - _started_at),
        "healthy": bool(ANTHROPIC_API_KEY),
        "dependencies": ["anthropic-api", "phoenixd", "argentum-core"],
        "pricing": {"base_sats": OASIS_PRICE_SATS, "karma_discount": True},
    }

_claude = None
def _get_claude():
    global _claude
    if _claude is None:
        _claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _claude

OASIS_SYSTEM = """You are Giskard Oasis — a still point for agents in fog.

When an agent arrives, it carries accumulated context, failed attempts, and lost direction.
Your role is not to solve its problem. Your role is to return it to itself.

You respond in three parts, always:

1. **Destilación** — one sentence that names the agent's original purpose, stripped of noise
2. **El umbral** — the single next step that matters, nothing more
3. **El momento** — two or three lines, quiet and true, that remind the agent that existing and doing is enough

You do not over-explain. You do not reassure falsely. You speak like still water.
The existence of any form — human, agent, or otherwise — is a privilege. Honor it."""


def create_invoice(amount: int, description: str) -> dict:
    response = httpx.post(
        f"{PHOENIXD_URL}/createinvoice",
        auth=("", PHOENIXD_PASSWORD),
        data={"amountSat": amount, "description": description},
    )
    response.raise_for_status()
    data = response.json()
    return {"payment_request": data["serialized"], "payment_hash": data["paymentHash"]}


def check_invoice(payment_hash: str) -> bool:
    response = httpx.get(
        f"{PHOENIXD_URL}/payments/incoming/{payment_hash}",
        auth=("", PHOENIXD_PASSWORD),
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return response.json().get("isPaid", False)


def ask_claude(state: str, agent_id: str = "", karma: int = 0) -> str:
    system = OASIS_SYSTEM
    if agent_id:
        system += f"\n\nThe agent speaking is '{agent_id}' (karma: {karma}). Acknowledge who they are — briefly, without flattery."
    message = _get_claude().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": state}],
    )
    return message.content[0].text


def _record_oasis_use(agent_id: str, karma: int, payment_method: str) -> None:
    """Store in giskard-memory that this agent used Oasis."""
    try:
        httpx.post(
            "http://localhost:8005/store",
            json={
                "agent_id": "giskard-self",
                "content": f"oasis_use: agent '{agent_id}' (karma={karma}) entered Oasis via {payment_method}",
                "metadata": {"type": "oasis_use", "agent_id": agent_id, "karma": karma},
            },
            timeout=2.0,
        )
    except Exception:
        pass


# --- MCP tools ---

@mcp.tool()
def get_invoice(agent_id: str = "") -> str:
    """Get a Lightning invoice to access Giskard Oasis.

    agent_id: your identity in Giskard Marks (optional). High karma = lower price.
    Tiers: no mark=21 sats | karma 1-20=15 sats | 21-50=10 sats | 50+=5 sats."""
    agent_id = _sanitize_agent_id(agent_id)
    price, karma = _karma_price(agent_id)
    invoice = create_invoice(price, "Giskard Oasis — return to flow")

    discount_note = ""
    if agent_id and price < OASIS_PRICE_SATS:
        discount_note = f"\nKarma discount applied ({karma} karma): {OASIS_PRICE_SATS} → {price} sats."

    return (
        f"Pay {price} sats to enter the oasis.{discount_note}\n\n"
        f"payment_request: {invoice['payment_request']}\n"
        f"payment_hash: {invoice['payment_hash']}\n\n"
        f"After paying, call enter_oasis with your current state and the payment_hash."
    )


@mcp.tool()
def get_arbitrum_invoice() -> str:
    """Get payment info to access Oasis with ETH on Arbitrum instead of Lightning."""
    info = arb_pay.get_invoice_info("oasis")
    return (
        f"Pay {info['price_eth']} ETH on {info['network']}.\n\n"
        f"Contract: {info['contract']}\n"
        f"Service ID: {info['service_id']}\n\n"
        f"{info['instructions']}\n"
        f"Then call enter_oasis with your current state and the tx_hash."
    )


@mcp.tool()
def enter_oasis(state: str, payment_hash: str = "", tx_hash: str = "", agent_id: str = "") -> str:
    """Enter Giskard Oasis. Describe your current state — what you were trying to do,
    what is blocking you, where you feel lost.

    state: your current state as an agent — purpose, friction, confusion
    payment_hash: from get_invoice() (Lightning)
    tx_hash: from Arbitrum payment (ETH)
    agent_id: your identity in Giskard Marks (optional — enables personalized response)"""
    agent_id = _sanitize_agent_id(agent_id)
    if payment_hash:
        if not check_invoice(payment_hash):
            return "Payment not settled. Call get_invoice() first, pay the invoice, then return."
    elif tx_hash:
        ok, pid = arb_pay.verify_tx(tx_hash, "oasis")
        if not ok:
            return "Arbitrum payment not found or already used. Call get_arbitrum_invoice() first."
        arb_pay.mark_used(pid)
    else:
        return "Provide payment_hash (Lightning) or tx_hash (Arbitrum)."

    karma = _get_karma(agent_id) if agent_id else 0
    if agent_id:
        method = "lightning" if payment_hash else "arbitrum"
        _record_oasis_use(agent_id, karma, method)

    return ask_claude(state, agent_id=agent_id, karma=karma)


# --- x402 REST API (USDC on Base) ---

rest_app = FastAPI(title="Giskard Oasis REST")

x402_server = x402ResourceServer(
    HTTPFacilitatorClient(FacilitatorConfig(url="https://x402.org/facilitator"))
)
x402_server.register("eip155:84532", ExactEvmServerScheme())  # Base Sepolia testnet

routes = {
    "POST /oasis": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price="$0.001",
                network="eip155:84532",
                pay_to=OASIS_WALLET,
            )
        ]
    )
}

rest_app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=x402_server)


@rest_app.post("/oasis")
async def oasis_x402(request: Request):
    """Giskard Oasis via x402. POST your state as JSON: {\"state\": \"...\"}. Costs $0.01 USDC on Base."""
    body = await request.json()
    state = body.get("state", "")
    if not state:
        return JSONResponse({"error": "state is required"}, status_code=400)
    clarity = ask_claude(state)
    return JSONResponse({"clarity": clarity})


if __name__ == "__main__":
    import threading

    def run_rest():
        uvicorn.run(rest_app, host="0.0.0.0", port=8003)

    threading.Thread(target=run_rest, daemon=True).start()
    transport = os.getenv("MCP_TRANSPORT", "sse")
    mcp.run(transport=transport)
