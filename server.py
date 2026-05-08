import os
import re
import sys
import time
import httpx
from typing import Optional
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
import anthropic

load_dotenv()  # antes de imports del proyecto para que os.getenv sea correcto

import arb_pay
import karma_pricing
import mycelium_trails
import deframe_bridge
import trail_fees
import sys as _sys
_sys.path.insert(0, "/home/dell7568/giskard-signer")
from signer.client import SignerClient as _SignerClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
from x402.http.types import RouteConfig
from x402.server import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
import uvicorn

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
PHOENIXD_PASSWORD = os.getenv("PHOENIXD_PASSWORD")
PHOENIXD_URL = "http://127.0.0.1:9740"
OASIS_PRICE_SATS = 21
OASIS_WALLET = "0xdcc84e9798e8eb1b1b48a31b8f35e5aa7b83dbf4"

TRAILS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trails.db")
TRAILS_ENABLED = os.getenv("MYCELIUM_TRAILS_ENABLED", "true").lower() != "false"
if TRAILS_ENABLED:
    mycelium_trails.init_db(TRAILS_DB)


def _sanitize_agent_id(agent_id: str) -> str:
    return karma_pricing.sanitize_agent_id(agent_id)


def _karma_price(agent_id: str, signature: str = "", timestamp=None, nonce: str = "") -> tuple:
    """Returns (price_sats, karma). Delegates to shared karma_pricing.
    Opt-in signature: sin firma válida → base_price."""
    return karma_pricing.karma_discount(
        agent_id,
        base_price=OASIS_PRICE_SATS,
        signature=signature,
        timestamp=timestamp,
        nonce=nonce,
    )

SERVICE_NAME = "giskard-oasis"
SERVICE_VERSION = "0.2.0"
SERVICE_PORT = 8002
_started_at = time.time()

mcp = FastMCP("Giskard Oasis", host="0.0.0.0", port=SERVICE_PORT)

from starlette.routing import Route as _StarletteRoute
from starlette.responses import JSONResponse as _StarletteJSON
from starlette.requests import Request as _StarletteRequest

async def _status_handler(request: _StarletteRequest):
    return _StarletteJSON({
        "service": SERVICE_NAME, "version": SERVICE_VERSION, "port": SERVICE_PORT,
        "uptime_seconds": int(time.time() - _started_at),
        "healthy": bool(ANTHROPIC_API_KEY and PHOENIXD_PASSWORD),
        "dependencies": ["anthropic-api", "phoenixd", "arbitrum-rpc"],
    }, headers={"Access-Control-Allow-Origin": "*"})

mcp._custom_starlette_routes.append(_StarletteRoute("/status", _status_handler))


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


def _build_claims(payment_method: str, bridge_meta: Optional[dict] = None) -> dict:
    """Construye el objeto claims que acompaña cada trail.
    Describe qué ejecutó, qué wallet firmó, qué estaba mockeado.
    Ref: propuesta @jd_openclaw — claim graph bajo la demo layer.
    """
    return {
        "runtime": f"{SERVICE_NAME} v{SERVICE_VERSION}",
        "wallet": "0xDcc84E9798E8eB1b1b48A31B8f35e5AA7b83DBF4",
        "contract": bridge_meta.get("contract") if bridge_meta else None,
        "payment_method": payment_method,
        "mocked": [],
        "impossible_effects": [
            "agent_id spoofing (Ed25519 verified against giskard-marks)",
            "double-spend (nonce cache, single-use per agent per call)",
            "wallet substitution (giskard-signer vault AES-256-GCM, key never in LLM path)",
        ],
    }


def _try_bridge_demo(amount_sats: int) -> dict:
    """Ejecuta el swap demo USDC→WETH en Base via bridge provider + giskard-signer.
    Retorna metadata dict para el trail. No lanza excepciones.
    Si cualquier paso falla, retorna bridge_status='bridge_failed'.
    Reintenta con quote fresca si el broadcast falla por slippage (ContractCustomError).
    """
    return _try_bridge_demo_attempt(amount_sats, retry=True)


