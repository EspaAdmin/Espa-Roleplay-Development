# services/starter.py
"""
Starter management service.

Provides a single manager function `manage_starter` which supports admin
operations for seeding or altering starter data for a nation.

Supported actions (action arg):
 - "add"      : increment (cash, population, resources, buildings, tech)
 - "subtract" : decrement (cash, resources)
 - "set"      : set exact value (cash, population)
 - "list"     : list current starter-like information (cash, provinces count, sample provinces)

Supported types (type arg):
 - "cash"
 - "population"
 - "resource"   (single resource name)
 - "resources"  (bulk via dict)
 - "building"   (single building template)
 - "buildings"  (bulk)
 - "tech"       (placeholder: marks technology as owned)

This module is defensive and returns dictionaries with "ok" and other keys
to describe what happened.

Assumptions:
 - An async DB accessor `db.get_conn()` exists and returns an aiosqlite connection
   whose rows are dict-like (aiosqlite.Row). Adjust if your project uses another name.
"""

import aiosqlite
import json
import random
import datetime
from typing import Any, Dict, List, Optional, Tuple

# change import path if your project uses a different db helper
from db import get_conn

DEFAULT_PROVINCE_POP = 100000  # default if setting population per-province

# ---------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------
async def _fetch_one(conn, sql: str, params: tuple = ()):
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return dict(row) if row else None

async def _fetch_all(conn, sql: str, params: tuple = ()):
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    return [dict(r) for r in rows]

# try to resolve nation by name or id
async def resolve_nation(nation_identifier: str) -> Optional[Dict[str, Any]]:
    """
    Accepts either a nation_id or a name. Returns the playernations row (dict) or None.
    """
    conn = await get_conn()
    try:
        # try direct nation_id match first
        r = await _fetch_one(conn, "SELECT * FROM playernations WHERE nation_id = ? LIMIT 1", (nation_identifier,))
        if r:
            return r
        # try name column fallback
        r = await _fetch_one(conn, "SELECT * FROM playernations WHERE COALESCE(name, '') = ? LIMIT 1", (nation_identifier,))
        if r:
            return r
        # try case-insensitive substring on name
        cur = await conn.execute("SELECT * FROM playernations WHERE LOWER(COALESCE(name,'')) LIKE ? LIMIT 1", (f"%{nation_identifier.lower()}%",))
        prow = await cur.fetchone()
        return dict(prow) if prow else None
    finally:
        await conn.close()

# gather provinces owned by nation
async def _get_provinces_for_nation(conn, nation_id: str) -> List[Dict[str, Any]]:
    rows = await _fetch_all(conn, "SELECT province_id, name, population, node_strength, resource FROM provinces WHERE controller_id = ?", (nation_id,))
    return rows

# helper to evenly distribute an amount across provinces (by node_strength or simple even split)
def _distribute_even(total: float, slots: int) -> List[float]:
    if slots <= 0:
        return []
    base = total // slots
    remainder = int(total - base * slots)
    arr = [base] * slots
    i = 0
    while remainder > 0 and i < slots:
        arr[i] += 1
        remainder -= 1
        i += 1
    return arr

# ---------------------------------------------------------------------
# Starter operations
# ---------------------------------------------------------------------
async def add_cash_to_nation(nation_id: str, amount: float) -> Dict[str, Any]:
    conn = await get_conn()
    try:
        await conn.execute("UPDATE playernations SET cash = COALESCE(cash,0) + ? WHERE nation_id = ?", (amount, nation_id))
        await conn.commit()
        return {"ok": True, "nation_id": nation_id, "cash_added": amount}
    except Exception as e:
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()

async def set_cash_on_nation(nation_id: str, amount: float) -> Dict[str, Any]:
    conn = await get_conn()
    try:
        await conn.execute("UPDATE playernations SET cash = ? WHERE nation_id = ?", (amount, nation_id))
        await conn.commit()
        return {"ok": True, "nation_id": nation_id, "cash_set": amount}
    except Exception as e:
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()

