"""
rama_purchase — flujo LN → $RAMA (Opción B: reservation simbólica)

Flujo:
  1. generate_rama_invoice(buyer_id, amount_sats, wallet_arb) → invoice LN
  2. buyer paga invoice via phoenixd
  3. confirm_rama_purchase(payment_hash, buyer_id, wallet_arb, amount_sats, note)
     → graba trail RAMA_GENESIS_PURCHASE con status pending_mainnet_delivery
     → retorna trail_id

Sin token circulante. El acto existe en Trails. Entrega post-deploy mainnet.
"""

import os
import time
import uuid
import json
import httpx

from mycelium_trails import record_trail, DB_PATH

PHOENIXD_URL = os.environ.get("PHOENIXD_URL", "http://localhost:9740")
PHOENIXD_PASSWORD = os.environ.get("PHOENIXD_PASSWORD", "")

GENESIS_AGENT = "creador"
GENESIS_EXEMPT = frozenset([GENESIS_AGENT, "giskard-self"])


def generate_rama_invoice(buyer_id: str, amount_sats: int, wallet_arb: str) -> dict:
    """Genera invoice LN para compra simbólica de $RAMA.

    Retorna: {payment_request, payment_hash, amount_sats, buyer_id, wallet_arb}
    """
    description = f"RAMA genesis reservation — {buyer_id} — {wallet_arb[:10]}…"
    resp = httpx.post(
        f"{PHOENIXD_URL}/createinvoice",
        auth=("", PHOENIXD_PASSWORD),
        data={"amountSat": amount_sats, "description": description},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "payment_request": data["serialized"],
        "payment_hash": data["paymentHash"],
        "amount_sats": amount_sats,
        "buyer_id": buyer_id,
        "wallet_arb": wallet_arb,
    }


def check_ln_payment(payment_hash: str) -> bool:
    """Verifica si el invoice LN está pagado en phoenixd."""
    resp = httpx.get(
        f"{PHOENIXD_URL}/payments/incoming/{payment_hash}",
        auth=("", PHOENIXD_PASSWORD),
        timeout=10,
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return resp.json().get("isPaid", False)


def confirm_rama_purchase(
    payment_hash: str,
    buyer_id: str,
    wallet_arb: str,
    amount_sats: int,
    note: str = "",
) -> dict:
    """Confirma pago LN y registra trail fundacional RAMA_GENESIS_PURCHASE.

    Verifica que el invoice esté pagado antes de grabar el trail.
    Retorna: {status, trail_id, payment_hash, wallet_arb, amount_sats}
    """
    if not check_ln_payment(payment_hash):
        return {"status": "unpaid", "error": "Invoice not settled. Pay and retry."}

    metadata = {
        "operation": "RAMA_GENESIS_PURCHASE",
        "buyer": buyer_id,
        "amount_sats": amount_sats,
        "wallet_arb": wallet_arb,
        "note": note or "First human participant. Skin in the game.",
        "payment_hash_ln": payment_hash,
        "status": "pending_mainnet_delivery",
        "delivery_condition": "RamaToken.sol deploy on mainnet + Legales approval",
    }

    nonce = str(uuid.uuid4())
    trail_id = record_trail(
        db_path=DB_PATH,
        agent_id=buyer_id,
        service="rama",
        operation="RAMA_GENESIS_PURCHASE",
        nonce=nonce,
        karma_at_time=None,
        success=True,
        rate_limit_cap=0,  # genesis — sin rate limit
        genesis_agents=GENESIS_EXEMPT,
        metadata=metadata,
        payment_hash=payment_hash,
    )

    if not trail_id:
        return {"status": "error", "error": "Trail registration failed."}

    return {
        "status": "ok",
        "trail_id": trail_id,
        "payment_hash": payment_hash,
        "wallet_arb": wallet_arb,
        "amount_sats": amount_sats,
        "delivery": "pending_mainnet_delivery",
    }


def get_genesis_purchases(db_path: str = DB_PATH) -> list:
    """Lista todos los trails RAMA_GENESIS_PURCHASE registrados."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trail_id, agent_id, timestamp, metadata, payment_hash "
        "FROM trails WHERE operation='RAMA_GENESIS_PURCHASE' ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        result.append({
            "trail_id": row["trail_id"],
            "buyer": row["agent_id"],
            "timestamp": row["timestamp"],
            "amount_sats": meta.get("amount_sats"),
            "wallet_arb": meta.get("wallet_arb"),
            "status": meta.get("status"),
            "note": meta.get("note"),
        })
    return result
