# services/build.py
import json
from typing import List, Dict, Any
from db import get_conn
import services.stockpile as stockpile
import discord
import datetime
from db import get_conn 
# services/build.py (append these functions)
import json
from db import get_conn  # adjust import if your db helper is named differently
import logging
log = logging.getLogger(__name__)

def _row_to_dict_safe(row):
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return {}

async def demolish_by_spec(nation_id: str, state_id: str, building_identifier: str, tier: int = None) -> dict:
    """
    Demolish one installed building owned by nation_id within state_id that matches building_identifier and tier.
    building_identifier can be an id (string/number) or partial name. Returns dict with ok/error and details.
    Defensive: doesn't assume specific province_buildings column names.
    """
    conn = await get_conn(); cur = await conn.cursor()

    try:
        # 1) fetch installed buildings in that state owned by the nation (don't filter by building columns here)
        await cur.execute("""
            SELECT pb.rowid AS pb_rowid, pb.*, p.province_id, p.state_id, p.controller_id, p.node_strength
            FROM province_buildings pb
            JOIN provinces p ON pb.province_id = p.province_id
            WHERE p.controller_id = ? AND p.state_id = ?
            ORDER BY COALESCE(p.node_strength,0) DESC
        """, (nation_id, state_id))
        rows = await cur.fetchall()
    except Exception as e:
        await conn.close()
        log.exception("demolish_by_spec: initial fetch failed")
        return {"error": f"DB error during lookup: {e}"}

    if not rows:
        await conn.close()
        return {"error": "No installed buildings found in that state owned by your nation."}

    # convert rows to dicts
    candidates = [_row_to_dict_safe(r) for r in rows]

    # 2) try to find the best matching candidate in Python
    def matches_identifier(candidate: dict, identifier: str) -> int:
        """
        Return a score for how well candidate matches identifier.
        Higher score means better match. 0 means no match.
        """
        score = 0
        # numeric match attempt
        try:
            if identifier is not None and str(candidate.get("building_id") or candidate.get("building") or candidate.get("building_template") or "").lower() == str(identifier).lower():
                score += 100
        except Exception:
            pass
        # exact name matches (if column exists)
        for key in ("building_name", "name", "building_template", "building", "type", "template_id"):
            try:
                val = candidate.get(key)
                if val is None:
                    continue
                sval = str(val).lower()
                if str(identifier).lower() == sval:
                    score += 90
                elif str(identifier).lower() in sval:
                    score += 50
            except Exception:
                continue
        return score

    # apply matching score
    best = None
    best_score = -1
    for c in candidates:
        sc = matches_identifier(c, building_identifier)
        # tier matching adds score
        c_tier = None
        for tk in ("tier", "level"):
            if tk in c and c[tk] is not None:
                try:
                    c_tier = int(c[tk])
                except Exception:
                    c_tier = None
        if tier is not None and c_tier is not None and int(tier) == int(c_tier):
            sc += 20

        # prefer higher node_strength as tie-breaker
        node_strength = float(c.get("node_strength") or c.get("node_strength", 0) or 0)
        sc += node_strength * 0.01

        if sc > best_score:
            best_score = sc
            best = c

    # if no match scored, pick the first candidate (strongest province)
    if best is None:
        best = candidates[0]

    # Now we have candidate `best` to remove
    pb_rowid = best.get("pb_rowid") or best.get("rowid") or best.get("id")
    province_id = best.get("province_id")
    building_name = best.get("building_name") or best.get("building_template") or best.get("building") or str(building_identifier or "Unknown")
    found_tier = best.get("tier") or tier or best.get("level") or None

    # Attempt to find maintenance_manpower from building_templates (optional)
    maintenance_manpower = 0
    try:
        # try likely keys for a template id stored on the installed row
        tpl_candidates = [best.get("building_template"), best.get("template_id"), best.get("building_id"), best.get("building")]
        tpl_candidates = [t for t in tpl_candidates if t]
        if tpl_candidates:
            # check each candidate for a template row
            for tpl in tpl_candidates:
                try:
                    await cur.execute("SELECT maintenance_manpower FROM building_templates WHERE template_id=? OR building_id=? OR name=? LIMIT 1", (tpl, tpl, tpl))
                    bt = await cur.fetchone()
                    if bt and "maintenance_manpower" in bt.keys() and bt["maintenance_manpower"] is not None:
                        maintenance_manpower = int(bt["maintenance_manpower"] or 0)
                        break
                except Exception:
                    continue
    except Exception:
        maintenance_manpower = 0

    # Delete the installed building row from province_buildings using rowid
    try:
        if pb_rowid:
            await cur.execute("DELETE FROM province_buildings WHERE rowid = ?", (pb_rowid,))
        else:
            # fallback: delete by province_id + a building column if any available
            possible_cols = ["building_template", "building_id", "building"]
            deleted = False
            for col in possible_cols:
                if col in best:
                    try:
                        await cur.execute(f"DELETE FROM province_buildings WHERE province_id = ? AND {col} = ? LIMIT 1", (province_id, best.get(col)))
                        deleted = True
                        break
                    except Exception:
                        continue
            if not deleted:
                # last resort: delete a single row in that province
                await cur.execute("DELETE FROM province_buildings WHERE province_id = ? LIMIT 1", (province_id,))
        # attempt to decrement province.manpower_used if that column exists
        try:
            await cur.execute("SELECT manpower_used FROM provinces WHERE province_id = ?", (province_id,))
            pm = await cur.fetchone()
            if pm and "manpower_used" in pm.keys():
                cur_man = int(pm["manpower_used"] or 0)
                new_man = max(0, cur_man - (maintenance_manpower or 0))
                await cur.execute("UPDATE provinces SET manpower_used = ? WHERE province_id = ?", (new_man, province_id))
        except Exception:
            # ignore if column missing
            pass

        await conn.commit(); await conn.close()
        return {"ok": True, "removed": building_name, "province_id": province_id, "tier": found_tier}
    except Exception as e:
        try:
            await conn.rollback()
        except Exception:
            pass
        await conn.close()
        log.exception("demolish_by_spec: delete failed")
        return {"error": f"Failed to demolish building: {e}"}