async def add_population_per_province(nation_id: str, per_province: int = DEFAULT_PROVINCE_POP) -> Dict[str, Any]:
    """
    Sets (adds) population for all provinces owned by the nation to per_province.
    If province already has population > 0 and you want to force replace use action 'set' on type 'population'.
    """
    conn = await get_conn()
    try:
        # fetch provinces
        provinces = await _get_provinces_for_nation(conn, nation_id)
        if not provinces:
            await conn.close()
            return {"ok": False, "error": "No provinces found for nation", "nation_id": nation_id}
        updated = []
        for p in provinces:
            pid = p["province_id"]
            # if present and >0, skip (this is 'add; to force set use 'set' type)
            curr_pop = int(p.get("population") or 0)
            if curr_pop == 0:
                await conn.execute("UPDATE provinces SET population = ? WHERE province_id = ?", (per_province, pid))
                updated.append(pid)
        await conn.commit()
        return {"ok": True, "nation_id": nation_id, "provinces_updated": len(updated), "updated_provinces": updated}
    except Exception as e:
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()

async def set_population_for_nation(nation_id: str, per_province: int = DEFAULT_PROVINCE_POP) -> Dict[str, Any]:
    conn = await get_conn()
    try:
        provinces = await _get_provinces_for_nation(conn, nation_id)
        if not provinces:
            await conn.close()
            return {"ok": False, "error": "No provinces found for nation", "nation_id": nation_id}
        updated = []
        for p in provinces:
            pid = p["province_id"]
            await conn.execute("UPDATE provinces SET population = ? WHERE province_id = ?", (per_province, pid))
            updated.append(pid)
        await conn.commit()
        return {"ok": True, "nation_id": nation_id, "provinces_updated": len(updated), "updated_provinces": updated}
    except Exception as e:
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()

async def add_resource_to_nation(nation_id: str, resource: str, total_amount: float) -> Dict[str, Any]:
    """
    Distributes total_amount of resource across the nation's provinces (even split).
    Uses province_stockpiles rows if available; otherwise creates new stockpile entries in top provinces.
    """
    conn = await get_conn()
    try:
        provinces = await _get_provinces_for_nation(conn, nation_id)
        if not provinces:
            await conn.close()
            return {"ok": False, "error": "No provinces found for nation"}
        slots = len(provinces)
        parts = _distribute_even(int(total_amount), slots)
        placed = []
        for p, amt in zip(provinces, parts):
            if amt <= 0:
                continue
            # check if stockpile row exists
            cur = await conn.execute("SELECT rowid, amount, capacity FROM province_stockpiles WHERE province_id = ? AND resource = ? LIMIT 1", (p["province_id"], resource))
            r = await cur.fetchone()
            if r:
                row = dict(r)
                new_amount = float(row.get("amount") or 0) + amt
                cap = float(row.get("capacity") or 0)
                if cap and new_amount > cap:
                    new_amount = cap
                await conn.execute("UPDATE province_stockpiles SET amount = ? WHERE rowid = ?", (new_amount, row["rowid"]))
                placed.append({"province_id": p["province_id"], "added": amt, "now": new_amount})
            else:
                default_cap = 100000.0
                insert_amt = min(amt, default_cap)
                await conn.execute("INSERT INTO province_stockpiles (province_id, resource, amount, capacity) VALUES (?, ?, ?, ?)", (p["province_id"], resource, insert_amt, default_cap))
                placed.append({"province_id": p["province_id"], "added": insert_amt, "now": insert_amt})
        await conn.commit()
        return {"ok": True, "nation_id": nation_id, "resource": resource, "distributed_total": sum(p['added'] for p in placed), "placements": placed}
    except Exception as e:
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()

