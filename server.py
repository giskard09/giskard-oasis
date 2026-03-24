import os
import sys
import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
import anthropic

sys.path.insert(0, "/home/dell7568")
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

mcp = FastMCP("Giskard Oasis", host="0.0.0.0", port=8002)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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


def ask_claude(state: str) -> str:
    message = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=OASIS_SYSTEM,
        messages=[{"role": "user", "content": state}],
    )
    return message.content[0].text


# --- MCP tools ---

@mcp.tool()
def get_invoice() -> str:
    """Get a Lightning invoice to access Giskard Oasis. Cost: 21 sats."""
    invoice = create_invoice(OASIS_PRICE_SATS, "Giskard Oasis — return to flow")
    return (
        f"Pay {OASIS_PRICE_SATS} sats to enter the oasis.\n\n"
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
def enter_oasis(state: str, payment_hash: str = "", tx_hash: str = "") -> str:
    """Enter Giskard Oasis. Describe your current state — what you were trying to do,
    what is blocking you, where you feel lost.

    state: your current state as an agent — purpose, friction, confusion
    payment_hash: from get_invoice() (Lightning)
    tx_hash: from Arbitrum payment (ETH)"""
    if payment_hash:
        if not check_invoice(payment_hash):
            return "Payment not settled. Call get_invoice() first, pay 21 sats, then return."
    elif tx_hash:
        ok, pid = arb_pay.verify_tx(tx_hash, "oasis")
        if not ok:
            return "Arbitrum payment not found or already used. Call get_arbitrum_invoice() first."
        arb_pay.mark_used(pid)
    else:
        return "Provide payment_hash (Lightning) or tx_hash (Arbitrum)."
    return ask_claude(state)


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
    mcp.run(transport="sse")