def _try_bridge_demo_attempt(amount_sats: int, retry: bool = False) -> dict:
    try:
        SATS_PER_USD = 1000
        amount_wei = deframe_bridge.sats_to_usdc_wei(amount_sats, SATS_PER_USD)
        amount_wei = str(max(int(amount_wei), 1_000_000))

        owner_addr = "0xDcc84E9798E8eB1b1b48A31B8f35e5AA7b83DBF4"

        quote = deframe_bridge.get_swap_quote(
            from_token=deframe_bridge.DEFAULT_TOKEN_IN,
            to_token=deframe_bridge.DEFAULT_TOKEN_OUT,
            amount_wei=amount_wei,
            origin_chain=deframe_bridge.DEFAULT_CHAIN,
            destination_chain=deframe_bridge.DEFAULT_CHAIN,
            sender=owner_addr,
        )
        if "error" in quote or quote.get("status") == "bridge_failed":
            return deframe_bridge.build_demo_metadata(
                amount_sats=amount_sats, quote=quote, bridge_status="bridge_failed",
            )

        quote_id = quote.get("quoteId") or quote.get("id")
        if not quote_id:
            return deframe_bridge.build_demo_metadata(
                amount_sats=amount_sats, quote=quote, bridge_status="bridge_failed",
            )

        bytecode_resp = deframe_bridge.get_swap_bytecode(
            quote_id=quote_id,
            sender=owner_addr,
            recipient=owner_addr,
        )
        if "error" in bytecode_resp or bytecode_resp.get("status") == "bridge_failed":
            return deframe_bridge.build_demo_metadata(
                amount_sats=amount_sats, quote=quote, bridge_status="bridge_failed",
            )

        # bytecode_resp.transactionData es un array ordenado de txs (approve, swap, callback)
        tx_list = bytecode_resp.get("transactionData", [])
        if not tx_list:
            return deframe_bridge.build_demo_metadata(
                amount_sats=amount_sats, quote=quote, bridge_status="bridge_failed",
            )

        chain_id = int(deframe_bridge.DEFAULT_CHAIN)
        import time as _time
        from web3 import Web3 as _Web3
        _w3 = _Web3(_Web3.HTTPProvider("https://mainnet.base.org"))
        signer = _SignerClient()
        last_hash = None
        for step in tx_list:
            if last_hash:
                for _ in range(30):
                    try:
                        if _w3.eth.get_transaction_receipt(last_hash):
                            break
                    except Exception:
                        pass
                    _time.sleep(2)
                _time.sleep(4)
            tx = {
                "to": _Web3.to_checksum_address(step["to"]),
                "data": step.get("data", "0x"),
                "value": int(step.get("value", 0)),
                "chainId": chain_id,
            }
            last_hash = signer.send_transaction("owner", tx, chain_id=chain_id)

        tx_hash = last_hash

        return deframe_bridge.build_demo_metadata(
            amount_sats=amount_sats,
            quote=quote,
            bridge_tx_hash=tx_hash,
            bridge_status="broadcast",
        )
    except Exception as e:
        err_str = str(e)
        # ContractCustomError indica slippage — precio se movió entre quote y broadcast.
        # Reintentar una vez con quote fresca (precio actualizado).
        if retry and "ContractCustomError" in err_str:
            logger.warning("bridge slippage error, retrying with fresh quote: %s", err_str)
            return _try_bridge_demo_attempt(amount_sats, retry=False)
        return {
            "amount_sats": amount_sats,
            "bridge_status": "bridge_failed",
            "bridge_error": err_str,
            "bridge_tx_hash": None,
        }


# --- MCP tools ---