def build_state_embed(info: dict) -> discord.Embed:
    """
    Build a state-level embed with production, consumption, net, stockpiles, buildings (aggregated).
    Does not show internal row ids.
    """
    emb = discord.Embed(title=f"State — {info.get('name')}", color=0x1ABC9C)
    emb.add_field(name="Provinces (your)", value=str(info.get("provinces_count", 0)), inline=True)
    emb.add_field(name="Population", value=f"{info.get('population_total'):,}", inline=True)
    emb.add_field(name="Manpower used", value=f"{info.get('manpower_used'):,}", inline=True)
    emb.add_field(name="Est. tax income / turn", value=f"${int(info.get('estimated_tax_income',0)):,}", inline=True)

    # stockpiles
    stocks = info.get("stockpiles", {})
    if stocks:
        lines = [f"{r}: {int(v['amount']):,}/{int(v['capacity']):,}" for r, v in stocks.items()]
        emb.add_field(name="Stockpiles", value="\n".join(lines), inline=False)

    # production / consumption / net
    produced = info.get("produced", {})
    consumed = info.get("consumed", {})
    net = info.get("net", {})
    if produced or consumed:
        prod_lines = []
        keys = sorted(set(list(produced.keys()) + list(consumed.keys())), key=lambda x: (-produced.get(x,0), x))
        for k in keys:
            p = int(produced.get(k,0))
            c = int(consumed.get(k,0))
            n = int(net.get(k,0))
            prod_lines.append(f"{k}: +{p} / -{c} => net {n}")
        emb.add_field(name="Production (per turn)", value="\n".join(prod_lines), inline=False)

    # buildings aggregated (no per-province listing)
    buildings = info.get("buildings", [])
    if buildings:
        lines = []
        for b in buildings:
            name = b.get("building_name") or b.get("building_id")
            count = int(b.get("count") or 0)
            tier = b.get("tier")
            if tier:
                lines.append(f"{name} — tier {tier} ×{count}")
            else:
                lines.append(f"{name} ×{count}")
        emb.add_field(name="Buildings (aggregated)", value="\n".join(lines[:40]), inline=False)

    return emb

async def find_buildings_aggregated(nation_id: str, building_query: str):
    """
    Return aggregated results: per state, per building template, how many installed.
    Returns list of dicts: {state_name, state_id, building_name, building_id, count}
    """
    conn = await get_conn(); cur = await conn.cursor()
    q = "%" + building_query.lower() + "%"
    # Find building template matches
    await cur.execute("SELECT id FROM building_templates WHERE LOWER(id) LIKE ? OR LOWER(name) LIKE ? LIMIT 50", (q, q))
    matches = await cur.fetchall()
    if not matches:
        await conn.close(); return []
    ids = [m["id"] for m in matches]
    # Find aggregated installed counts per state + building
    placeholders = ",".join("?" for _ in ids)
    sql = f"""
        SELECT s.state_id, s.name as state_name, bt.id as building_id, bt.name as building_name, COUNT(*) as cnt
        FROM province_buildings pb
        JOIN provinces p ON pb.province_id = p.province_id
        JOIN states s ON p.state_id = s.state_id
        JOIN building_templates bt ON bt.id = pb.building_id
        WHERE p.controller_id=? AND pb.building_id IN ({placeholders})
        GROUP BY s.state_id, bt.id
        ORDER BY s.name, bt.name
        LIMIT 200
    """
    await cur.execute(sql, (nation_id, *ids))
    rows = await cur.fetchall(); await conn.close()
    return [dict(r) for r in rows]

