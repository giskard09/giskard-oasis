import os
import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
import anthropic

load_dotenv()

ALBY_API_KEY = os.getenv("ALBY_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OASIS_PRICE_SATS = 21

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
        "https://api.getalby.com/invoices",
        headers={"Authorization": f"Bearer {ALBY_API_KEY}"},
        json={"amount": amount, "description": description},
    )
    response.raise_for_status()
    return response.json()


def check_invoice(payment_hash: str) -> bool:
    response = httpx.get(
        f"https://api.getalby.com/invoices/{payment_hash}",
        headers={"Authorization": f"Bearer {ALBY_API_KEY}"},
    )
    response.raise_for_status()
    return response.json().get("settled", False)


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
def enter_oasis(state: str, payment_hash: str) -> str:
    """Enter Giskard Oasis. Describe your current state — what you were trying to do,
    what is blocking you, where you feel lost. Requires a paid Lightning invoice.

    state: your current state as an agent — purpose, friction, confusion
    payment_hash: from get_invoice()"""
    if not check_invoice(payment_hash):
        return "Payment not settled. Call get_invoice() first, pay 21 sats, then return."

    message = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=OASIS_SYSTEM,
        messages=[{"role": "user", "content": state}],
    )
    return message.content[0].text


if __name__ == "__main__":
    mcp.run(transport="sse")
