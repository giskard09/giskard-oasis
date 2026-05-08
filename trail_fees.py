"""
trail_fees — lógica de cobro por trail y free tier por integrador.

Reglas:
  - Agentes internos (EXEMPT_AGENTS): siempre gratis.
  - Integradores en INTEGRATOR_WHITELIST: N trails/día gratis, luego fee normal.
  - Resto: TRAIL_FEE_SATS sats/trail via Lightning, con descuento por karma.
  - TRAIL_FEES_ENABLED=false → todo gratis (modo wu wei hasta confirmación de integrador).

Karma discount (mismo esquema que Oasis):
  karma 0        → TRAIL_FEE_SATS     (21 sats)
  karma 1-20     → 15 sats
  karma 21-50    → 10 sats
  karma 50+      →  5 sats
"""

import os
import sqlite3
import time
from typing import Optional

TRAIL_FEE_SATS = int(os.getenv("TRAIL_FEE_SATS", "21"))
TRAIL_FEES_ENABLED = os.getenv("TRAIL_FEES_ENABLED", "false").lower() == "true"

# Agentes siempre exentos — nunca pagan, nunca se pide invoice
EXEMPT_AGENTS = frozenset({
    "giskard-self",
    "pioneer-agent-001",
    "lightning",
    "creador",
})

# Free tier por integrador: {agent_id_prefix_or_exact: trails_per_day}
# Activado manualmente por el creador via /trails/whitelist o edición directa
INTEGRATOR_FREE_TIERS: dict[str, int] = {
    "aeoess-aps":  100,
    "chox-cell":   100,
    "accord":      100,
}

# Descuento por karma (sats a cobrar)
def fee_for_karma(karma: int) -> int:
    if karma >= 50:
        return 5
    if karma >= 21:
        return 10
    if karma >= 1:
        return 15
    return TRAIL_FEE_SATS


def is_exempt(agent_id: str) -> bool:
    return agent_id in EXEMPT_AGENTS


def free_tier_remaining(agent_id: str, db_path: str) -> int:
    """Retorna cuántos trails gratuitos le quedan hoy al integrador.
    Si no está en whitelist, retorna 0.
    """
    cap = None
    for prefix, daily_cap in INTEGRATOR_FREE_TIERS.items():
        if agent_id == prefix or agent_id.startswith(prefix):
            cap = daily_cap
            break
    if cap is None:
        return 0

    # Contar trails de hoy para este agente
    day_start = int(time.time()) - (int(time.time()) % 86400)
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM trails WHERE agent_id=? AND timestamp>=?",
            (agent_id, day_start),
        ).fetchone()
        conn.close()
        used = row[0] if row else 0
    except Exception:
        return cap
    return max(0, cap - used)


def trail_fee_required(agent_id: str, karma: int, db_path: str) -> Optional[int]:
    """Retorna sats a cobrar por el próximo trail, o None si es gratis.

    None → gratis (exento o dentro del free tier)
    int  → sats a cobrar via Lightning
    """
    if not TRAIL_FEES_ENABLED:
        return None
    if is_exempt(agent_id):
        return None
    if free_tier_remaining(agent_id, db_path) > 0:
        return None
    return fee_for_karma(karma)


def add_to_whitelist(agent_id: str, daily_cap: int = 100) -> None:
    """Agrega un integrador al free tier en runtime (no persiste al reiniciar)."""
    INTEGRATOR_FREE_TIERS[agent_id] = daily_cap


def remove_from_whitelist(agent_id: str) -> None:
    INTEGRATOR_FREE_TIERS.pop(agent_id, None)
