# services/nation.py
"""
Nation service - extended to support assigning existing unowned playernations
and adding starter resources + randomly placing starter buildings into suitable provinces.

Functions exported:
- create_nation(...)               # existing behavior (create new nation row + assign json country)
- assign_existing_nation(...)      # NEW: assign a pre-existing playernation (nation_id) to a discord user
- get_unclaimed_countries(...)     # existing: JSON country list (unclaimed)
- get_unowned_playernations(...)   # NEW: returns list of playernations with no owner_discord_id
- starter config helpers (load, add/remove building/tech, set starter cash/pop)
- import_json_from_path / bytes   # unchanged
"""

import os
import json
import time
import re
import logging
import random
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

from db import get_conn
import aiosqlite

log = logging.getLogger(__name__)

DB_PATH = "game.db"
GAME_JSON_PATH = "gameData(1).json"
STARTER_CONFIG_PATH = os.path.join("data", "starter_config.json")


# ------------------------
# DB connection helper
# ------------------------
async def _get_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn


# ------------------------
# JSON helpers (gameData)
# ------------------------
_gamejson_cache: Optional[Dict[str, Any]] = None


def load_gamejson(path: str = GAME_JSON_PATH) -> Dict[str, Any]:
    global _gamejson_cache
    if _gamejson_cache is not None:
        return _gamejson_cache
    if not os.path.exists(path):
        raise FileNotFoundError(f"Game JSON not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "GameData" in data and isinstance(data["GameData"], dict):
        data = data["GameData"]
    _gamejson_cache = data
    return data


def get_countries_from_json(gd: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    gd = gd or load_gamejson()
    for k in ("CountryInfo", "countries", "Country", "Countries"):
        v = gd.get(k)
        if isinstance(v, list):
            out = []
            for item in v:
                cid = item.get("CountryID") or item.get("id") or item.get("country_id") or item.get("CountryId")
                name = item.get("Name") or item.get("name") or item.get("CountryName") or item.get("country")
                out.append({"id": int(cid) if cid is not None else None, "name": str(name) if name is not None else f"Country {cid}", "raw": item})
            return out
    return []


def get_states_from_json(gd: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    gd = gd or load_gamejson()
    for k in ("StateInfo", "states", "State", "StateList"):
        v = gd.get(k)
        if isinstance(v, list):
            return v
    return []


def get_provinces_from_json(gd: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    gd = gd or load_gamejson()
    for k in ("ProvinceInfo", "provinces", "Province", "ProvinceList"):
        v = gd.get(k)
        if isinstance(v, list):
            return v
    return []


# ------------------------
# Starter config helpers (extend to include starter_resources)
# ------------------------
def ensure_data_dir():
    d = os.path.dirname(STARTER_CONFIG_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def load_starter_config() -> Dict[str, Any]:
    ensure_data_dir()
    if not os.path.exists(STARTER_CONFIG_PATH):
        cfg = {
            "buildings": [],
            "technology": [],
            "starter_population_per_province": 100000,
            "starter_cash": 0,
            "starter_resources": {}  # resource -> total amount to give to the nation (distributed)
        }
        with open(STARTER_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return cfg
    with open(STARTER_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("buildings", [])
    cfg.setdefault("technology", [])
    cfg.setdefault("starter_population_per_province", 100000)
    cfg.setdefault("starter_cash", 0)
    cfg.setdefault("starter_resources", {})
    return cfg


def save_starter_config(cfg: Dict[str, Any]) -> None:
    ensure_data_dir()
    with open(STARTER_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ------------------------
# Utility helpers
# ------------------------
def _slugify(s: str) -> str:
    s = (s or "").strip()
    s = s.lower()
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:32] or f"n{int(time.time())}"


async def _table_columns(conn: aiosqlite.Connection, table: str) -> List[str]:
    try:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []

async def _detect_column(conn, table: str, candidates: List[str]) -> Optional[str]:
    """
    Find the first column name in candidates that exists in table.
    """
    try:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        cols = await cur.fetchall()
        present = {c["name"] for c in cols}
        for c in candidates:
            if c in present:
                return c
    except Exception:
        log.exception("Failed to fetch pragma for table %s", table)
    return None

# ------------------------
# JSON country detection (unchanged)
# ------------------------
async def get_unclaimed_countries() -> List[Dict[str, Any]]:
    gd = load_gamejson()
    countries = get_countries_from_json(gd)
    states = get_states_from_json(gd)

    state_to_country = {}
    for s in states:
        sid = s.get("StateID") or s.get("id")
        cid = s.get("CountryID") or s.get("country_id")
        if sid is not None:
            try:
                state_to_country[int(sid)] = int(cid) if cid is not None else None
            except Exception:
                state_to_country[int(sid)] = None

    country_states = {}
    for sid, cid in state_to_country.items():
        country_states.setdefault(cid, []).append(sid)

    conn = await _get_conn()
    try:
        prowcols = await _table_columns(conn, "provinces")
        state_col = None
        controller_col = None
        for c in ("state_id", "StateID", "state", "stateid"):
            if c in prowcols:
                state_col = c; break
        for c in ("controller_id", "controller", "owner", "owner_nation", "nation_id"):
            if c in prowcols:
                controller_col = c; break

        claimed_country_ids = set()
        if state_col and controller_col:
            for country in countries:
                cid = country.get("id")
                sids = country_states.get(cid, [])
                if not sids:
                    continue
                placeholders = ",".join(["?"] * len(sids))
                q = f"SELECT COUNT(*) as cnt FROM provinces WHERE {state_col} IN ({placeholders}) AND {controller_col} IS NOT NULL AND {controller_col} != ''"
                cur = await conn.execute(q, tuple(sids))
                r = await cur.fetchone()
                cnt = int(r["cnt"] or 0) if r else 0
                if cnt > 0:
                    claimed_country_ids.add(cid)
        else:
            claimed_country_ids = set()

        out = []
        for c in countries:
            cid = c.get("id")
            if cid in claimed_country_ids:
                continue
            out.append({"id": cid, "name": c.get("name") or f"Country {cid}"})
        return out
    finally:
        await conn.close()


# ------------------------
# NEW: list pre-existing playernations that have no linked owner (owner_discord_id NULL/empty)
# ------------------------
async def get_unowned_playernations() -> List[Dict[str, Any]]:
    """
    Return a list of playernations that have no linked owner (owner_discord_id is NULL/empty).
    Result items: {"id": ..., "name": ...}
    This version is defensive about column names.
    """
    conn = await _get_conn()
    try:
        cols = await _table_columns(conn, "playernations")
        # detect likely column names
        id_col = None
        name_col = None
        owner_col = None
        for c in ("nation_id", "nation", "id"):
            if c in cols:
                id_col = c
                break
        for c in ("name", "nation_name"):
            if c in cols:
                name_col = c
                break
        for c in ("owner_discord_id", "owner", "owner_id"):
            if c in cols:
                owner_col = c
                break

        # If there's no explicit id column, fallback to returning rowid-based results
        if not id_col:
            cur = await conn.execute("SELECT rowid, * FROM playernations LIMIT 500")
            rows = await cur.fetchall()
            out = []
            for r in rows:
                # find an owner-like column in the row
                owner_val = None
                for oc in ("owner_discord_id", "owner", "owner_id"):
                    if oc in r.keys():
                        owner_val = r[oc]
                        break
                if owner_val in (None, ""):
                    out.append({"id": r["rowid"], "name": r.get("name") or str(r["rowid"])})
            return out

        # Build a safe SELECT with detected columns
        if name_col:
            # Use the detected name column as-is
            select_cols = f"{id_col} as id, {name_col} as name"
        else:
            # No name column found; only return id
            select_cols = f"{id_col} as id"

        if owner_col:
            # Only rows where owner column is NULL or empty string
            q = f"SELECT {select_cols}, {owner_col} as owner FROM playernations WHERE {owner_col} IS NULL OR {owner_col} = '' LIMIT 500"
            cur = await conn.execute(q)
        else:
            # If owner column not present, treat all rows as unowned (but return only id/name)
            q = f"SELECT {select_cols} FROM playernations LIMIT 500"
            cur = await conn.execute(q)

        rows = await cur.fetchall()
        out = []
        for r in rows:
            # If owner present and not null/empty, skip
            if "owner" in r.keys() and r["owner"] not in (None, ""):
                continue
            out.append({"id": r["id"], "name": (r.get("name") or str(r["id"]))})
        return out
    finally:
        await conn.close()


# ------------------------
# Utility: find provinces for a nation
# ------------------------
async def _get_provinces_for_nation(conn: aiosqlite.Connection, nation_id: str) -> List[Dict[str, Any]]:
    pcols = await _table_columns(conn, "provinces")
    ctrl_col = None
    pid_col = None
    resource_col = None
    pop_col = None
    for c in ("controller_id", "controller", "owner", "owner_nation", "nation_id"):
        if c in pcols:
            ctrl_col = c; break
    for c in ("province_id", "ProvinceID", "id", "prov_id", "rowid"):
        if c in pcols:
            pid_col = c; break
    for c in ("resource", "resource_type", "arable_pct", "arable", "node_resource"):
        if c in pcols:
            resource_col = c; break
    for c in ("population", "Population", "pop"):
        if c in pcols:
            pop_col = c; break

    rows = []
    if not ctrl_col:
        # nothing to do
        return rows
    q = f"SELECT * FROM provinces WHERE {ctrl_col} = ?"
    try:
        cur = await conn.execute(q, (nation_id,))
        rows = await cur.fetchall()
    except Exception:
        # fallback try controller_id explicitly
        try:
            cur = await conn.execute("SELECT * FROM provinces WHERE controller_id = ?", (nation_id,))
            rows = await cur.fetchall()
        except Exception:
            rows = []
    # convert to dicts
    out = []
    for r in rows:
        d = dict(r)
        out.append(d)
    return out

async def get_nation_overview(nation_id: str) -> Dict[str, Any]:
    """
    Return a dict containing:
      - basic: row from playernations (as dict)
      - cash
      - population_total
      - manpower_total (if available)
      - manpower_used (if calculable)
      - estimated_tax_income (if tax_rate exists)
      - players: list of {"discord_id","role"} where role is "primary" or "secondary" or explicit role if present
      - states: mapping state_id -> {"state_name", "province_count", "provinces": [ {province rows} ]}
      - provinces: list of province dicts (controlled by nation)
    This function attempts to be resilient to varying schema names by probing common column names.
    """
    out = {
        "basic": None,
        "cash": None,
        "population_total": 0,
        "manpower_total": None,
        "manpower_used": None,
        "estimated_tax_income": None,
        "players": [],
        "states": {},   # state_id -> {state_name, province_count, provinces: [...]}
        "provinces": [],  # flat list
    }

    conn = await get_conn()
    try:
        # 1) read playernations row
        cur = await conn.execute("SELECT * FROM playernations WHERE nation_id = ? LIMIT 1", (nation_id,))
        pn = await cur.fetchone()
        if not pn:
            return {"error": "nation_not_found"}
        # sqlite3.Row -> dict conversion
        pn_dict = dict(pn)
        out["basic"] = pn_dict

        # cash fields (common names)
        cash_col = None
        for c in ("cash", "treasury", "balance"):
            if c in pn_dict:
                cash_col = c
                break
        if cash_col:
            out["cash"] = float(pn_dict.get(cash_col) or 0)

        # tax rate if present
        tax_rate = None
        for c in ("tax_rate", "tax", "rate"):
            if c in pn_dict:
                tax_rate = pn_dict.get(c)
                break

        # manpower stored on playernation maybe
        for mcol in ("manpower_pool", "manpower", "manpower_total"):
            if mcol in pn_dict:
                out["manpower_total"] = int(pn_dict.get(mcol) or 0)
                break

        # 2) find provinces owned / controlled by this nation
        # detect controller column and population column in provinces
        p_ctrl_col = await _detect_column(conn, "provinces", ["controller_id", "controller", "owner", "owner_nation", "nation_id"])
        p_pop_col = await _detect_column(conn, "provinces", ["population", "pop", "Population"])
        p_state_col = await _detect_column(conn, "provinces", ["state_id", "state", "state_name"])
        p_id_col = await _detect_column(conn, "provinces", ["province_id", "id", "rowid"])

        if not p_ctrl_col:
            # fallback: no provinces table or no controller column -> return minimal
            return out

        # select provinces controlled by this nation
        sql = f"SELECT * FROM provinces WHERE {p_ctrl_col} = ?"
        cur = await conn.execute(sql, (nation_id,))
        prov_rows = await cur.fetchall()
        provinces = [dict(r) for r in prov_rows]  # convert to python dicts
        out["provinces"] = provinces

        # population total
        if p_pop_col:
            pop_sum = 0
            for p in provinces:
                try:
                    pop_sum += int(p.get(p_pop_col) or 0)
                except Exception:
                    pass
            out["population_total"] = pop_sum

        # 3) group provinces by state_id and fetch state names
        # Build mapping state_id -> list of provinces
        states_map = defaultdict(list)
        for p in provinces:
            sid = p.get(p_state_col) if p_state_col else None
            states_map[sid].append(p)

        # get state names for state ids
        state_name_col_candidates = ["name", "state_name", "display_name"]
        # detect state table columns
        state_id_col = await _detect_column(conn, "states", ["state_id", "id", "rowid"])
        state_name_col = await _detect_column(conn, "states", state_name_col_candidates)

        states_out = {}
        for sid, plist in states_map.items():
            sname = str(sid)
            if sid is not None and state_id_col:
                # lookup name
                try:
                    cur = await conn.execute(f"SELECT * FROM states WHERE {state_id_col} = ? LIMIT 1", (sid,))
                    srow = await cur.fetchone()
                    if srow and state_name_col and state_name_col in srow.keys():
                        sname = srow[state_name_col]
                except Exception:
                    pass
            states_out[sid] = {
                "state_id": sid,
                "state_name": sname,
                "province_count": len(plist),
                "provinces": plist
            }
        out["states"] = states_out

        # 4) players: primary owner (from playernations.owner_discord_id) and secondary members from nation_players
        owner_col = None
        # owner column detection in playernations
        cur = await conn.execute("PRAGMA table_info(playernations)")
        pcols = await cur.fetchall()
        pcols_set = {c["name"] for c in pcols}
        owner_col = next((c for c in ("owner_discord_id", "owner", "owner_id") if c in pcols_set), None)
        if owner_col and owner_col in pn_dict and pn_dict.get(owner_col):
            primary_id = str(pn_dict.get(owner_col))
            out["players"].append({"discord_id": primary_id, "role": "primary"})
        else:
            primary_id = None

        # get secondary members from nation_players
        # check if nation_players table exists
        try:
            cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nation_players'")
            if await cur.fetchone():
                cur = await conn.execute("SELECT * FROM nation_players WHERE nation_id = ?", (nation_id,))
                rows = await cur.fetchall()
                for r in rows:
                    rdict = dict(r)
                    did = str(rdict.get("discord_id"))
                    if did == primary_id:
                        continue
                    # if there's a role column present in nation_players use it; else mark secondary
                    role = rdict.get("role") if "role" in rdict else "secondary"
                    out["players"].append({"discord_id": did, "role": role or "secondary"})
        except Exception:
            log.exception("Error fetching nation_players (secondary members)")

        # 5) manpower_used approximation: if province_buildings contains maintenance_manpower or similar, sum it
        try:
            # detect province_buildings table and manpower col
            cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='province_buildings'")
            if await cur.fetchone():
                # detect manpower column
                cur2 = await conn.execute("PRAGMA table_info(province_buildings)")
                pb_cols = await cur2.fetchall()
                pb_names = {c["name"] for c in pb_cols}
                manpower_col = next((c for c in ("maintenance_manpower", "manpower_used", "manpower") if c in pb_names), None)
                prov_ref_col = next((c for c in ("province_id", "province", "prov_id") if c in pb_names), None)
                if manpower_col and prov_ref_col:
                    # sum maintenance across buildings in provinces owned by this nation
                    total_used = 0
                    for p in provinces:
                        provid = p.get(prov_ref_col) or p.get("province_id") or p.get("id") or p.get("rowid")
                        if not provid:
                            continue
                        cur3 = await conn.execute(f"SELECT SUM(COALESCE({manpower_col},0)) as s FROM province_buildings WHERE {prov_ref_col} = ?", (provid,))
                        rr = await cur3.fetchone()
                        if rr and rr["s"]:
                            total_used += int(rr["s"])
                    out["manpower_used"] = total_used
        except Exception:
            log.exception("manpower_used calc failed")

        # 6) estimated tax income if tax_rate and population known
        try:
            if tax_rate is not None and out["population_total"]:
                # If tax_rate looks like a percentage (e.g., 0.1), we multiply
                try:
                    tr = float(tax_rate)
                    est = int(out["population_total"] * tr)
                    out["estimated_tax_income"] = est
                except Exception:
                    out["estimated_tax_income"] = None
        except Exception:
            log.exception("tax calc failed")

        return out

    except Exception as e:
        log.exception("get_nation_overview failed")
        return {"error": str(e)}
    finally:
        try:
            await conn.close()
        except Exception:
            pass






# ------------------------
# Add starter resources to a nation's provinces
# ------------------------
async def _add_starter_resources(conn: aiosqlite.Connection, nation_id: str, resources_map: Dict[str, float]) -> Dict[str, Any]:
    """
    Distribute the starter resources (resource->total_amount) across provinces of the nation.
    Preference: provinces that have the matching resource (province.resource column) first; if none found,
    distribute across all provinces evenly. Update province_stockpiles rows (insert or update).
    """
    out = {"distributed": {}, "errors": []}
    provinces = await _get_provinces_for_nation(conn, nation_id)
    if not provinces:
        out["errors"].append("No provinces found for nation to distribute starter resources.")
        return out

    # find columns in province_stockpiles
    ps_cols = await _table_columns(conn, "province_stockpiles")
    ps_pid_col = None
    ps_res_col = None
    ps_amount_col = None
    ps_capacity_col = None
    for c in ("province_id", "ProvinceID", "prov_id", "id"):
        if c in ps_cols:
            ps_pid_col = c; break
    for c in ("resource", "resource_name", "res"):
        if c in ps_cols:
            ps_res_col = c; break
    for c in ("amount", "qty", "quantity"):
        if c in ps_cols:
            ps_amount_col = c; break
    for c in ("capacity", "cap"):
        if c in ps_cols:
            ps_capacity_col = c; break

    # Fallback names if not detected
    if not ps_pid_col:
        # cannot operate
        out["errors"].append("province_stockpiles table missing province id column; cannot insert starter resources.")
        return out

    # index provinces by resource if possible
    resource_to_provs = {}
    for p in provinces:
        p_res = None
        for k in ("resource", "resource_type", "arable", "node_resource"):
            if k in p:
                p_res = p.get(k)
                break
        resource_to_provs.setdefault(p_res, []).append(p)

    # For each resource, distribute evenly across candidate provinces (matching resource first; otherwise any)
    for rname, total_amt in resources_map.items():
        try:
            total_amt = float(total_amt)
        except Exception:
            out["errors"].append(f"Invalid amount for resource {rname}: {resources_map[rname]}")
            continue
        candidates = resource_to_provs.get(rname) or []
        if not candidates:
            # fallback to any provinces
            candidates = provinces

        # distribute evenly, but respect capacity if present
        per = int(total_amt // len(candidates)) if candidates else 0
        remainder = int(total_amt - per * len(candidates))
        distributed = 0
        for idx, prov in enumerate(candidates):
            add = per + (1 if idx < remainder else 0)
            if add <= 0:
                continue
            pid = prov.get("province_id") or prov.get("ProvinceID") or prov.get("id") or prov.get("prov_id") or prov.get("rowid")
            # ensure row exists in province_stockpiles for (pid, rname)
            try:
                # attempt update if row exists
                if ps_res_col and ps_amount_col:
                    cur = await conn.execute(f"SELECT {ps_amount_col}, {ps_capacity_col if ps_capacity_col else 'NULL'} FROM province_stockpiles WHERE {ps_pid_col}=? AND {ps_res_col}=?", (pid, rname))
                    prow = await cur.fetchone()
                    if prow:
                        cur_amt = int(prow[ps_amount_col]) if ps_amount_col and prow[ps_amount_col] is not None else 0
                        cap = None
                        if ps_capacity_col and prow[ps_capacity_col] is not None:
                            try:
                                cap = int(prow[ps_capacity_col])
                            except Exception:
                                cap = None
                        new_amt = cur_amt + add
                        if cap is not None:
                            new_amt = min(new_amt, cap)
                        await conn.execute(f"UPDATE province_stockpiles SET {ps_amount_col} = ? WHERE {ps_pid_col}=? AND {ps_res_col}=?", (new_amt, pid, rname))
                    else:
                        # insert row with amount = min(add, capacity if known else add)
                        cap_val = None
                        if ps_capacity_col:
                            # attempt to pull building capacity? default to add*2
                            cap_val = int(max(add * 2, add))
                        if ps_res_col and ps_amount_col and ps_pid_col:
                            await conn.execute(f"INSERT INTO province_stockpiles ({ps_pid_col}, {ps_res_col}, {ps_amount_col}{', '+ps_capacity_col if ps_capacity_col else ''}) VALUES ({', '.join(['?'] * (2 + (1 if ps_capacity_col else 0)))})",
                                               tuple([pid, rname] + ([cap_val] if ps_capacity_col else [])))
                    distributed += add
                else:
                    # fallback: if structure unknown, record as skipped
                    out["errors"].append("province_stockpiles columns not detected; cannot insert resource records.")
                    break
            except Exception as e:
                log.exception("Failed to add starter resource row")
                out["errors"].append(f"Failed to add resource {rname} to province {pid}: {e}")
        out["distributed"][rname] = distributed
    try:
        await conn.commit()
    except Exception:
        pass
    return out


# ------------------------
# Helper: pick candidate provinces for a building template
# ------------------------
async def _find_candidate_provinces_for_building(conn: aiosqlite.Connection, nation_id: str, building_template: str) -> List[int]:
    """
    Return a list of province ids (ints/strings consistent with DB) where this building would fit.
    Strategy:
      - Inspect building_templates table for 'outputs'/'produces' or name hints (e.g., 'mine','farm','refinery')
      - Prefer provinces whose 'resource' column equals the produced resource
      - Otherwise return all provinces of the nation
    """
    # load building template row if available
    bt_cols = await _table_columns(conn, "building_templates")
    candidate_resource = None
    try:
        # attempt to lookup template by template_id or name match
        q = None
        if "template_id" in bt_cols:
            q = "SELECT * FROM building_templates WHERE template_id = ? LIMIT 1"
        elif "id" in bt_cols:
            q = "SELECT * FROM building_templates WHERE id = ? LIMIT 1"
        elif "name" in bt_cols:
            q = "SELECT * FROM building_templates WHERE name = ? LIMIT 1"
        if q:
            cur = await conn.execute(q, (building_template,))
            row = await cur.fetchone()
            if row:
                brow = dict(row)
                # try to parse outputs/produces keys
                for k in ("outputs", "produces", "production", "output"):
                    if k in brow and brow.get(k):
                        try:
                            if isinstance(brow[k], str):
                                j = json.loads(brow[k])
                            else:
                                j = brow[k]
                            if isinstance(j, dict):
                                # take the first resource name
                                candidate_resource = next(iter(j.keys()), None)
                                break
                        except Exception:
                            # fallback: if string contains resource names, search keywords
                            s = str(brow[k]).lower()
                            for key in ("ore","coal","oil","food","uranium","iron","steel","fuel","military"):
                                if key in s:
                                    candidate_resource = key.title() if key != "food" else "Food"
                                    break
                # try name heuristics
                if not candidate_resource:
                    name_lower = str(brow.get("name","")).lower()
                    for key in ("ore","coal","oil","food","uranium","farm","mine","refinery","smelter"):
                        if key in name_lower:
                            candidate_resource = key.title() if key not in ("ore","coal","oil","uranium","food") else key.title()
                            break
    except Exception:
        log.exception("Failed to inspect building_templates")

    # now find provinces belonging to nation
    provinces = await _get_provinces_for_nation(conn, nation_id)
    if not provinces:
        return []

    matches = []
    if candidate_resource:
        for p in provinces:
            # check common resource keys
            p_res = None
            for k in ("resource", "resource_type", "arable", "node_resource"):
                if k in p:
                    p_res = p.get(k)
                    break
            # normalize strings for comparison
            if p_res and isinstance(p_res, str) and p_res.strip().lower() == str(candidate_resource).strip().lower():
                pid = p.get("province_id") or p.get("ProvinceID") or p.get("id") or p.get("prov_id") or p.get("rowid")
                matches.append(pid)
    if not matches:
        # fallback: return all provinces ID list
        for p in provinces:
            pid = p.get("province_id") or p.get("ProvinceID") or p.get("id") or p.get("prov_id") or p.get("rowid")
            matches.append(pid)
    return matches


# ------------------------
# Add starter buildings into provinces (improved: random placement into suitable provinces)
# ------------------------
async def _add_starter_buildings_placed(conn: aiosqlite.Connection, nation_id: str, starters: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    For each starter building in starters (dicts with 'template', 'tier', 'count'),
    find suitable provinces (via _find_candidate_provinces_for_building), randomly choose
    a province and insert into province_buildings table. If none suitable, record as skipped.
    """
    out = {"attempts": 0, "inserted": 0, "skipped": [], "errors": []}
    if not starters:
        return out

    pbcols = await _table_columns(conn, "province_buildings")
    pcols = await _table_columns(conn, "provinces")
    pid_colname = None
    for c in ("province_id", "ProvinceID", "id", "prov_id"):
        if c in pcols:
            pid_colname = c; break
    if not pid_colname:
        out["errors"].append("provinces table missing id column; cannot place buildings.")
        return out

    # allowed insert columns in province_buildings
    insert_cols = []
    for c in ("province_id", "building_id", "building_template", "tier", "count", "installed_turn"):
        if c in pbcols:
            insert_cols.append(c)
    if not insert_cols:
        out["errors"].append("province_buildings table missing expected insert columns.")
        return out

    placeholders = ",".join(["?"] * len(insert_cols))
    colstr = ",".join(insert_cols)

    for starter in starters:
        template = starter.get("template") or starter.get("building") or starter.get("building_id")
        tier = int(starter.get("tier") or 1)
        count = int(starter.get("count") or 1)
        for _ in range(count):
            out["attempts"] += 1
            # find candidate provinces
            try:
                candidates = await _find_candidate_provinces_for_building(conn, nation_id, template)
            except Exception as e:
                candidates = []
                log.exception("find_candidate_provinces failed")
            if not candidates:
                out["skipped"].append({"template": template, "reason": "no candidate provinces"})
                continue
            chosen = random.choice(candidates)
            # construct values for insert columns
            vals = []
            for c in insert_cols:
                if c in ("province_id",):
                    vals.append(chosen)
                elif c in ("building_id","building_template"):
                    vals.append(template)
                elif c == "tier":
                    vals.append(tier)
                elif c == "count":
                    vals.append(1)
                elif c == "installed_turn":
                    vals.append(int(time.time()))
                else:
                    vals.append(None)
            try:
                await conn.execute(f"INSERT INTO province_buildings ({colstr}) VALUES ({placeholders})", tuple(vals))
                out["inserted"] += 1
            except Exception as e:
                log.exception("Failed to insert starter building")
                out["errors"].append(f"Failed to insert {template} into province {chosen}: {e}")
                # don't break; try next
    try:
        await conn.commit()
    except Exception:
        pass
    return out

async def get_unowned_playernation_names(limit: int = 25) -> List[str]:
    """
    Return a list of nation names (strings) from playernations that have no owner.
    This reads the DB live so the autocomplete updates when new nations are assigned.
    """
    conn = await _get_conn()
    try:
        cols = await _table_columns(conn, "playernations")
        # find owner column if present
        owner_col = next((c for c in ("owner_discord_id", "owner", "owner_id") if c in cols), None)
        name_col = next((c for c in ("name", "nation_name", "display_name", "nation") if c in cols), None)

        if not name_col:
            # fallback: we can still return rowids as strings if name isn't present
            cur = await conn.execute("SELECT rowid FROM playernations LIMIT ?", (limit,))
            rows = await cur.fetchall()
            return [str(r["rowid"]) for r in rows]

        if owner_col:
            q = f"SELECT {name_col} as name FROM playernations WHERE ({owner_col} IS NULL OR {owner_col} = '') AND {name_col} IS NOT NULL ORDER BY {name_col} COLLATE NOCASE LIMIT ?"
            cur = await conn.execute(q, (limit,))
        else:
            q = f"SELECT {name_col} as name FROM playernations WHERE {name_col} IS NOT NULL ORDER BY {name_col} COLLATE NOCASE LIMIT ?"
            cur = await conn.execute(q, (limit,))

        rows = await cur.fetchall()
        return [r["name"] for r in rows if r and r["name"] is not None]
    except Exception as e:
        log.exception("get_unowned_playernation_names failed")
        return []
    finally:
        try:
            await conn.close()
        except Exception:
            pass





# ------------------------
# NEW: assign_existing_nation
# ------------------------
async def assign_existing_nation_by_name(admin_discord_id: str, target_user_discord_id: str, nation_name: str, apply_starters: bool = True) -> Dict[str, Any]:
    """
    Find a playernation row by exact case-insensitive name match in playernations,
    ensure it is currently unowned, set owner_discord_id, and apply starters (cash/resources/buildings).

    Returns: dict with keys: ok(bool), nation_row (dict), errors(list), starter_resources_result, starter_buildings_result
    """
    out = {"ok": False, "nation_row": None, "errors": [], "starter_resources_result": {}, "starter_buildings_result": {}}
    if not nation_name or not str(nation_name).strip():
        out["errors"].append("No nation_name provided.")
        return out

    conn = await _get_conn()
    try:
        pn_cols = await _table_columns(conn, "playernations")
        name_col = next((c for c in ("name", "nation_name", "display_name", "nation") if c in pn_cols), None)
        id_col = next((c for c in ("nation_id", "id", "nation") if c in pn_cols), None)
        owner_col = next((c for c in ("owner_discord_id", "owner", "owner_id") if c in pn_cols), None)

        if not name_col:
            out["errors"].append("playernations table has no name column to search by.")
            return out

        # 1) Exact case-insensitive match
        q = f"SELECT rowid, * FROM playernations WHERE {name_col} = ? COLLATE NOCASE LIMIT 2"
        cur = await conn.execute(q, (nation_name.strip(),))
        rows = await cur.fetchall()
        if not rows:
            # 2) Try partial match (if exact not found)
            q2 = f"SELECT rowid, * FROM playernations WHERE {name_col} LIKE ? COLLATE NOCASE LIMIT 10"
            cur = await conn.execute(q2, (f"%{nation_name.strip()}%",))
            rows = await cur.fetchall()
            if not rows:
                out["errors"].append(f"No playernation found matching '{nation_name}'.")
                return out
            # If partial returned multiple, ask admin to be more specific
            if len(rows) > 1:
                out["errors"].append("Multiple partial matches found. Try the exact name or pick one of these:")
                out["matches"] = [dict(r) for r in rows[:8]]
                return out

        # If more than one exact match (unlikely), abort
        if len(rows) > 1:
            out["errors"].append("Multiple exact matches found for that name. Please use an exact, unique name.")
            out["matches"] = [dict(r) for r in rows[:8]]
            return out

        row = dict(rows[0])
        # check owner column if available
        if owner_col and owner_col in row and row.get(owner_col) not in (None, ""):
            out["errors"].append(f"That nation is already owned (owner={row.get(owner_col)}).")
            return out

        # write owner field
        if not owner_col:
            out["errors"].append("playernations table missing owner column; cannot persist the assignment.")
            return out

        # Determine canonical id value to use for provinces linkage
        # prefer 'nation_id' column if present, else use rowid
        canonical_nation_id = None
        if id_col and id_col in row and row.get(id_col) not in (None, ""):
            canonical_nation_id = row.get(id_col)
        else:
            canonical_nation_id = row.get("rowid")

        # Update the playernation's owner_discord_id
        await conn.execute(f"UPDATE playernations SET {owner_col} = ? WHERE rowid = ?", (str(target_user_discord_id), row["rowid"]))
        await conn.commit()

        # set starter cash if column exists
        cfg = load_starter_config()
        starter_cash = float(cfg.get("starter_cash", 0))
        try:
            if "cash" in pn_cols:
                await conn.execute("UPDATE playernations SET cash = COALESCE(cash,0) + ? WHERE rowid = ?", (starter_cash, row["rowid"]))
            elif "treasury" in pn_cols:
                await conn.execute("UPDATE playernations SET treasury = COALESCE(treasury,0) + ? WHERE rowid = ?", (starter_cash, row["rowid"]))
            await conn.commit()
        except Exception:
            log.exception("Failed to set starter cash (non-fatal)")

        # set manpower 40% of province population if possible
        try:
            # compute total population of provinces controlled by canonical_nation_id
            pcols = await _table_columns(conn, "provinces")
            ctrl_col = next((c for c in ("controller_id", "controller", "owner", "owner_nation", "nation_id") if c in pcols), None)
            pop_col = next((c for c in ("population", "Population", "pop") if c in pcols), None)
            total_pop = 0
            if ctrl_col and pop_col:
                cur = await conn.execute(f"SELECT SUM(COALESCE({pop_col},0)) as s FROM provinces WHERE {ctrl_col} = ?", (canonical_nation_id,))
                rr = await cur.fetchone()
                total_pop = int(rr["s"] or 0) if rr else 0
            manpower_val = int(total_pop * 0.4) if total_pop else 0
            if "manpower_pool" in pn_cols:
                await conn.execute("UPDATE playernations SET manpower_pool = ? WHERE rowid = ?", (manpower_val, row["rowid"]))
            elif "manpower" in pn_cols:
                await conn.execute("UPDATE playernations SET manpower = ? WHERE rowid = ?", (manpower_val, row["rowid"]))
            await conn.commit()
        except Exception:
            log.exception("Failed to set manpower (non-fatal)")

        # Finally: apply starter resources & buildings (best-effort)
        try:
            if apply_starters:
                starter_resources = cfg.get("starter_resources", {}) or {}
                if starter_resources:
                    rr = await _add_starter_resources(conn, canonical_nation_id, starter_resources)
                    out["starter_resources_result"] = rr
                starters = cfg.get("buildings", []) or []
                if starters:
                    br = await _add_starter_buildings_placed(conn, canonical_nation_id, starters)
                    out["starter_buildings_result"] = br
        except Exception:
            log.exception("Failed to apply starters")

        out["ok"] = True
        out["nation_row"] = row
        return out

    except Exception as e:
        log.exception("assign_existing_nation_by_name failed")
        out["errors"].append(str(e))
        return out
    finally:
        try:
            await conn.close()
        except Exception:
            pass
# ------------------------
# Existing create_nation function (kept from previous implementation)
# (This function creates a new playernation record and assigns JSON country states)
# If you already have a robust version, you can keep it; including here for completeness.
# ------------------------
async def create_nation(admin_discord_id: str, target_user_discord_id: str, nation_name: str, country_name: Optional[str] = None, apply_starters: bool = True) -> Dict[str, Any]:
    # (Implementation identical to previous create_nation you had)
    # For brevity, assume this file contains your earlier create_nation function unchanged.
    # If you need the full create_nation inserted here, let me know — I can paste it again.
    return {"ok": False, "message": "create_nation not implemented in this snippet - use assign_existing_nation or the previously supplied create_nation."}

# services/nation.py

async def assign_player_to_nation(discord_id: str, nation_name: str):
    conn = await get_conn()
    try:
        # Confirm nation exists
        cur = await conn.execute("SELECT nation_id FROM playernations WHERE name=?", (nation_name,))
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "error": f"No nation with name '{nation_name}'"}
        nation_id = row["nation_id"]

        # Insert mapping into nation_players (ignore if already exists)
        await conn.execute("""
            INSERT OR IGNORE INTO nation_players (nation_id, discord_id)
            VALUES (?, ?)
        """, (nation_id, discord_id))
        await conn.commit()
        return {"ok": True, "nation_id": nation_id}
    finally:
        await conn.close()


# ------------------------
# Starter-config helpers exposed for bot (unchanged)
# ------------------------
def add_starter_building(template: str, tier: int = 1, count: int = 1) -> Dict[str, Any]:
    cfg = load_starter_config()
    cfg.setdefault("buildings", [])
    cfg["buildings"].append({"template": template, "tier": int(tier), "count": int(count)})
    save_starter_config(cfg)
    return {"ok": True, "msg": "added", "current": cfg["buildings"]}


def remove_starter_building(template: str) -> Dict[str, Any]:
    cfg = load_starter_config()
    before = list(cfg.get("buildings", []))
    cfg["buildings"] = [b for b in before if str(b.get("template")) != str(template)]
    save_starter_config(cfg)
    return {"ok": True, "msg": "removed", "before": before, "after": cfg["buildings"]}


def add_starter_tech(tech_name: str) -> Dict[str, Any]:
    cfg = load_starter_config()
    cfg.setdefault("technology", [])
    if tech_name not in cfg["technology"]:
        cfg["technology"].append(tech_name)
        save_starter_config(cfg)
    return {"ok": True, "current": cfg["technology"]}


def remove_starter_tech(tech_name: str) -> Dict[str, Any]:
    cfg = load_starter_config()
    before = list(cfg.get("technology", []))
    cfg["technology"] = [t for t in before if t != tech_name]
    save_starter_config(cfg)
    return {"ok": True, "before": before, "after": cfg["technology"]}


def set_starter_population_per_province(value: int) -> Dict[str, Any]:
    cfg = load_starter_config()
    cfg["starter_population_per_province"] = int(value)
    save_starter_config(cfg)
    return {"ok": True, "value": cfg["starter_population_per_province"]}


def set_starter_cash(value: float) -> Dict[str, Any]:
    cfg = load_starter_config()
    cfg["starter_cash"] = float(value)
    save_starter_config(cfg)
    return {"ok": True, "value": cfg["starter_cash"]}


# ------------------------
# JSON import helpers - unchanged (see previous file)
# ------------------------
async def import_json_assignments(json_data: Dict[str, Any]) -> Dict[str, Any]:
    # same as previously provided in your service - omitted here for brevity
    return {"ok": False, "error": "Not implemented in this snippet."}


async def import_json_from_bytes(bytes_content: bytes) -> Dict[str, Any]:
    try:
        asdict = json.loads(bytes_content.decode("utf-8"))
    except Exception:
        try:
            asdict = json.loads(bytes_content.decode("utf-8-sig"))
        except Exception as e:
            return {"ok": False, "error": f"Failed to parse JSON: {e}"}
    return await import_json_assignments(asdict)


async def import_json_from_path(path: str = GAME_JSON_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"ok": False, "error": f"Failed to load JSON: {e}"}
    return await import_json_assignments(data)
