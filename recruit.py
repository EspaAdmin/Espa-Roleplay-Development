# services/recruit.py
# Recruit service aligned to provided game.db schema.
# - state-first resource deduction
# - tech checks
# - per-unit recruit queue rows (recruits table)
# - army / autocomplete helpers
# - list_available_units() to show all templates a nation can recruit

import json
import logging
from typing import List, Tuple, Dict, Any, Optional

from db import get_conn

LOG = logging.getLogger("services.recruit")
AUTOCOMPLETE_LIMIT = 25

# -------------------------
# Util
# -------------------------
async def _table_exists(conn, name: str) -> bool:
    cur = await conn.cursor()
    try:
        await cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        r = await cur.fetchone()
        return bool(r)
    except Exception:
        return False

def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        # fallback if it's sequence-like
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return dict(row)

def _parse_json_field(v):
    if not v:
        return {}
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except Exception:
        return {}

# -------------------------
# Unit templates & eligibility
# -------------------------
async def get_unit_template(template_id: str) -> Optional[Dict[str, Any]]:
    conn = await get_conn(); cur = await conn.cursor()
    try:
        await cur.execute("SELECT * FROM unit_templates WHERE template_id=? LIMIT 1", (template_id,))
        row = await cur.fetchone()
        await conn.close()
        return _row_to_dict(row)
    except Exception as e:
        LOG.exception("get_unit_template")
        try:
            await conn.close()
        except Exception:
            pass
        return None

async def _get_nation_row(nation_id: str) -> Optional[Dict[str, Any]]:
    conn = await get_conn(); cur = await conn.cursor()
    try:
        await cur.execute("SELECT * FROM playernations WHERE nation_id=? LIMIT 1", (nation_id,))
        r = await cur.fetchone()
        await conn.close()
        return _row_to_dict(r)
    except Exception:
        try:
            await conn.close()
        except Exception:
            pass
        return None

def _classification_allows(unit_row: Dict[str, Any], nation_row: Dict[str, Any]) -> bool:
    """Return True if the template is allowed for the nation based on classification/ref nation."""
    if not unit_row:
        return False
    cls = unit_row.get("classification") or ""
    cls = cls.strip()
    if not cls:
        return True
    # Normalize tokens
    tokens = {t.strip().upper() for t in cls.replace(",", " ").split()}
    # WGRD check: nation's WGRD_nation truthy
    if "WGRD" in tokens:
        if nation_row and nation_row.get("WGRD_nation"):
            return True
    # CMO check: reference_nation must match nation's affiliation
    if "CMO" in tokens:
        ref = (unit_row.get("reference_nation") or "").strip()
        aff = (nation_row.get("affiliation") or "").strip()
        if ref and aff and ref.lower() == aff.lower():
            return True
    # If token is a specific nation code, allow if matches nation's name/ref
    if tokens:
        # e.g. token might be 'US' and unit.reference_nation 'US'
        ref = (unit_row.get("reference_nation") or "").strip().upper()
        natref = (nation_row.get("name") or nation_row.get("nation_id") or "").strip().upper() if nation_row else ""
        if ref and ref in tokens:
            return True
        if natref and any(natref in t for t in tokens):
            return True
    # default deny
    return False

async def list_available_units(nation_id: str, prefix: str = "") -> List[Tuple[str, str]]:
    """
    Return list of (template_id, label) allowed for this nation, filtered by prefix.
    Label contains category and display_name.
    """
    nation = await _get_nation_row(nation_id)
    conn = await get_conn(); cur = await conn.cursor()
    out = []
    pref = (prefix or "").lower()
    try:
        await cur.execute("SELECT template_id, display_name, name, category, classification, reference_nation FROM unit_templates ORDER BY category, display_name")
        rows = await cur.fetchall()
        for r in rows:
            ur = _row_to_dict(r)
            tid = str(ur.get("template_id"))
            label_name = ur.get("display_name") or ur.get("name") or tid
            cat = ur.get("category") or ""
            label = f"{label_name} [{cat}] ({tid})"
            if pref and pref not in label.lower() and pref not in tid.lower():
                continue
            if _classification_allows(ur, nation):
                out.append((tid, label))
                if len(out) >= AUTOCOMPLETE_LIMIT:
                    break
    except Exception as e:
        LOG.exception("list_available_units")
    await conn.close()
    return out