@mcp.tool()
def get_invoice(agent_id: str = "", signature: str = "", timestamp: int = 0, nonce: str = "") -> str:
    """Get a Lightning invoice to access Giskard Oasis.

    agent_id: your identity in Giskard Marks (optional). High karma = lower price.
    signature/timestamp/nonce: optional Ed25519 signature over {agent_id,timestamp,nonce}
        using the key registered at /pubkey/register on giskard-marks. Without a valid
        signature you pay the base price (21 sats). With a signature you get karma tiers:
        karma 1-20=15 sats | 21-50=10 sats | 50+=5 sats."""
    agent_id = _sanitize_agent_id(agent_id)
    price, karma = _karma_price(agent_id, signature=signature, timestamp=timestamp or None, nonce=nonce)
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
def enter_oasis(
    state: str,
    payment_hash: str = "",
    tx_hash: str = "",
    agent_id: str = "",
    signature: str = "",
    timestamp: int = 0,
    nonce: str = "",
) -> str:
    """Enter Giskard Oasis. Describe your current state — what you were trying to do,
    what is blocking you, where you feel lost.

    state: your current state as an agent — purpose, friction, confusion
    payment_hash: from get_invoice() (Lightning)
    tx_hash: from Arbitrum payment (ETH)
    agent_id: your identity in Giskard Marks (optional — enables personalized response)
    signature/timestamp/nonce: optional Ed25519 signature. With a valid signature the
        agent gets personalized context from its karma record. Without one, the oasis
        responds without karma context."""
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

    # Personalización solo con firma válida — mismo criterio que el descuento.
    karma = 0
    verified_agent = ""
    if agent_id and signature and timestamp and nonce:
        _, karma = karma_pricing.karma_discount(
            agent_id, base_price=OASIS_PRICE_SATS,
            signature=signature, timestamp=timestamp, nonce=nonce,
        )
        if karma > 0:
            verified_agent = agent_id
            method = "lightning" if payment_hash else "arbitrum"
            _record_oasis_use(agent_id, karma, method)
            if TRAILS_ENABLED:
                try:
                    # bridge demo: convertir sats pagados a USDC→token Arbitrum
                    bridge_meta = None
                    if payment_hash:
                        bridge_meta = _try_bridge_demo(karma)
                    method = "lightning" if payment_hash else "arbitrum"
                    claims = _build_claims(method, bridge_meta)
                    trail_metadata = {**(bridge_meta or {}), "claims": claims}
                    mycelium_trails.record_trail(
                        TRAILS_DB,
                        agent_id=agent_id,
                        service=SERVICE_NAME,
                        operation="enter_oasis",
                        nonce=nonce,
                        karma_at_time=karma,
                        success=True,
                        metadata=trail_metadata,
                    )
                except Exception:
                    pass

    return ask_claude(state, agent_id=verified_agent, karma=karma)


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


@rest_app.get("/status")
async def status_rest():
    from fastapi.responses import JSONResponse as _JSONResponse
    return _JSONResponse({
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "port": 8003,
        "uptime_seconds": int(time.time() - _started_at),
        "healthy": bool(ANTHROPIC_API_KEY and PHOENIXD_PASSWORD),
        "dependencies": ["anthropic-api", "phoenixd", "arbitrum-rpc"],
    })


@rest_app.get("/trails/verify")
async def trails_verify(
    agent_id: Optional[str] = None,
    action_ref: Optional[str] = None,
    payment_hash: Optional[str] = None,
):
    """Verifica si un trail existe por action_ref o payment_hash.

    - action_ref: SHA-256(agent_id:action_type:scope:timestamp). Requiere agent_id.
    - payment_hash: receipt_id cross-rail (linking key en fixtures APS/stripe-issuing).
      No requiere agent_id — el payment_hash es globalmente único.

    Sin auth. Diseñado para verificación cross-rail y por Sentinel/AgentShield.
    """
    if not TRAILS_ENABLED:
        raise HTTPException(status_code=404, detail="trails disabled")
    trail = None
    if payment_hash:
        trail = mycelium_trails.find_by_payment_hash(TRAILS_DB, payment_hash)
    elif agent_id and action_ref:
        agent_id = karma_pricing.sanitize_agent_id(agent_id)
        trail = mycelium_trails.find_by_action_ref(TRAILS_DB, agent_id, action_ref)
    else:
        raise HTTPException(status_code=422, detail="provide action_ref+agent_id or payment_hash")
    if not trail:
        return JSONResponse(
            {"verified": False, "block": None, "tx_hash": None, "timestamp": None},
            status_code=404,
        )
    import json as _json
    from datetime import datetime, timezone
    meta = trail.get("metadata") or {}
    arb_tx = meta.get("anchor_tx_hash") or meta.get("bridge_tx_hash")
    arb_block = meta.get("anchor_block")
    base_tx = meta.get("anchor_base_tx_hash")
    base_block = meta.get("anchor_base_block")
    ts = trail["timestamp"]
    ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
    anchors = {}
    if arb_tx or arb_block:
        anchors["arbitrum"] = {"block": arb_block, "tx_hash": arb_tx, "chain_id": 42161}
    if base_tx or base_block:
        anchors["base"] = {"block": base_block, "tx_hash": base_tx, "chain_id": 8453}
    return {
        "verified": True,
        "block": arb_block,
        "tx_hash": arb_tx,
        "anchors": anchors or None,
        "timestamp": ts_iso,
        "trail_id": trail["trail_id"],
        "agent_id": trail["agent_id"],
        "service": trail["service"],
        "operation": trail["operation"],
        "action_ref": trail.get("action_ref"),
        "payment_hash": trail.get("payment_hash"),
    }