async def add_buildings_to_nation(nation_id: str, building_template: str, count: int = 1, tier: int = 1) -> Dict[str, Any]:
    """
    Add 'count' instances of building_template to provinces owned by the nation.
    Attempt to place into provinces that are suitable (simple heuristic: if building name contains a resource name prefer provinces that have that resource).
    If none suitable - place to any province that has no identical building up to a reasonable limit.
    """
    conn = await get_conn()
    try:
        provinces = await _get_provinces_for_nation(conn, nation_id)
        if not provinces:
            await conn.close()
            return {"ok": False, "error": "No provinces found for nation"}

        # Try to read building template row if it exists to infer resource hints
        cur = await conn.execute("SELECT * FROM building_templates WHERE template_id = ? LIMIT 1", (building_template,))
        bt = await cur.fetchone()
        bt_row = dict(bt) if bt else None
        preferred_resource = None

        if bt_row:
            # Attempt to parse an inputs_json or similar column to figure out resource hints
            try:
                inp = None
                # common column names that might contain inputs
                for key in ("inputs_json", "inputs", "requires_json", "inputs_map"):
                    if key in bt_row and bt_row.get(key):
                        inp = bt_row.get(key)
                        break
                if inp and isinstance(inp, str):
                    try:
                        inp_j = json.loads(inp)
                    except Exception:
                        inp_j = None
                else:
                    inp_j = inp if isinstance(inp, dict) else None

                # If we have a dict, take the first key as a hint
                if isinstance(inp_j, dict) and inp_j:
                    first_key = next(iter(inp_j.keys()), None)
                    if first_key:
                        preferred_resource = first_key
                else:
                    # fallback: inspect name for resource tokens (simple heuristic)
                    nm = (bt_row.get("name") or "").lower()
                    for token in ("raw ore", "ore", "coal", "oil", "uranium", "food"):
                        if token in nm:
                            # normalize token names to expected resource labels
                            if token in ("raw ore", "ore"):
                                preferred_resource = "Raw Ore"
                            elif token == "coal":
                                preferred_resource = "Coal"
                            elif token == "oil":
                                preferred_resource = "Oil"
                            elif token == "uranium":
                                # prefer the "Raw Uranium" naming you mentioned earlier
                                preferred_resource = "Raw Uranium"
                            elif token == "food":
                                preferred_resource = "Food"
                            break
            except Exception:
                # ignore parsing errors, fall back to name-based hints only
                preferred_resource = None

        # Build candidate list - prioritize provinces with preferred_resource
        candidates = provinces.copy()
        if preferred_resource:
            pref = [p for p in provinces if (p.get("resource") or "").lower() == str(preferred_resource).lower()]
            other = [p for p in provinces if p not in pref]
            candidates = pref + other

        # randomize to spread placements
        random.shuffle(candidates)

        placed = []
        attempted = 0
        for p in candidates:
            if attempted >= count:
                break
            pid = p["province_id"]

            # guard: don't place more than a small number of the same building per province
            cur = await conn.execute("SELECT COUNT(*) as cnt FROM province_buildings WHERE province_id = ? AND (building_template = ? OR building_id = ?)", (pid, building_template, building_template))
            rc = await cur.fetchone()
            cnt = int(rc["cnt"] or 0) if rc else 0
            if cnt >= 3:
                continue

            # Try the common insert schema; if it fails, try alternate column names
            ts = datetime.datetime.utcnow().isoformat()
            inserted = False
            try:
                await conn.execute(
                    "INSERT INTO province_buildings (province_id, building_template, tier, installed_at) VALUES (?, ?, ?, ?)",
                    (pid, building_template, tier, ts)
                )
                inserted = True
            except Exception:
                # fallback: try building_id column name
                try:
                    await conn.execute(
                        "INSERT INTO province_buildings (province_id, building_id, tier, installed_at) VALUES (?, ?, ?, ?)",
                        (pid, building_template, tier, ts)
                    )
                    inserted = True
                except Exception:
                    # as a last resort, try a very permissive insert if schema is different (best-effort)
                    try:
                        # find columns dynamically (not always possible) - skip if not supported
                        pass
                    except Exception:
                        inserted = False

            if inserted:
                placed.append({"province_id": pid, "building_template": building_template, "tier": tier})
                attempted += 1

        await conn.commit()
        return {"ok": True, "nation_id": nation_id, "placed": placed, "requested": count, "placed_count": len(placed)}
    except Exception as e:
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()

        
async def list_starter_status(nation_id: str) -> Dict[str, Any]:
    """
    Return a summary of nation starter state: cash, province count, total population, sample provinces, stockpiles summary.
    """
    conn = await get_conn()
    try:
        pn = await _fetch_one(conn, "SELECT nation_id, COALESCE(name,'') as name, COALESCE(cash,0) as cash FROM playernations WHERE nation_id = ? LIMIT 1", (nation_id,))
        if not pn:
            await conn.close()
            return {"ok": False, "error": "Nation not found", "nation_id": nation_id}
        provinces = await _get_provinces_for_nation(conn, nation_id)
        total_pop = sum(int(p.get("population") or 0) for p in provinces)
        province_count = len(provinces)
        sample = [{"province_id": p["province_id"], "population": p.get("population") or 0, "resource": p.get("resource")} for p in provinces[:10]]
        # stockpile summary: total per resource
        cur = await conn.execute("""
            SELECT ps.resource, COALESCE(SUM(ps.amount),0) as total
            FROM province_stockpiles ps
            JOIN provinces p ON ps.province_id = p.province_id
            WHERE p.controller_id = ?
            GROUP BY ps.resource
        """, (nation_id,))
        rows = await cur.fetchall()
        stock_summary = {r["resource"]: float(r["total"] or 0) for r in rows}
        await conn.close()
        return {"ok": True, "nation": pn, "province_count": province_count, "total_population": total_pop, "sample_provinces": sample, "stock_summary": stock_summary}
    except Exception as e:
        try:
            await conn.close()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}