# -------------------------
# Unit autocomplete (drilldown)
# -------------------------
async def unit_autocomplete_for_nation(nation_id: str, prefix: str = "") -> List[Tuple[str,str]]:
    """
    If prefix matches a category, return units in that category.
    Otherwise return categories or matching templates.
    """
    conn = await get_conn(); cur = await conn.cursor()
    try:
        # categories
        await cur.execute("SELECT DISTINCT category FROM unit_templates WHERE category IS NOT NULL ORDER BY category")
        cats = [dict(r)["category"] for r in await cur.fetchall()]
    except Exception:
        cats = []
    await conn.close()
    pref = (prefix or "").strip()
    if not pref:
        # return categories
        return [(c, c) for c in cats[:AUTOCOMPLETE_LIMIT]]

    # if exact category match -> return units in category
    for c in cats:
        if c and c.lower() == pref.lower():
            return await units_in_category_for_nation(nation_id, c, prefix="")

    # otherwise search by name/template id
    return await list_available_units(nation_id, prefix=pref)

async def units_in_category_for_nation(nation_id: str, category: str, prefix: str = "") -> List[Tuple[str,str]]:
    nation = await _get_nation_row(nation_id)
    conn = await get_conn(); cur = await conn.cursor()
    pref = (prefix or "").lower()
    out = []
    try:
        await cur.execute("SELECT template_id, display_name, name, classification, reference_nation FROM unit_templates WHERE category=? ORDER BY display_name", (category,))
        rows = await cur.fetchall()
        for r in rows:
            ur = _row_to_dict(r)
            if not _classification_allows(ur, nation):
                continue
            tid = str(ur.get("template_id"))
            label_name = ur.get("display_name") or ur.get("name") or tid
            label = f"{label_name} ({tid})"
            if pref and pref not in label.lower():
                continue
            out.append((tid, label))
            if len(out) >= AUTOCOMPLETE_LIMIT:
                break
    except Exception as e:
        LOG.exception("units_in_category_for_nation")
    await conn.close()
    return out

# -------------------------
# Armies helpers (schema uses armies.army_id)
# -------------------------
async def list_armies_for_nation(nation_id: str, prefix: str = "") -> List[Tuple[str,str]]:
    conn = await get_conn(); cur = await conn.cursor()
    out = []
    pref = (prefix or "").lower()
    try:
        # armies table present per schema
        await cur.execute("SELECT army_id, name, state_id FROM armies WHERE nation_id=? ORDER BY name", (nation_id,))
        rows = await cur.fetchall()
        for r in rows:
            ar = _row_to_dict(r)
            aid = str(ar.get("army_id"))
            label = f"{ar.get('name') or aid} ({ar.get('state_id')})"
            if pref and pref not in label.lower():
                continue
            out.append((aid, label))
            if len(out) >= AUTOCOMPLETE_LIMIT:
                break
    except Exception as e:
        LOG.exception("list_armies_for_nation")
    await conn.close()
    return out

async def create_army(nation_id: str, name: str, state_id: str) -> Dict[str,Any]:
    conn = await get_conn(); cur = await conn.cursor()
    try:
        await cur.execute("INSERT INTO armies (nation_id, name, state_id) VALUES (?,?,?)", (nation_id, name, state_id))
        army_id = cur.lastrowid
        await conn.commit(); await conn.close()
        return {"ok": True, "army_id": army_id}
    except Exception as e:
        LOG.exception("create_army")
        try:
            await conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}