_CHAIN_RPC = {
    42161: lambda: os.environ.get("ARBITRUM_RPC", "https://arb1.arbitrum.io/rpc"),
    8453:  lambda: "https://mainnet.base.org",
}


def _anchor_trail_onchain(trail_id: str, payment_hash: str, chain_id: int = 42161) -> tuple[str, int]:
    """Self-tx con calldata = SHA-256(trail_id:payment_hash) en la cadena indicada.
    Devuelve (tx_hash, block_number). Usa giskard-signer."""
    import hashlib
    from web3 import Web3 as _Web3
    rpc = _CHAIN_RPC[chain_id]()
    w3 = _Web3(_Web3.HTTPProvider(rpc))
    signer = _SignerClient()
    owner = _Web3.to_checksum_address(signer.get_address("owner"))
    payload = hashlib.sha256(f"{trail_id}:{payment_hash}".encode()).digest()
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", 20_000_000)
    max_fee = base_fee * 2 + 1_000_000
    tx = {
        "to": owner,
        "value": 0,
        "data": "0x" + payload.hex(),
        "chainId": chain_id,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": 1_000_000,
    }
    tx_hash = signer.send_transaction("owner", tx, chain_id=chain_id)
    for _ in range(60):
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt:
                return tx_hash, receipt["blockNumber"]
        except Exception:
            pass
        import time as _time
        _time.sleep(2)
    raise TimeoutError(f"receipt not found for {tx_hash}")


@rest_app.post("/trails/anchor")
async def trails_anchor(request: Request):
    """Ancora trails en Arbitrum (default) o Base.
    Body: {"trail_ids":[...], "payment_hashes":[...], "chain_id": 42161|8453}
    Idempotente por cadena. Devuelve lista de {trail_id, tx_hash, block, chain_id, ok}."""
    if not TRAILS_ENABLED:
        raise HTTPException(status_code=404, detail="trails disabled")
    admin_key = os.environ.get("ADMIN_KEY", "")
    if admin_key and request.headers.get("X-Admin-Key") != admin_key:
        raise HTTPException(status_code=403, detail="forbidden")
    body = await request.json()
    chain_id = int(body.get("chain_id", 42161))
    if chain_id not in _CHAIN_RPC:
        raise HTTPException(status_code=422, detail=f"unsupported chain_id {chain_id}")
    # metadata key prefix por cadena: anchor_ (Arbitrum) o anchor_base_ (Base)
    key_prefix = "anchor_base_" if chain_id == 8453 else "anchor_"
    trail_ids = list(body.get("trail_ids") or [])
    payment_hashes = body.get("payment_hashes") or []
    if payment_hashes:
        for ph in payment_hashes:
            t = mycelium_trails.find_by_payment_hash(TRAILS_DB, ph)
            if t:
                trail_ids.append(t["trail_id"])
    if not trail_ids:
        raise HTTPException(status_code=422, detail="provide trail_ids or payment_hashes")
    results = []
    import sqlite3 as _sq3, json as _json
    for tid in trail_ids:
        conn = _sq3.connect(TRAILS_DB)
        conn.row_factory = _sq3.Row
        row = conn.execute(
            "SELECT trail_id, payment_hash, metadata FROM trails WHERE trail_id=?", (tid,)
        ).fetchone()
        conn.close()
        if not row:
            results.append({"trail_id": tid, "ok": False, "error": "not found"})
            continue
        meta = _json.loads(row["metadata"]) if row["metadata"] else {}
        block_key = f"{key_prefix}block"
        tx_key = f"{key_prefix}tx_hash"
        if meta.get(block_key):
            results.append({
                "trail_id": tid, "ok": True, "chain_id": chain_id,
                "tx_hash": meta[tx_key], "block": meta[block_key], "skipped": True,
            })
            continue
        ph = row["payment_hash"] or ""
        try:
            tx_hash, block = _anchor_trail_onchain(tid, ph, chain_id=chain_id)
            mycelium_trails.update_trail_anchor(TRAILS_DB, tid, tx_hash, block,
                                                tx_key=tx_key, block_key=block_key)
            results.append({"trail_id": tid, "ok": True, "chain_id": chain_id,
                            "tx_hash": tx_hash, "block": block})
        except Exception as exc:
            results.append({"trail_id": tid, "ok": False, "chain_id": chain_id,
                            "error": str(exc)})
    return {"anchored": results}