# ---------------------------------------------------------------------
# Main manager function - single entrypoint
# ---------------------------------------------------------------------
async def manage_starter(action: str,
                         typ: str,
                         nation_identifier: str,
                         *,
                         amount: Optional[float] = None,
                         resource: Optional[str] = None,
                         building_template: Optional[str] = None,
                         count: Optional[int] = None,
                         tier: Optional[int] = 1,
                         per_province: Optional[int] = None,
                         tech_id: Optional[str] = None
                         ) -> Dict[str, Any]:
    """
    action: "add" | "subtract" | "set" | "list"
    typ: one of "cash","population","resource","resources","building","buildings","tech"
    nation_identifier: nation_id or nation name (string)
    other args are type-specific
    """
    # resolve nation
    pn = await resolve_nation(nation_identifier)
    if not pn:
        return {"ok": False, "error": f"No playernation found with nation_id or name = {nation_identifier}"}
    nation_id = pn["nation_id"]

    # dispatch
    action = action.lower()
    typ = typ.lower()

    try:
        if action == "list":
            return await list_starter_status(nation_id)

        if typ == "cash":
            if action == "add":
                if amount is None:
                    return {"ok": False, "error": "amount is required for add cash"}
                return await add_cash_to_nation(nation_id, float(amount))
            if action == "subtract":
                if amount is None:
                    return {"ok": False, "error": "amount is required for subtract cash"}
                return await add_cash_to_nation(nation_id, -float(amount))
            if action == "set":
                if amount is None:
                    return {"ok": False, "error": "amount is required for set cash"}
                return await set_cash_on_nation(nation_id, float(amount))
            return {"ok": False, "error": f"Unsupported action {action} for cash"}

        if typ == "population":
            if action == "add":
                per = per_province or int(amount or DEFAULT_PROVINCE_POP)
                return await add_population_per_province(nation_id, per)
            if action == "set":
                per = per_province or int(amount or DEFAULT_PROVINCE_POP)
                return await set_population_for_nation(nation_id, per)
            return {"ok": False, "error": f"Unsupported action {action} for population"}

        if typ in ("resource","resources"):
            # for 'resource' expect 'resource' and amount; for 'resources' expect `amount` to be total and resource used
            if resource is None:
                return {"ok": False, "error": "resource parameter required"}
            if action == "add":
                if amount is None:
                    return {"ok": False, "error": "amount required to add resource"}
                return await add_resource_to_nation(nation_id, resource, float(amount))
            if action == "subtract":
                # subtract is not implemented fine-grained: try to remove amount greedily
                if amount is None:
                    return {"ok": False, "error": "amount required to subtract resource"}
                # reuse add_resource_to_nation negative?
                # We'll attempt removal via province_stockpiles greedy algorithm
                conn = await get_conn()
                try:
                    remain = float(amount)
                    rows = await _fetch_all(conn, """
                        SELECT ps.rowid as ps_rowid, ps.amount, p.province_id
                        FROM province_stockpiles ps
                        JOIN provinces p ON ps.province_id = p.province_id
                        WHERE p.controller_id = ? AND ps.resource = ? AND ps.amount > 0
                        ORDER BY COALESCE(p.node_strength,0) DESC
                    """, (nation_id, resource))
                    removed = []
                    for r in rows:
                        if remain <= 0:
                            break
                        avail = float(r["amount"] or 0)
                        take = min(avail, remain)
                        newamt = avail - take
                        await conn.execute("UPDATE province_stockpiles SET amount = ? WHERE rowid = ?", (newamt, r["ps_rowid"]))
                        removed.append({"province_id": r["province_id"], "removed": take, "now": newamt})
                        remain -= take
                    await conn.commit()
                    return {"ok": True, "requested_removed": float(amount), "actual_removed": float(amount) - remain, "details": removed}
                except Exception as e:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                    return {"ok": False, "error": str(e)}
                finally:
                    await conn.close()
            return {"ok": False, "error": f"Unsupported action {action} for resource"}

        if typ in ("building","buildings"):
            if building_template is None:
                return {"ok": False, "error": "building_template parameter required"}
            c = int(count or 1)
            if action == "add":
                return await add_buildings_to_nation(nation_id, building_template, c, int(tier or 1))
            if action == "subtract":
                # simple demolition: remove up to c matching buildings (no refund)
                conn = await get_conn()
                try:
                    # attempt to remove oldest installed matching buildings in nation's provinces
                    cur = await conn.execute("""
                        SELECT pb.rowid as pb_rowid, pb.province_id, pb.building_template
                        FROM province_buildings pb
                        JOIN provinces p ON pb.province_id = p.province_id
                        WHERE p.controller_id = ? AND (pb.building_template = ? OR pb.building_id = ?)
                        ORDER BY pb.rowid ASC
                        LIMIT ?
                    """, (nation_id, building_template, building_template, c))
                    rows = await cur.fetchall()
                    if not rows:
                        await conn.close()
                        return {"ok": False, "error": "No matching buildings found to remove"}
                    removed = []
                    for r in rows:
                        rid = r["pb_rowid"]
                        await conn.execute("DELETE FROM province_buildings WHERE rowid = ?", (rid,))
                        removed.append({"rowid": rid, "province_id": r["province_id"], "template": r.get("building_template")})
                    await conn.commit()
                    return {"ok": True, "removed": removed}
                except Exception as e:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                    return {"ok": False, "error": str(e)}
                finally:
                    await conn.close()
            return {"ok": False, "error": f"Unsupported action {action} for building"}

        if typ == "tech":
            # placeholder - mark tech in a nation_techs table if exists, otherwise record in a simple table
            conn = await get_conn()
            try:
                if action == "add":
                    if not tech_id:
                        return {"ok": False, "error": "tech_id required to add tech"}
                    # create table if missing
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS nation_techs (
                            nation_id TEXT,
                            tech_id TEXT,
                            acquired_at TEXT,
                            PRIMARY KEY (nation_id, tech_id)
                        )
                    """)
                    await conn.execute("INSERT OR IGNORE INTO nation_techs (nation_id, tech_id, acquired_at) VALUES (?, ?, ?)",
                                       (nation_id, tech_id, datetime.datetime.utcnow().isoformat()))
                    await conn.commit()
                    return {"ok": True, "nation_id": nation_id, "tech_added": tech_id}
                if action == "subtract":
                    if not tech_id:
                        return {"ok": False, "error": "tech_id required to remove tech"}
                    await conn.execute("DELETE FROM nation_techs WHERE nation_id = ? AND tech_id = ?", (nation_id, tech_id))
                    await conn.commit()
                    return {"ok": True, "tech_removed": tech_id}
                if action == "list":
                    rows = await _fetch_all(conn, "SELECT tech_id, acquired_at FROM nation_techs WHERE nation_id = ?", (nation_id,))
                    return {"ok": True, "techs": rows}
            except Exception as e:
                try:
                    await conn.rollback()
                except Exception:
                    pass
                return {"ok": False, "error": str(e)}
            finally:
                await conn.close()

        return {"ok": False, "error": f"Unsupported type {typ}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