async def get_army_details(nation_id: str, army_id: str) -> Dict[str, Any]:
    conn = await get_conn(); cur = await conn.cursor()
    try:
        await cur.execute("SELECT army_id, name, state_id FROM armies WHERE army_id=? AND nation_id=? LIMIT 1", (army_id, nation_id))
        a = await cur.fetchone()
        if not a:
            await conn.close(); return {"ok": False, "error":"Army not found"}
        army = _row_to_dict(a)
        # units per army: try an army_units or playerarmy_units table, fallback to empty
        units = []
        try:
            await cur.execute("SELECT template_id, quantity FROM army_units WHERE army_id=?", (army_id,))
            rows = await cur.fetchall()
            for r in rows:
                units.append(_row_to_dict(r))
        except Exception:
            try:
                await cur.execute("SELECT template_id, qty as quantity FROM playerarmy_units WHERE army_id=?", (army_id,))
                rows = await cur.fetchall()
                for r in rows:
                    units.append(_row_to_dict(r))
            except Exception:
                units = []
        await conn.close()
        return {"ok": True, "army": army, "units": units}
    except Exception as e:
        LOG.exception("get_army_details")
        try:
            await conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}

# -------------------------
# State manpower and resource helpers
# -------------------------
async def state_recruitable_manpower(conn, state_id: str, nation_id: str) -> int:
    cur = await conn.cursor()
    # total population in state controlled by nation
    await cur.execute("SELECT COALESCE(SUM(population),0) as p FROM provinces WHERE state_id=? AND controller_id=?", (state_id, nation_id))
    r = await cur.fetchone()
    total_pop = int((r["p"] if r and "p" in r.keys() else (r[0] if r else 0)) or 0)
    # building manpower used in state
    await cur.execute("""
        SELECT COALESCE(SUM(bt.maintenance_manpower * pb.count),0) as manpower_used
        FROM province_buildings pb
        JOIN provinces p ON pb.province_id = p.province_id
        JOIN building_templates bt ON pb.building_id = bt.id
        WHERE p.state_id = ? AND p.controller_id = ?
    """, (state_id, nation_id))
    mu = await cur.fetchone()
    used_by_buildings = int((mu["manpower_used"] if mu and "manpower_used" in mu.keys() else (mu[0] if mu else 0)) or 0)
    # committed troops: sum of recruits assigned to armies located in this state's provinces (best-effort)
    committed = 0
    try:
        await cur.execute("SELECT province_id FROM provinces WHERE state_id=? AND controller_id=?", (state_id, nation_id))
        provs = [pr["province_id"] for pr in await cur.fetchall()]
        if provs:
            placeholders = ",".join("?" * len(provs))
            q = f"SELECT COALESCE(COUNT(*),0) as committed FROM recruits WHERE province_id IN ({placeholders}) AND nation_id=?"
            params = tuple(provs) + (nation_id,)
            await cur.execute(q, params)
            cr = await cur.fetchone()
            committed = int((cr["committed"] if cr and "committed" in cr.keys() else (cr[0] if cr else 0)) or 0)
    except Exception:
        committed = 0
    cap = int(total_pop * 0.40)
    available = max(0, cap - used_by_buildings - committed)
    return available

async def _deduct_resource_state_then_nation(conn, nation_id: str, state_id: str, resource: str, needed: float) -> Tuple[bool, float]:
    cur = await conn.cursor()
    remaining = float(needed)
    deducted = 0.0
    # state provinces first
    await cur.execute("SELECT province_id FROM provinces WHERE state_id=? AND controller_id=?", (state_id, nation_id))
    provs = [r["province_id"] for r in await cur.fetchall()]
    for pid in provs:
        if remaining <= 0:
            break
        await cur.execute("SELECT amount FROM province_stockpiles WHERE province_id=? AND resource=?", (pid, resource))
        r = await cur.fetchone()
        avail = float((r["amount"] if r and "amount" in r.keys() else (r[0] if r else 0)) or 0)
        take = min(avail, remaining)
        if take > 0:
            await cur.execute("UPDATE province_stockpiles SET amount = amount - ? WHERE province_id=? AND resource=?", (take, pid, resource))
            remaining -= take
            deducted += take
    # nation-wide
    if remaining > 0:
        await cur.execute("SELECT province_id FROM provinces WHERE controller_id=?", (nation_id,))
        all_provs = [r["province_id"] for r in await cur.fetchall()]
        for pid in all_provs:
            if remaining <= 0:
                break
            if pid in provs:
                continue
            await cur.execute("SELECT amount FROM province_stockpiles WHERE province_id=? AND resource=?", (pid, resource))
            r = await cur.fetchone()
            avail = float((r["amount"] if r and "amount" in r.keys() else (r[0] if r else 0)) or 0)
            take = min(avail, remaining)
            if take > 0:
                await cur.execute("UPDATE province_stockpiles SET amount = amount - ? WHERE province_id=? AND resource=?", (take, pid, resource))
                remaining -= take
                deducted += take
    # do not commit here; caller will commit after inserting recruits
    success = remaining <= 1e-9
    return success, deducted