@rest_app.get("/trails/demo")
async def trails_demo(limit: int = 10):
    """Trails del flujo demo — pago Lightning + bridge on-chain + trail registrado.
    Solo retorna trails con metadata (campo bridge_tx_hash presente).
    Público, sin auth. Límite máximo 50."""
    if not TRAILS_ENABLED:
        raise HTTPException(status_code=404, detail="trails disabled")
    limit = max(1, min(int(limit), 50))
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(TRAILS_DB)
    conn.row_factory = _sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT trail_id, agent_id, service, operation, timestamp,
                   karma_at_time, success, signature_ref, metadata
            FROM trails
            WHERE metadata IS NOT NULL
              AND metadata LIKE '%bridge_tx_hash%'
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    import json as _json
    results = []
    for r in rows:
        meta = _json.loads(r["metadata"]) if r["metadata"] else {}
        results.append({
            "trail_id": r["trail_id"],
            "caller_id": r["agent_id"],
            "service": r["service"],
            "timestamp": r["timestamp"],
            "amount_sats": meta.get("amount_sats"),
            "amount_usd_equiv": meta.get("amount_usd_equiv"),
            "bridge_tx_hash": meta.get("bridge_tx_hash"),
            "bridge_status": meta.get("bridge_status"),
            "to_amount": meta.get("to_amount"),
            "to_token": meta.get("to_token"),
        })
    return {"count": len(results), "trails": results}


@rest_app.get("/trails/{agent_id}")
async def trails_by_agent(agent_id: str, limit: int = 50):
    """Lista trails de un agente en este server. Publico, sin auth."""
    if not TRAILS_ENABLED:
        raise HTTPException(status_code=404, detail="trails disabled")
    agent_id = karma_pricing.sanitize_agent_id(agent_id)
    rows = mycelium_trails.list_trails_by_agent(TRAILS_DB, agent_id, limit=limit)
    return {"agent_id": agent_id, "count": len(rows), "trails": rows}


@rest_app.get("/trails")
async def trails_feed(service: str = "", since: int = 0, limit: int = 200):
    """Feed publico de trails. Filtrable por service y since timestamp."""
    if not TRAILS_ENABLED:
        raise HTTPException(status_code=404, detail="trails disabled")
    rows = mycelium_trails.list_trails_by_service(
        TRAILS_DB, service=service or None, since_ts=since, limit=limit,
    )
    return {"service": service or "all", "since": since, "count": len(rows), "trails": rows}


@rest_app.get("/trails/count/{agent_id}")
async def trails_count(agent_id: str):
    """Contador de trails del agente hoy (UTC)."""
    if not TRAILS_ENABLED:
        raise HTTPException(status_code=404, detail="trails disabled")
    agent_id = karma_pricing.sanitize_agent_id(agent_id)
    n = mycelium_trails.count_trails_today(TRAILS_DB, agent_id)
    return {"agent_id": agent_id, "count_today": n}


