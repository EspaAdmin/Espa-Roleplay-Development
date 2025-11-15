# services/economy_modifiers.py
import logging
from typing import Dict, Any, List, Optional, Tuple
from db import get_conn

log = logging.getLogger(__name__)

VALID_SCOPES = {"global", "nation", "state", "province"}
VALID_EFFECTS = {"production", "population", "tax", "all"}
VALID_KINDS = {"mul", "add"}

async def add_modifier(scope: str,
                       scope_id: Optional[str],
                       effect: str,
                       kind: str,
                       value: float,
                       source: Optional[str] = None,
                       created_turn: Optional[int] = None,
                       expires_turn: Optional[int] = None) -> Dict[str, Any]:
    """
    Add a modifier.
    - scope: global|nation|state|province
    - scope_id: the id for that scope, or None for global
    - effect: production|population|tax|all
    - kind: mul or add
    - value: for mul -> 0.9 = -10%; for add -> -0.1 = -10%
    """
    scope = scope.lower()
    effect = effect.lower()
    kind = kind.lower()

    if scope not in VALID_SCOPES:
        return {"ok": False, "error": "invalid scope"}
    if effect not in VALID_EFFECTS:
        return {"ok": False, "error": "invalid effect"}
    if kind not in VALID_KINDS:
        return {"ok": False, "error": "invalid kind"}

    conn = await get_conn()
    try:
        cur = await conn.execute(
            "INSERT INTO modifiers (scope, scope_id, effect, kind, value, source, created_turn, expires_turn, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (scope, scope_id, effect, kind, float(value), source, created_turn, expires_turn)
        )
        await conn.commit()
        return {"ok": True, "id": cur.lastrowid}
    except Exception as e:
        log.exception("add_modifier failed")
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()


async def remove_modifier(mod_id: int) -> Dict[str, Any]:
    conn = await get_conn()
    try:
        await conn.execute("DELETE FROM modifiers WHERE id = ?", (mod_id,))
        await conn.commit()
        return {"ok": True}
    except Exception as e:
        log.exception("remove_modifier failed")
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()


async def list_modifiers(scope: Optional[str] = None, scope_id: Optional[str] = None, only_active: bool = True) -> List[Dict[str, Any]]:
    conn = await get_conn()
    try:
        q = "SELECT * FROM modifiers WHERE 1=1"
        params = []
        if scope:
            q += " AND scope = ?"; params.append(scope)
        if scope_id is not None:
            q += " AND scope_id = ?"; params.append(scope_id)
        if only_active:
            q += " AND active = 1"
        cur = await conn.execute(q, tuple(params))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# Core aggregator: compute final modifiers for a given state (or nation/global)
async def compute_final_modifiers_for_state(nation_id: str, state_id: Optional[str], current_turn: Optional[int] = None) -> Dict[str, Dict[str, float]]:
    """
    Returns a dict mapping effect -> {"add_sum": float, "mul_product": float, "final": float, "breakdown":[...] }
    Aggregates modifiers in the following order of precedence:
       - global
       - nation (nation_id)
       - state (state_id)
       - province modifiers are not included here (call compute for province if needed)
    Includes modifiers with effect == 'all' as applicable to every effect.
    """
    # collect candidate modifiers
    conn = await get_conn()
    try:
        # select active modifiers and those not expired
        cur = await conn.execute("SELECT * FROM modifiers WHERE active = 1")
        rows = await cur.fetchall()
        mods = [dict(r) for r in rows]
    finally:
        await conn.close()

    # helper to decide if modifier applies to our (scope, scope_id)
    def mod_applies(m):
        # expire check
        if current_turn is not None and m.get("expires_turn") is not None:
            if m["expires_turn"] < current_turn:
                return False
        # scope check
        s = m["scope"]
        sid = m.get("scope_id")
        if s == "global":
            return True
        if s == "nation" and sid == nation_id:
            return True
        if s == "state" and sid == state_id:
            return True
        if s == "province":
            return False  # provinces handled elsewhere
        return False

    # effects we will compute
    effects = ["production", "population", "tax"]
    out = {}
    for eff in effects:
        add_sum = 0.0
        muls = []
        breakdown = []
        for m in mods:
            if not mod_applies(m):
                continue
            if m["effect"] not in (eff, "all"):
                continue
            kind = m["kind"]
            val = float(m["value"])
            src = m.get("source")
            scope = m.get("scope")
            # record breakdown item
            breakdown.append({"id": m.get("id"), "scope": scope, "source": src, "kind": kind, "value": val})
            if kind == "add":
                add_sum += val
            else:
                # mul: treat value as multiplier fraction (e.g., 0.9)
                muls.append(val)
        prod = 1.0
        for m in muls:
            prod *= float(m)
        final = max(0.0, (1.0 + add_sum) * prod)
        out[eff] = {
            "add_sum": add_sum,
            "mul_product": prod,
            "final": final,
            "breakdown": breakdown
        }
    return out