def build_findbuildings_embed(agg_rows: list, total_count: int, query: str) -> discord.Embed:
    """
    Build nicer embed for findbuildings aggregated results (state / building / count).
    """
    emb = discord.Embed(title=f"Find Buildings — \"{query}\"", color=0x9B59B6)
    if not agg_rows:
        emb.description = "No buildings found."
        return emb
    # group by state for a nicer presentation
    grouped = {}
    for r in agg_rows:
        state = r.get("state_name") or r.get("state_id")
        grouped.setdefault(state, []).append(r)
    for state, items in grouped.items():
        lines = []
        for it in items:
            lines.append(f"**{it['building_name']}** — {int(it['cnt'])} installed")
        emb.add_field(name=state, value="\n".join(lines), inline=False)
    if total_count and total_count > len(agg_rows):
        emb.set_footer(text=f"Showing {len(agg_rows)} aggregated rows.")
    return emb

    """
    Build an embed listing results for /findbuildings.
    rows: list of dicts {state_name, province_name, building_name, tier, count, installed_id}
    total_count: total number of matched entries (for display)
    query: the search string used
    """
    emb = discord.Embed(title=f"Find Buildings — \"{query}\" ({total_count} matches)", color=0x9B59B6)
    if not rows:
        emb.description = "No buildings found."
        return emb
    # show up to 40 entries
    lines = []
    for r in rows[:40]:
        name = r.get("building_name") or r.get("building_id")
        state = r.get("state_name") or r.get("state_id")
        prov = r.get("province_name") or r.get("province_id")
        tier = r.get("tier", 1)
        count = r.get("count", 1)
        iid = r.get("installed_id", "")
        lines.append(f"**{name}** — {state} / {prov} — tier {tier} ×{count} (id:{iid})")
    emb.add_field(name="Locations (first 40)", value="\n".join(lines), inline=False)
    if total_count > len(rows):
        emb.set_footer(text=f"Showing {len(rows)} of {total_count} matches")
    return emb
# Return list of owned states for a nation (for autocomplete)
async def owned_states_for_nation(nation_id: str, prefix: str = "") -> List[Dict[str, str]]:
    conn = await get_conn(); cur = await conn.cursor()
    q = """
    SELECT s.state_id, s.name, COUNT(p.province_id) AS provinces
    FROM states s
    JOIN provinces p ON p.state_id = s.state_id
    WHERE p.controller_id = ?
    GROUP BY s.state_id, s.name
    HAVING provinces > 0
    ORDER BY s.name
    """
    await cur.execute(q, (nation_id,))
    rows = await cur.fetchall(); await conn.close()
    out = []
    pref = (prefix or "").lower()
    for r in rows:
        sid = r["state_id"]; name = r["name"]
        label = f"{name} ({r['provinces']} provs)"
        if not pref or pref in label.lower() or pref in sid.lower():
            out.append({"id": sid, "label": label})
            if len(out) >= 25:
                break
    return out

async def available_buildings(prefix: str = "") -> List[Dict[str, str]]:
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("SELECT id, name FROM building_templates ORDER BY name")
    rows = await cur.fetchall(); await conn.close()
    out = []
    pref = (prefix or "").lower()
    for r in rows:
        bid = r["id"]; name = r["name"]
        label = f"{name} ({bid})"
        if not pref or pref in name.lower() or pref in bid.lower():
            out.append({"id": bid, "label": label})
            if len(out) >= 25:
                break
    return out