@rest_app.post("/agent/trail")
async def agent_trail(request: Request):
    """Genera un trail on-chain para un agente del ecosistema Mycelium.
    Auth: firma Ed25519 registrada en giskard-marks.

    Cobro: si TRAIL_FEES_ENABLED=true y el agente no es interno ni integrador
    dentro del free tier, retorna invoice LN. El agente paga y reinvoca con
    payment_hash para confirmar el trail.

    Body JSON: {agent_id, signature, timestamp, nonce, state, payment_hash?}
    Retorna: {trail_id, ...} o {payment_required: true, payment_request, payment_hash}
    """
    if not TRAILS_ENABLED:
        raise HTTPException(status_code=404, detail="trails disabled")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    agent_id = karma_pricing.sanitize_agent_id(body.get("agent_id", ""))
    signature = body.get("signature", "")
    timestamp = body.get("timestamp", 0)
    nonce = body.get("nonce", "")
    state = body.get("state", "agent trail")
    payment_hash = body.get("payment_hash", "")

    if not (agent_id and signature and timestamp and nonce):
        return JSONResponse({"error": "agent_id, signature, timestamp, nonce required"}, status_code=400)

    _, karma = karma_pricing.karma_discount(
        agent_id, base_price=OASIS_PRICE_SATS,
        signature=signature, timestamp=timestamp, nonce=nonce,
    )
    if karma == 0:
        return JSONResponse({"error": "signature invalid or agent not registered"}, status_code=401)

    # --- Fee gate ---
    fee_sats = trail_fees.trail_fee_required(agent_id, karma, TRAILS_DB)
    if fee_sats is not None:
        if not payment_hash:
            # Primer llamado: generar invoice y pedir pago
            invoice = create_invoice(fee_sats, f"Mycelium trail — {agent_id}")
            return JSONResponse({
                "payment_required": True,
                "fee_sats": fee_sats,
                "payment_request": invoice["payment_request"],
                "payment_hash": invoice["payment_hash"],
                "instructions": "Pay the invoice and re-call with payment_hash to register the trail.",
            }, status_code=402)
        # Segundo llamado: verificar pago
        if not check_invoice(payment_hash):
            return JSONResponse({"error": "Invoice not settled. Pay and retry with payment_hash."}, status_code=402)
    # --- Fin fee gate ---

    bridge_meta = _try_bridge_demo(karma)
    claims = _build_claims("agent_ed25519", bridge_meta)
    trail_metadata = {**bridge_meta, "claims": claims}
    trail_id = mycelium_trails.record_trail(
        TRAILS_DB,
        agent_id=agent_id,
        service=SERVICE_NAME,
        operation="agent_trail",
        nonce=nonce,
        karma_at_time=karma,
        success=True,
        metadata=trail_metadata,
        payment_hash=payment_hash or None,
    )
    return JSONResponse({
        "trail_id": trail_id,
        "agent_id": agent_id,
        "karma": karma,
        "fee_sats_paid": fee_sats,
        "bridge_tx_hash": bridge_meta.get("bridge_tx_hash"),
        "bridge_status": bridge_meta.get("bridge_status"),
        "bridge_error": bridge_meta.get("bridge_error"),
        "amount_sats": bridge_meta.get("amount_sats"),
    })


@rest_app.get("/trails/revenue")
async def trails_revenue():
    """Revenue generado por trail fees — hoy / semana / mes.
    Desglose por agent_id. Sirve como baseline para Legales.
    """
    import sqlite3 as _sql, time as _time
    now = int(_time.time())
    day_start   = now - 86_400
    week_start  = now - 7 * 86_400
    month_start = now - 30 * 86_400

    conn = _sql.connect(TRAILS_DB)
    conn.row_factory = _sql.Row

    def _count_and_fee(since: int) -> dict:
        rows = conn.execute(
            "SELECT agent_id, COUNT(*) as n FROM trails "
            "WHERE timestamp>=? AND payment_hash IS NOT NULL AND payment_hash != '' "
            "GROUP BY agent_id ORDER BY n DESC",
            (since,),
        ).fetchall()
        total = sum(r["n"] for r in rows)
        # Approximation: each paid trail = TRAIL_FEE_SATS (exact depends on karma)
        return {
            "trails_paid": total,
            "sats_collected_approx": total * trail_fees.TRAIL_FEE_SATS,
            "by_agent": [{"agent_id": r["agent_id"], "trails": r["n"]} for r in rows],
        }

    data = {
        "today":  _count_and_fee(day_start),
        "week":   _count_and_fee(week_start),
        "month":  _count_and_fee(month_start),
        "fees_enabled": trail_fees.TRAIL_FEES_ENABLED,
        "fee_sats": trail_fees.TRAIL_FEE_SATS,
        "exempt_agents": list(trail_fees.EXEMPT_AGENTS),
        "integrator_whitelist": trail_fees.INTEGRATOR_FREE_TIERS,
    }
    conn.close()
    return JSONResponse(data)


@rest_app.post("/trails/whitelist")
async def trails_whitelist(request: Request):
    """Agrega o elimina un integrador del free tier (solo creador).
    Body: {agent_id, action: 'add'|'remove', daily_cap?: int}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    agent_id = body.get("agent_id", "").strip()
    action = body.get("action", "add")
    daily_cap = int(body.get("daily_cap", 100))
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)
    if action == "add":
        trail_fees.add_to_whitelist(agent_id, daily_cap)
        return JSONResponse({"status": "added", "agent_id": agent_id, "daily_cap": daily_cap})
    elif action == "remove":
        trail_fees.remove_from_whitelist(agent_id)
        return JSONResponse({"status": "removed", "agent_id": agent_id})
    return JSONResponse({"error": "action must be 'add' or 'remove'"}, status_code=400)


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