# -------------------------
# Cost estimation & recruit flow
# -------------------------
async def estimate_recruit_cost(nation_id: str, template_id: str, quantity: int = 1) -> Dict[str,Any]:
    tpl = await get_unit_template(template_id)
    if not tpl:
        return {"error": "Template not found"}
    manpower_per = int(tpl.get("manpower_cost") or 0)
    cash_per = float(tpl.get("build_cash_cost") or 0.0)
    resources = _parse_json_field(tpl.get("resources_json") or "{}")
    qty = max(1, int(quantity))
    manpower_total = manpower_per * qty
    cash_total = int(round(cash_per * qty))
    resources_total = {}
    for k, v in resources.items():
        try:
            resources_total[k] = int(round(float(v) * qty))
        except Exception:
            resources_total[k] = int(round((v or 0) * qty))
    return {"manpower": manpower_total, "cash": cash_total, "resources": resources_total}

async def recruit_unit(nation_id: str, template_id: str, quantity: int, state_id: str, army_id: Optional[int] = None) -> Dict[str,Any]:
    """
    Queue (and reserve/deduct) recruitment of `quantity` units of template_id from `state_id`.
    Inserts one row per unit into recruits table (schema), reserves manpower & deducts cash & resources.
    """
    conn = await get_conn(); cur = await conn.cursor()
    try:
        tpl = await get_unit_template(template_id)
        if not tpl:
            await conn.close(); return {"error":"Template not found"}
        # tech_required check (player_technologies)
        tech_req = (tpl.get("tech_required") or "").strip()
        if tech_req:
            try:
                await cur.execute("SELECT 1 FROM player_technologies WHERE nation_id=? AND tech_id=? LIMIT 1", (nation_id, tech_req))
                ok = await cur.fetchone()
                if not ok:
                    await conn.close(); return {"error":"Required tech not researched"}
            except Exception:
                # permissive fallback
                pass

        # check eligibility by classification
        nation_row = await _get_nation_row(nation_id)
        if not _classification_allows(tpl, nation_row):
            await conn.close(); return {"error":"Unit not allowed for your nation (classification/affiliation mismatch)"}

        est = await estimate_recruit_cost(nation_id, template_id, quantity)
        if est.get("error"):
            await conn.close(); return est

        # check state manpower
        avail_manpower = await state_recruitable_manpower(conn, state_id, nation_id)
        if est["manpower"] > avail_manpower:
            await conn.close(); return {"error":"Insufficient recruitable manpower in state", "missing_manpower": est["manpower"] - avail_manpower}

        # check cash
        await cur.execute("SELECT cash FROM playernations WHERE nation_id=? LIMIT 1", (nation_id,))
        prow = await cur.fetchone()
        cash_available = float((prow["cash"] if prow and "cash" in prow.keys() else (prow[0] if prow else 0)) or 0)
        if cash_available < est["cash"]:
            await conn.close(); return {"error":"Insufficient cash", "missing_cash": int(round(est["cash"] - cash_available))}

        # attempt to deduct resources state-first then nation-wide
        resources_missing = {}
        for res, need in est["resources"].items():
            ok, deducted = await _deduct_resource_state_then_nation(conn, nation_id, state_id, res, float(need))
            if not ok:
                resources_missing[res] = int(round(need - deducted))
        if resources_missing:
            await conn.rollback(); await conn.close()
            return {"error":"Missing resources", "missing": resources_missing}

        # deduct cash and reserve manpower in playernations
        await cur.execute("UPDATE playernations SET cash = cash - ? WHERE nation_id=?", (est["cash"], nation_id))
        # increment manpower_used
        await cur.execute("UPDATE playernations SET manpower_used = COALESCE(manpower_used,0) + ? WHERE nation_id=?", (est["manpower"], nation_id))

        # insert per-unit rows into recruits table
        # recruits schema: recruit_id, nation_id, army_id, state_id, province_id, unit_template_id, created_turn, status
        # province_id: best-effort choose first province in state owned by nation
        await cur.execute("SELECT province_id FROM provinces WHERE state_id=? AND controller_id=? LIMIT 1", (state_id, nation_id))
        prov_row = await cur.fetchone()
        province_id = prov_row["province_id"] if prov_row and "province_id" in prov_row.keys() else (prov_row[0] if prov_row else None)
        # find current turn
        await cur.execute("SELECT value FROM config WHERE key='current_turn' LIMIT 1")
        crow = await cur.fetchone()
        current_turn = int((crow["value"] if crow and "value" in crow.keys() else (crow[0] if crow else 1)) or 1)
        for _ in range(max(1, int(quantity))):
            await cur.execute("""INSERT INTO recruits (nation_id, army_id, state_id, province_id, unit_template_id, created_turn, status)
                                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
                              (nation_id, army_id, state_id, province_id, template_id, current_turn, "queued"))
        await conn.commit()
        await conn.close()
        return {"ok": True, "queued": int(quantity)}
    except Exception as e:
        LOG.exception("recruit_unit failed")
        try:
            await conn.rollback()
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}

# -------------------------
# Recruit list / cancel
# -------------------------
async def list_recruits(nation_id: str, state_id: Optional[str] = None) -> List[Dict[str,Any]]:
    conn = await get_conn(); cur = await conn.cursor()
    try:
        q = "SELECT recruit_id, unit_template_id, army_id, state_id, province_id, created_turn, status FROM recruits WHERE nation_id=?"
        params = [nation_id]
        if state_id:
            q += " AND state_id=?"; params.append(state_id)
        await cur.execute(q, tuple(params))
        rows = [ _row_to_dict(r) for r in await cur.fetchall() ]
        await conn.close()
        return rows
    except Exception as e:
        LOG.exception("list_recruits")
        try:
            await conn.close()
        except Exception:
            pass
        return []

async def disband_recruit(nation_id: str, recruit_id: int) -> Dict[str,Any]:
    conn = await get_conn(); cur = await conn.cursor()
    try:
        await cur.execute("SELECT * FROM recruits WHERE recruit_id=? AND nation_id=? LIMIT 1", (recruit_id, nation_id))
        r = await cur.fetchone()
        if not r:
            await conn.close(); return {"ok": False, "error": "Not found"}
        # attempt refund policy: refund cash and restore resources not implemented here (complex) - just delete and free manpower
        tpl_id = r["unit_template_id"] if "unit_template_id" in r.keys() else (r[4] if len(r)>=5 else None)
        # estimate cost to rollback manpower/cash: best-effort
        est = await estimate_recruit_cost(nation_id, tpl_id, 1)
        # remove row
        await cur.execute("DELETE FROM recruits WHERE recruit_id=? AND nation_id=?", (recruit_id, nation_id))
        # refund cash and manpower (simple)
        if est and not est.get("error"):
            try:
                await cur.execute("UPDATE playernations SET cash = COALESCE(cash,0) + ? WHERE nation_id=?", (est["cash"], nation_id))
                await cur.execute("UPDATE playernations SET manpower_used = COALESCE(manpower_used,0) - ? WHERE nation_id=?", (est["manpower"], nation_id))
            except Exception:
                pass
        await conn.commit(); await conn.close()
        return {"ok": True}
    except Exception as e:
        LOG.exception("disband_recruit")
        try:
            await conn.rollback()
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