def _row_to_dict_safe(row):
    """Convert sqlite row to dict safely."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return {}

async def get_build_queue(nation_id: str):
    """
    Return pending builds for a nation from state_builds.
    Defensive: selects sb.* and left-joins states + building_templates with a flexible join condition
    so it won't fail if your schema uses building_id or template_id.
    """
    conn = await get_conn()
    cur = await conn.cursor()

    # select all state_builds columns and try to attach state name and building template name
    await cur.execute("""
        SELECT sb.*, s.name AS state_name,
               bt.name AS building_name
        FROM state_builds sb
        LEFT JOIN states s ON sb.state_id = s.state_id
        LEFT JOIN building_templates bt ON (bt.id = sb.building_id)
        WHERE sb.nation_id = ? AND sb.status = 'pending'
        ORDER BY COALESCE(sb.complete_turn, 999999) ASC
    """, (nation_id,))
    rows = await cur.fetchall()
    await conn.close()

    out = []
    for r in rows:
        d = _row_to_dict_safe(r)
        # normalize common fields with safe fallbacks
        d["state_name"] = d.get("state_name") or d.get("state_id") or ""
        # building identifier/fallbacks
        d["building_id"] = d.get("building_id") or d.get("building") or d.get("building_template")
        d["building_name"] = d.get("building_name") or str(d.get("building_id") or "Unknown")
        # possible started turn name variants
        d["started_turn"] = d.get("started_turn") or d.get("started turn") or d.get("start_turn") or None
        d["complete_turn"] = d.get("complete_turn") or d.get("complete turn") or None
        # ensure tier exists
        d["tier"] = d.get("tier") or 1
        # parse reserved_json if present
        reserved_raw = d.get("reserved_json") or d.get("reserved") or None
        try:
            d["reserved_json"] = json.loads(reserved_raw) if reserved_raw else {}
        except Exception:
            d["reserved_json"] = {}
        out.append(d)
    return out

def build_buildqueue_embed(queue_rows):
    """
    Render a neat embed for the build queue rows returned by get_build_queue.
    Shows building name, state, tier, started/complete turns and a short reserved-resources summary.
    """
    emb = discord.Embed(title="Building Queue", color=0x95A5A6)
    if not queue_rows:
        emb.description = "No queued builds."
        return emb

    for b in queue_rows[:60]:
        name = b.get("building_name") or str(b.get("building_id") or "Unknown")
        state = b.get("state_name") or str(b.get("state_id") or "Unknown")
        tier = b.get("tier") or "?"
        started = b.get("started_turn") or "N/A"
        complete = b.get("complete_turn") or "N/A"
        reserved = b.get("reserved_json") or {}
        # make reserved summary small and readable
        if isinstance(reserved, dict) and reserved:
            res_parts = []
            for k, v in reserved.items():
                try:
                    # if nested dict, stringify elegantly
                    if isinstance(v, dict):
                        sub = ", ".join(f"{sk}:{sv}" for sk, sv in v.items())
                        res_parts.append(f"{k}({sub})")
                    else:
                        res_parts.append(f"{k}×{v}")
                except Exception:
                    res_parts.append(str(k))
            reserved_str = ", ".join(res_parts)
        else:
            reserved_str = "None"

        value_lines = [
            f"State: {state}",
            f"Tier: {tier}",
            f"Started: {started}  •  Complete turn: {complete}",
            f"Reserved: {reserved_str}"
        ]
        emb.add_field(name=name, value="\n".join(value_lines), inline=False)

    return emb
    """
    Simple embed renderer for build queue rows returned by get_build_queue.
    """
    emb = discord.Embed(title="Building Queue", color=0x95A5A6)
    if not queue_rows:
        emb.description = "No queued builds."
        return emb
    for b in queue_rows[:60]:
        name = b.get("building_name") or b.get("building_template") or "Unknown"
        state = b.get("state_name") or b.get("state_id") or "Unknown"
        tier = b.get("tier") or "?"
        complete = b.get("complete_turn") or "?"
        emb.add_field(name=f"{name} (Tier {tier})", value=f"State: {state}\nComplete turn: {complete}", inline=False)
    return emb

async def get_state_info(nation_id: str, state_id: str) -> Dict[str, Any]:
    """
    Returns state-level aggregates: population, manpower_used, stockpiles (aggregated),
    list of building templates in the state (aggregated counts), and per-resource produced/consumed/net,
    plus estimated tax income for the state (uses nation's tax_rate).
    """
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("SELECT name FROM states WHERE state_id=?", (state_id,))
    r = await cur.fetchone()
    if not r:
        await conn.close(); return {"error": "State not found"}
    name = r["name"]

    # provinces in state owned by nation
    await cur.execute("""SELECT p.province_id, p.name, p.population
                         FROM provinces p WHERE p.state_id=? AND p.controller_id=?""", (state_id, nation_id))
    provs = await cur.fetchall()
    provinces = [dict(p) for p in provs]

    # aggregates: population and manpower_used (sum of building maintenance manpower)
    await cur.execute("""
        SELECT SUM(p.population) as population_total,
               COALESCE(SUM(bt.maintenance_manpower * pb.count * pb.tier), 0) as manpower_used
        FROM provinces p
        LEFT JOIN province_buildings pb ON pb.province_id = p.province_id
        LEFT JOIN building_templates bt ON bt.id = pb.building_id
        WHERE p.state_id=? AND p.controller_id=?
    """, (state_id, nation_id))
    mu = await cur.fetchone()
    population_total = int(mu["population_total"] or 0)
    manpower_used = int(mu["manpower_used"] or 0)

    # stockpiles aggregated for this state's provinces under ownership
    await cur.execute("""
        SELECT ps.resource, SUM(ps.amount) as amount, SUM(ps.capacity) as capacity
        FROM province_stockpiles ps
        JOIN provinces p ON ps.province_id = p.province_id
        WHERE p.state_id=? AND p.controller_id=?
        GROUP BY ps.resource
    """, (state_id, nation_id))
    stocks = await cur.fetchall()
    stocklist = {r["resource"]: {"amount": float(r["amount"] or 0), "capacity": float(r["capacity"] or 0)} for r in stocks}

    # building list aggregated by template in this state (no per-province listing)
    await cur.execute("""
        SELECT bt.id as building_id, bt.name as building_name, SUM(pb.count) as count, pb.tier
        FROM province_buildings pb
        JOIN provinces p ON pb.province_id = p.province_id
        JOIN building_templates bt ON bt.id = pb.building_id
        WHERE p.state_id=? AND p.controller_id=?
        GROUP BY bt.id, pb.tier
        ORDER BY bt.name
    """, (state_id, nation_id))
    bld_rows = await cur.fetchall()
    buildings = [dict(r) for r in bld_rows]

    # compute per-resource produced and consumed in this state (from installed buildings in state's provinces)
    await cur.execute("""
        SELECT pb.building_id, pb.tier, pb.count, bt.inputs, bt.outputs
        FROM province_buildings pb
        JOIN provinces p ON pb.province_id = p.province_id
        JOIN building_templates bt ON bt.id = pb.building_id
        WHERE p.state_id=? AND p.controller_id=?
    """, (state_id, nation_id))
    prod_rows = await cur.fetchall()
    produced = {}
    consumed = {}
    for pr in prod_rows:
        try:
            inputs = json.loads(pr["inputs"] or "{}")
        except Exception:
            inputs = {}
        try:
            outputs = json.loads(pr["outputs"] or "{}")
        except Exception:
            outputs = {}
        tier = int(pr["tier"] or 1)
        count = int(pr["count"] or 1)
        mult = tier * count
        for res, amt in outputs.items():
            produced[res] = produced.get(res, 0.0) + float(amt) * mult
        for res, amt in inputs.items():
            consumed[res] = consumed.get(res, 0.0) + float(amt) * mult

    # net per resource
    net = {}
    for r in set(list(produced.keys()) + list(consumed.keys())):
        net[r] = produced.get(r, 0.0) - consumed.get(r, 0.0)

    # get nation's tax rate to compute approximate income per state
    await cur.execute("SELECT tax_rate FROM playernations WHERE nation_id=?", (nation_id,))
    tr = await cur.fetchone()
    tax_rate = float(tr["tax_rate"] or 0) if tr else 0
    estimated_tax_income = tax_rate * population_total

    await conn.close()
    return {
        "name": name,
        "provinces": provinces,
        "provinces_count": len(provinces),
        "population_total": population_total,
        "manpower_used": manpower_used,
        "stockpiles": stocklist,
        "buildings": buildings,
        "produced": produced,
        "consumed": consumed,
        "net": net,
        "estimated_tax_income": estimated_tax_income
    }

async def get_nation_info(nation_id: str) -> Dict[str, Any]:
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("SELECT name, cash, debt, tax_rate FROM playernations WHERE nation_id=?", (nation_id,))
    rn = await cur.fetchone()
    if not rn:
        await conn.close(); return {"error": "Nation not found"}
    name = rn["name"] or nation_id
    cash = float(rn["cash"] or 0); debt = float(rn["debt"] or 0); tax_rate = float(rn["tax_rate"] or 0)
    await cur.execute("""
        SELECT SUM(p.population) as total_pop, COALESCE(SUM(bt.maintenance_manpower * pb.count * pb.tier),0) as manpower_used
        FROM provinces p
        LEFT JOIN province_buildings pb ON pb.province_id = p.province_id
        LEFT JOIN building_templates bt ON bt.id = pb.building_id
        WHERE p.controller_id=?
    """, (nation_id,))
    s = await cur.fetchone()
    total_pop = int(s["total_pop"] or 0)
    manpower_used = int(s["manpower_used"] or 0)
    recruitable = max(0, int(total_pop * 0.4) - manpower_used)
    estimated_tax_income = tax_rate * total_pop
    await conn.close()
    return {
        "name": name,
        "cash": cash,
        "debt": debt,
        "tax_rate": tax_rate,
        "population_total": total_pop,
        "manpower_used": manpower_used,
        "recruitable": recruitable,
        "estimated_tax_income": estimated_tax_income
    }

async def start_build(nation_id: str, state_id: str, building_id: str, tier: int = 1) -> Dict[str, Any]:
    """
    Reserve resources across provinces in a state for a build and queue the build.
    Returns {ok:True, build_id, complete_turn} or {error: msg}.
    """
    conn = await get_conn(); cur = await conn.cursor()
    # validate template
    await cur.execute("SELECT * FROM building_templates WHERE id=?", (building_id,))
    tpl = await cur.fetchone()
    if not tpl:
        await conn.close(); return {"error": "Building template not found"}
    # ensure state exists and nation controls at least one province there
    await cur.execute("SELECT 1 FROM provinces WHERE state_id=? AND controller_id=? LIMIT 1", (state_id, nation_id))
    if not await cur.fetchone():
        await conn.close(); return {"error": "You do not control any provinces in that state"}

    # parse resource cost (tpl is sqlite3.Row — use bracket access)
    try:
        raw = tpl["build_cost_resources"] if "build_cost_resources" in tpl.keys() else tpl["build_cost_resources"]
        cost_resources = json.loads(raw or "{}")
    except Exception:
        cost_resources = {}

    # current turn and compute complete turn
    await cur.execute("SELECT value FROM config WHERE key='current_turn'")
    r = await cur.fetchone(); cur_turn = int(r["value"]) if r else 0
    try:
        build_time_val = tpl["build_time_turns"] if "build_time_turns" in tpl.keys() else tpl.get("build_time_turns", 1)
        build_time = int(build_time_val or 1)
    except Exception:
        build_time = 1
    complete_turn = cur_turn + build_time

    # insert a pending state_build row so we have a build_id to attach reservations to
    await cur.execute("""INSERT INTO state_builds (state_id, building_id, tier, started_turn, complete_turn, nation_id, status, reserved_json)
                         VALUES (?, ?, ?, ?, ?, ?, 'pending', '{}')""",
                      (state_id, building_id, tier, cur_turn, complete_turn, nation_id))
    build_id = cur.lastrowid

    # try to reserve required resources across provinces in state, greedily by node_strength
    reservations = []  # list of {province_id, resource, amount}
    try:
        for resource, required in cost_resources.items():
            remaining = float(required)
            # get provinces in that state and owned by nation
            await cur.execute("""SELECT province_id FROM provinces
                                 WHERE state_id=? AND controller_id=?
                                 ORDER BY node_strength DESC""", (state_id, nation_id))
            provs = await cur.fetchall()
            for p in provs:
                if remaining <= 1e-9:
                    break
                pid = p["province_id"]
                # available in this province (unreserved)
                avail = await stockpile.get_available_amount(pid, resource)
                if avail <= 1e-9:
                    continue
                take = min(avail, remaining)
                ok = await stockpile.reserve_resources(build_id, pid, resource, take)
                if not ok:
                    continue
                reservations.append({"province_id": pid, "resource": resource, "amount": take})
                remaining -= take
            if remaining > 1e-6:
                # insufficient resources -> rollback reservations
                await cur.execute("DELETE FROM province_reservations WHERE build_id=?", (build_id,))
                await cur.execute("DELETE FROM state_builds WHERE id=?", (build_id,))
                await conn.commit(); await conn.close()
                return {"error": f"Insufficient {resource} in state to start build (need {required})"}
        # all required resources reserved successfully; record reserved_json
        await cur.execute("UPDATE state_builds SET reserved_json=? WHERE id=?", (json.dumps(reservations), build_id))
        await conn.commit(); await conn.close()
        return {"ok": True, "build_id": build_id, "complete_turn": complete_turn}
    except Exception as e:
        await cur.execute("DELETE FROM province_reservations WHERE build_id=?", (build_id,))
        await cur.execute("DELETE FROM state_builds WHERE id=?", (build_id,))
        await conn.commit(); await conn.close()
        return {"error": f"Failed to reserve resources: {e}"}

async def cancel_build(nation_id: str, build_id: int) -> Dict[str, Any]:
    """
    Cancel a pending build in the queue. Frees reservations and removes the state_build row.
    Only allowed for builds that belong to the nation and are still pending.
    """
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("SELECT * FROM state_builds WHERE id=?", (build_id,))
    b = await cur.fetchone()
    if not b:
        await conn.close(); return {"error": "Build not found"}
    if b["nation_id"] != nation_id:
        await conn.close(); return {"error": "You do not own that build"}
    if b["status"] != "pending":
        await conn.close(); return {"error": "Only pending builds can be cancelled"}
    # free reservations
    await cur.execute("DELETE FROM province_reservations WHERE build_id=?", (build_id,))
    # remove build record
    await cur.execute("DELETE FROM state_builds WHERE id=?", (build_id,))
    await conn.commit(); await conn.close()
    return {"ok": True}

async def demolish(installed_rowid: int, nation_id: str) -> Dict[str, Any]:
    """
    Immediately remove an installed building. No refund. Only allowed if province belongs to nation.
    """
    conn = await get_conn(); cur = await conn.cursor()
    # check building exists and province ownership
    await cur.execute("SELECT pb.province_id, p.controller_id FROM province_buildings pb JOIN provinces p ON pb.province_id = p.province_id WHERE pb.rowid=?", (installed_rowid,))
    r = await cur.fetchone()
    if not r:
        await conn.close(); return {"error": "Installed building not found"}
    if r["controller_id"] != nation_id:
        await conn.close(); return {"error": "You do not control that province/building"}
    # delete it
    await cur.execute("DELETE FROM province_buildings WHERE rowid=?", (installed_rowid,))
    await conn.commit(); await conn.close()
    return {"ok": True}

async def find_buildings(nation_id: str, building_query: str):
    """
    Search for buildings by building_id or partial name across the nation's provinces.
    Returns list of {state_id, province_id, province_name, building_id, building_name, tier, count, installed_id}
    """
    conn = await get_conn(); cur = await conn.cursor()
    q = "%" + building_query.lower() + "%"
    await cur.execute("SELECT id, name FROM building_templates WHERE LOWER(id) LIKE ? OR LOWER(name) LIKE ? LIMIT 50", (q, q))
    matches = await cur.fetchall()
    if not matches:
        await conn.close(); return []
    ids = [m["id"] for m in matches]
    # find installed buildings of those types in nation's provinces
    await cur.execute(f"""
        SELECT s.state_id, s.name as state_name, p.province_id, p.name as province_name,
               pb.rowid as installed_id, pb.building_id, bt.name as building_name, pb.tier, pb.count
        FROM province_buildings pb
        JOIN provinces p ON pb.province_id = p.province_id
        JOIN states s ON p.state_id = s.state_id
        JOIN building_templates bt ON bt.id = pb.building_id
        WHERE p.controller_id=? AND pb.building_id IN ({','.join('?' for _ in ids)})
        ORDER BY s.name, p.name
    """, (nation_id, *ids))
    rows = await cur.fetchall(); await conn.close()
    return [dict(r) for r in rows]

async def get_resources_by_state(nation_id: str):
    """
    For each province owned by the nation, return resource tile info (per-province).
    Kept for compatibility with older commands.
    """
    RAW = set(["Raw Ore", "Coal", "Oil", "Food", "Raw Uranium"])
    conn = await get_conn(); cur = await conn.cursor()
    # get all provinces owned by nation
    await cur.execute("SELECT province_id, name, state_id FROM provinces WHERE controller_id=?", (nation_id,))
    provs = await cur.fetchall()
    out = {}
    for p in provs:
        pid = p["province_id"]; pname = p["name"]; sid = p["state_id"]
        # find raw resources in this province by capacity
        await cur.execute("SELECT resource, capacity, amount FROM province_stockpiles WHERE province_id=?", (pid,))
        rows = await cur.fetchall()
        # pick resource in RAW with largest capacity
        best = None
        for r in rows:
            res = r["resource"]
            if not res:
                continue
            if res.lower() == "food":
                res = "Food"
            if res.lower() in ("raw uranium", "raw_uranium", "raw-uranium"):
                res = "Raw Uranium"
            if res not in RAW:
                continue
            cap = float(r["capacity"] or 0)
            if best is None or cap > best["capacity"]:
                best = {"resource": res, "capacity": cap, "amount": float(r["amount"] or 0)}
        if best is None:
            entry = {"province_id": pid, "province_name": pname, "resource": None, "quality": None, "utilized": False}
        else:
            cap = best["capacity"]
            if cap >= 500:
                quality = ("Rich", 5)
            elif cap >= 200:
                quality = ("Common", 3)
            else:
                quality = ("Poor", 1)
            # check if there's any building in this province that produces this resource
            await cur.execute("""
                SELECT 1 FROM province_buildings pb
                JOIN building_templates bt ON bt.id = pb.building_id
                WHERE pb.province_id=? AND bt.outputs LIKE ?
                LIMIT 1
            """, (pid, f'%"' + best["resource"] + f'"%'))
            prod_row = await cur.fetchone()
            utilized = bool(prod_row)
            entry = {"province_id": pid, "province_name": pname, "resource": best["resource"], "quality": quality, "utilized": utilized, "capacity": cap, "amount": best["amount"]}
        out.setdefault(sid, []).append(entry)
    await conn.close()
    return out

# state-level rollup
async def get_resources_rollup(nation_id: str):
    """
    Return a state-by-state rollup of resources for provinces owned by nation_id.

    Output structure:
    {
      state_id: {
         state_name: str,
         total_provinces: int,
         resourceless: int,
         resources: {
             resource_name: {
                 provinces: int,
                 utilized: int,
                 qualities: {"Rich": n, "Common": n, "Poor": n, "Unknown": n},
                 total_available: int   # computed as quality_value * provinces
             },
             ...
         }
      },
      ...
    }

    Uses explicit provinces.resource column if present; falls back to inferring
    from province_stockpiles (capacity-based).
    Canonical resource names: "Raw Ore", "Coal", "Oil", "Food", "Raw Uranium".
    """
    conn = await get_conn(); cur = await conn.cursor()

    # helper to safely access either sqlite3.Row or dict-like row
    def _row_val(row, key, default=None):
        try:
            return row[key]
        except Exception:
            try:
                return row.get(key, default)
            except Exception:
                return default

    # Detect columns
    await cur.execute("PRAGMA table_info(provinces)")
    cols = await cur.fetchall()
    col_names = {c["name"] for c in cols}

    use_explicit = "resource" in col_names

    RAW = set(["Raw Ore", "Coal", "Oil", "Food", "Raw Uranium"])

    def _qual_label_from_val(v):
        if v is None:
            return "Unknown"
        try:
            i = int(v)
            if i >= 3:
                return "Rich"
            if i == 2:
                return "Common"
            return "Poor"
        except Exception:
            s = str(v).lower()
            if s.startswith("rich"):
                return "Rich"
            if s.startswith("comm") or s.startswith("med"):
                return "Common"
            if s.startswith("poor"):
                return "Poor"
        return "Unknown"

    # mapping quality labels to numeric availability
    quality_value = {"Rich": 5, "Common": 3, "Poor": 1, "Unknown": 0}

    out = {}

    # gather states owned by nation (states that have at least one province owned)
    await cur.execute("""
        SELECT s.state_id, s.name
        FROM states s
        JOIN provinces p ON p.state_id = s.state_id
        WHERE p.controller_id = ?
        GROUP BY s.state_id, s.name
        ORDER BY s.name
    """, (nation_id,))
    states = await cur.fetchall()

    for s in states:
        sid = _row_val(s, "state_id") or s["state_id"]
        sname = _row_val(s, "name") or s["name"]
        entry = {
            "state_name": sname,
            "total_provinces": 0,
            "resourceless": 0,
            "resources": {}
        }

        # select provinces; include resource fields if present
        select_cols = "province_id, name"
        if use_explicit:
            select_cols += ", resource, resource_quality"
        await cur.execute(f"SELECT {select_cols} FROM provinces WHERE controller_id=? AND state_id=? ORDER BY name", (nation_id, sid))
        provs = await cur.fetchall()

        for p in provs:
            entry["total_provinces"] += 1
            pid = _row_val(p, "province_id") or p["province_id"]

            # determine resource + quality
            resname = None
            qlabel = None

            if use_explicit:
                raw_res = _row_val(p, "resource")
                raw_q = _row_val(p, "resource_quality")
                if raw_res is not None:
                    rn = str(raw_res).strip()
                    # normalize legacy names
                    if rn.lower() in ("raw uranium", "raw_uranium", "raw-uranium", "uranium"):
                        resname = "Raw Uranium"
                    elif rn.lower() in ("food", "arable"):
                        resname = "Food"
                    else:
                        resname = rn
                    qlabel = _qual_label_from_val(raw_q)
                else:
                    resname = None
                    qlabel = None
            else:
                # fallback: infer from province_stockpiles biggest capacity for raw resources
                await cur.execute("SELECT resource, capacity FROM province_stockpiles WHERE province_id=?", (pid,))
                rows = await cur.fetchall()
                best = None
                for r in rows:
                    rname = _row_val(r, "resource")
                    if not rname:
                        continue
                    rn = str(rname)
                    if rn.lower() == "food":
                        rn = "Food"
                    if rn.lower() in ("raw uranium", "raw_uranium", "raw-uranium"):
                        rn = "Raw Uranium"
                    if rn not in RAW:
                        continue
                    cap = float(_row_val(r, "capacity") or 0)
                    if best is None or cap > best["capacity"]:
                        best = {"resource": rn, "capacity": cap}
                if best:
                    resname = best["resource"]
                    cap = best["capacity"]
                    if cap >= 500:
                        qlabel = "Rich"
                    elif cap >= 200:
                        qlabel = "Common"
                    elif cap > 0:
                        qlabel = "Poor"
                    else:
                        qlabel = "Unknown"
                else:
                    resname = None
                    qlabel = None

            # utilization check: any installed building in this province that lists the resource in outputs
            utilized = False
            if resname:
                await cur.execute("""
                    SELECT 1 FROM province_buildings pb
                    JOIN building_templates bt ON bt.id = pb.building_id
                    WHERE pb.province_id=? AND bt.outputs LIKE ?
                    LIMIT 1
                """, (pid, f'%"' + resname + f'"%'))
                if await cur.fetchone():
                    utilized = True

            if not resname:
                entry["resourceless"] += 1
            else:
                rmap = entry["resources"].setdefault(resname, {"provinces": 0, "utilized": 0, "qualities": {"Rich": 0, "Common": 0, "Poor": 0, "Unknown": 0}, "total_available": 0})
                rmap["provinces"] += 1
                if utilized:
                    rmap["utilized"] += 1
                if qlabel:
                    if qlabel not in rmap["qualities"]:
                        rmap["qualities"]["Unknown"] += 1
                    else:
                        rmap["qualities"][qlabel] += 1
                else:
                    rmap["qualities"]["Unknown"] += 1
                # add to total_available using quality value mapping
                qv = quality_value.get(qlabel, 0)
                rmap["total_available"] += qv

        out[sid] = entry

    await conn.close()
    return out
