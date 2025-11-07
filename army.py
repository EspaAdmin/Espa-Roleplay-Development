# services/army.py
# Army viewing service: builds an embed summarizing an army, its location (province/state),
# units (counts, manpower), and totals. Defensive schema detection to fit your DB.

import discord
from discord import ui
from typing import Any, Dict, List, Tuple, Optional
from db import get_conn
import services.recruit as recruit_service  # create_army lives there
import asyncio


# ---------- Helper: list states the nation controls (id,label) ----------
async def _owned_states_for_nation(nation_id: str) -> List[Tuple[str, str]]:
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
    rows = await cur.fetchall()
    await conn.close()
    out = []
    for r in rows:
        sid = r["state_id"]; name = r["name"]; cnt = int(r["provinces"] or 0)
        label = f"{name} ({sid}) — {cnt} provs"
        out.append((sid, label))
    return out

# ---------- Helper: provinces in a state that the nation controls ----------
async def _provinces_in_state_for_nation(nation_id: str, state_id: str) -> List[Tuple[str,str]]:
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("SELECT province_id, name FROM provinces WHERE state_id=? AND controller_id=? ORDER BY name", (state_id, nation_id))
    rows = await cur.fetchall(); await conn.close()
    out = [(r["province_id"], f"{r['name']} ({r['province_id']})") for r in rows]
    return out

# ---------- Interactive handler: create army with drill-down selects ----------
async def handle_create_army_interactive(interaction: discord.Interaction, nation_id: str, timeout: float = 120.0):
    """
    Send an interactive state -> province selector to help player place the new army.
    On final selection, calls recruit_service.create_army and then returns get_army_embed to show the new army.
    """
    await interaction.response.defer()

    # gather owned states
    states = await _owned_states_for_nation(nation_id)
    if not states:
        await interaction.followup.send("You do not control any states with provinces to place an army.", ephemeral=True)
        return

    # build first select for states
    class StateSelect(ui.Select):
        def __init__(self, options):
            super().__init__(placeholder="Choose state to place army in", min_values=1, max_values=1, options=options)

        async def callback(self, inter: discord.Interaction):
            sel_state = self.values[0]
            # fetch provinces for that state
            provs = await _provinces_in_state_for_nation(nation_id, sel_state)
            if not provs:
                await inter.followup.send("No owned provinces found in that state.", ephemeral=True)
                return
            # build province select view
            prov_options = [discord.SelectOption(label=label, value=pid) for pid, label in provs]
            prov_select = ProvinceSelect(prov_options, sel_state)
            view2 = ui.View(timeout=timeout)
            view2.add_item(prov_select)
            try:
                await inter.edit_original_response(content=f"State selected: **{sel_state}** — choose province to place army", embed=None, view=view2)
            except Exception:
                try:
                    await inter.followup.send(content=f"State selected: **{sel_state}** — choose province to place army", view=view2)
                except Exception:
                    pass

    class ProvinceSelect(ui.Select):
        def __init__(self, options, state_id):
            super().__init__(placeholder="Choose province to place army", min_values=1, max_values=1, options=options)
            self.state_id = state_id

        async def callback(self, inter: discord.Interaction):
            chosen_pid = self.values[0]
            # prompt for army name using a simple confirmation step or just create with default name
            # We'll create with default name "Army {province_id}"
            default_name = f"Army {chosen_pid}"
            res = await recruit_service.create_army(nation_id, default_name, chosen_pid)
            if not res.get("ok"):
                await inter.followup.send(f"❌ Could not create army: {res.get('error')}", ephemeral=True)
                return
            army_id = res.get("id")
            # show the newly created army embed if possible using get_army_embed from this service
            from services.army import get_army_embed as _get_army_embed  # local import to avoid circulars
            emb_res = await _get_army_embed(nation_id, army_id)
            if emb_res.get("ok"):
                await inter.followup.send("✅ Army created.", embed=emb_res["embed"])
            else:
                # fallback simple confirmation
                await inter.followup.send(f"✅ Army created with id {army_id} but could not display full view: {emb_res.get('error')}")

    # assemble initial state select view
    state_options = [discord.SelectOption(label=label, value=sid) for sid, label in states]
    view = ui.View(timeout=timeout)
    view.add_item(StateSelect(state_options))

    try:
        await interaction.followup.send("Choose state to place new army in", view=view, ephemeral=True)
    except Exception:
        try:
            await interaction.followup.send("Could not present interactive placement. Try again.", ephemeral=True)
        except Exception:
            pass

        
# -------------------------
# Utility: read table columns
# -------------------------
async def _table_columns(conn, table_name: str) -> List[str]:
    cur = await conn.cursor()
    try:
        await cur.execute(f"PRAGMA table_info({table_name})")
        rows = await cur.fetchall()
    except Exception:
        return []
    cols = []
    for r in rows:
        # r may be tuple-like; column name at index 1
        try:
            cols.append(r[1])
        except Exception:
            # fallback if dict-like
            keys = list(r.keys())
            if len(keys) > 1:
                cols.append(r[keys[1]])
    return cols

# -------------------------
# Autocomplete helper (for bot)
# -------------------------
async def army_autocomplete_for_nation(nation_id: str, prefix: str = "") -> List[Tuple[str, str]]:
    """
    Return list of (army_id, label) for given nation, for autocomplete.
    """
    conn = await get_conn(); cur = await conn.cursor()
    cols = await _table_columns(conn, "armies")
    # detect id column
    id_col = next((c for c in ("id", "army_id", "armies_id") if c in cols), (cols[0] if cols else "rowid"))
    name_col = "name" if "name" in cols else (cols[1] if len(cols) > 1 else id_col)
    q = f"SELECT {id_col} as aid, {name_col} as aname, province_id FROM armies WHERE nation_id=? ORDER BY {name_col} LIMIT 200"
    try:
        await cur.execute(q, (nation_id,))
        rows = await cur.fetchall()
    except Exception:
        await cur.execute("SELECT * FROM armies WHERE nation_id=? LIMIT 200", (nation_id,))
        rows = await cur.fetchall()
    out = []
    pref = (prefix or "").lower()
    for r in rows:
        aid = str(r["aid"]) if "aid" in r.keys() else str(r[list(r.keys())[0]])
        aname = r["aname"] if "aname" in r.keys() else str(aid)
        label = f"{aname} (id:{aid})"
        if not pref or pref in label.lower() or pref in str(aid):
            out.append((aid, label))
            if len(out) >= 25:
                break
    await conn.close()
    return out

# -------------------------
# Build embed for an army
# -------------------------
async def get_army_embed(nation_id: str, army_id: Any) -> Dict[str, Any]:
    """
    Returns dict: {ok:bool, embed:discord.Embed | None, error: str | None}
    This function fetches the army, validates ownership, fetches units and templates,
    totals manpower and unit counts, and returns a Discord embed ready to send.
    """
    conn = await get_conn(); cur = await conn.cursor()

    # determine armies id column
    cols_armies = await _table_columns(conn, "armies")
    id_col = next((c for c in ("id", "army_id", "armies_id") if c in cols_armies), (cols_armies[0] if cols_armies else "rowid"))

    # fetch army row
    try:
        await cur.execute(f"SELECT * FROM armies WHERE {id_col}=? LIMIT 1", (army_id,))
        arow = await cur.fetchone()
    except Exception as e:
        await conn.close()
        return {"ok": False, "error": f"DB error fetching army: {e}"}

    if not arow:
        await conn.close()
        return {"ok": False, "error": "Army not found."}

    # ownership check
    if "nation_id" in arow.keys() and str(arow["nation_id"]) != str(nation_id):
        await conn.close()
        return {"ok": False, "error": "Army does not belong to your nation."}

    # pick display fields
    army_name = arow["name"] if "name" in arow.keys() else f"Army {army_id}"
    location_province = arow["province_id"] if "province_id" in arow.keys() else (arow.get("location_province") if hasattr(arow, "get") else None)

    # fetch province name & state
    province_name = None
    state_id = None
    state_name = None
    if location_province:
        await cur.execute("SELECT province_id, name, state_id FROM provinces WHERE province_id=? LIMIT 1", (location_province,))
        prow = await cur.fetchone()
        if prow:
            province_name = prow["name"] if "name" in prow.keys() else None
            state_id = prow["state_id"] if "state_id" in prow.keys() else None
            if state_id:
                await cur.execute("SELECT name FROM states WHERE state_id=? LIMIT 1", (state_id,))
                srow = await cur.fetchone()
                if srow:
                    state_name = srow["name"] if "name" in srow.keys() else None

    # fetch units in this army: look for playerarmy entries referencing army_id OR rows located at the army's province
    # detect playerarmy columns
    pa_cols = await _table_columns(conn, "playerarmy")
    units = []
    try:
        if "army_id" in pa_cols:
            # read by army_id
            await cur.execute("SELECT * FROM playerarmy WHERE army_id=? AND nation_id=? ORDER BY template_id", (army_id, nation_id))
            units = await cur.fetchall()
        else:
            # fallback: use location_province
            if location_province:
                await cur.execute("SELECT * FROM playerarmy WHERE location_province=? AND nation_id=? ORDER BY template_id", (location_province, nation_id))
                units = await cur.fetchall()
            else:
                units = []
    except Exception:
        units = []

    # gather template info for each unique template_id
    template_ids = set()
    for u in units:
        # possible keys: template_id, unit_template, template
        if "template_id" in u.keys():
            template_ids.add(u["template_id"])
        else:
            # pick any column that looks like template id
            for k in u.keys():
                if k.lower().startswith("template"):
                    template_ids.add(u[k])
                    break

    templates = {}
    if template_ids:
        # build query using template_id column (detect schema)
        ut_cols = await _table_columns(conn, "unit_templates")
        tid_col = "template_id" if "template_id" in ut_cols else (next((c for c in ("id","unit_id") if c in ut_cols), ut_cols[0] if ut_cols else "rowid"))
        name_col = "name" if "name" in ut_cols else (ut_cols[1] if len(ut_cols) > 1 else tid_col)
        manpower_col = next((c for c in ("manpower_cost", "manpower", "manpower_required") if c in ut_cols), None)

        placeholders = ",".join("?" * len(template_ids))
        q = f"SELECT {tid_col} as tid, {name_col} as tname" + (f", {manpower_col} as manpower" if manpower_col else "") + f" FROM unit_templates WHERE {tid_col} IN ({placeholders})"
        try:
            await cur.execute(q, tuple(template_ids))
            trows = await cur.fetchall()
            for t in trows:
                tid = t["tid"]
                tname = t["tname"] if "tname" in t.keys() else str(tid)
                mpc = float(t["manpower"]) if (manpower_col and "manpower" in t.keys() and t["manpower"] not in (None,"")) else (float(t[manpower_col]) if manpower_col and manpower_col in t.keys() else 0.0) if manpower_col else 0.0
                templates[str(tid)] = {"name": tname, "manpower": mpc}
        except Exception:
            # fallback: load each template individually
            for tid in template_ids:
                try:
                    await cur.execute(f"SELECT * FROM unit_templates WHERE {tid_col}=? LIMIT 1", (tid,))
                    tr = await cur.fetchone()
                    if tr:
                        name = tr["name"] if "name" in tr.keys() else str(tid)
                        mpc = 0.0
                        if "manpower_cost" in tr.keys():
                            try: mpc = float(tr["manpower_cost"] or 0)
                            except: mpc = 0.0
                        templates[str(tid)] = {"name": name, "manpower": mpc}
                except Exception:
                    templates[str(tid)] = {"name": str(tid), "manpower": 0.0}

    # prepare breakdown
    per_type_counts: Dict[str, int] = {}
    per_type_manpower: Dict[str, float] = {}
    total_units = 0
    total_manpower = 0.0

    for u in units:
        # determine template id and count
        if "template_id" in u.keys():
            tid = str(u["template_id"])
        else:
            # find first key that looks like template id
            tid = None
            for k in u.keys():
                if k.lower().startswith("template"):
                    tid = str(u[k])
                    break
            if tid is None:
                # cannot determine, skip
                continue
        count = int(u["count"] or 0) if "count" in u.keys() else int(u.get("number") or 0)
        total_units += count
        tmpl = templates.get(tid, {"name": tid, "manpower": 0.0})
        mpc = float(tmpl.get("manpower") or 0.0)
        man_cost = mpc * count
        per_type_counts[tid] = per_type_counts.get(tid, 0) + count
        per_type_manpower[tid] = per_type_manpower.get(tid, 0.0) + man_cost
        total_manpower += man_cost

    # build embed
    emb = discord.Embed(title=f"⚔️ Army — {army_name} (id: {army_id})", color=0xE74C3C)
    loc_line = f"Province: **{location_province or '—'}**"
    if province_name:
        loc_line += f" — {province_name}"
    if state_id:
        loc_line += f"\nState: **{state_name or state_id}** ({state_id})"
    emb.add_field(name="Location", value=loc_line, inline=False)

    emb.add_field(name="Units (total)", value=f"Distinct types: **{len(per_type_counts)}** — total units: **{total_units:,}**", inline=False)
    emb.add_field(name="Total manpower (est)", value=f"**{int(round(total_manpower)):,}**", inline=False)

    # create a summary lines for each unit type, sorted by count desc
    sorted_types = sorted(per_type_counts.items(), key=lambda x: -x[1])
    if sorted_types:
        # assemble lines; ensure embed fields don't exceed 1024 chars
        lines = []
        for tid, cnt in sorted_types:
            name = templates.get(tid, {}).get("name", tid)
            mpc = templates.get(tid, {}).get("manpower", 0.0)
            man_total = per_type_manpower.get(tid, 0.0)
            lines.append(f"**{name}** ({tid}) — {cnt:,} • mp/unit: {int(mpc)} total: {int(round(man_total))}")
        # chunk lines into fields below 900 characters
        CHUNK = 900
        cur_text = ""
        field_index = 1
        for line in lines:
            if len(cur_text) + len(line) + 1 > CHUNK:
                emb.add_field(name=f"Unit breakdown (cont {field_index})", value=cur_text.rstrip("\n"), inline=False)
                field_index += 1
                cur_text = ""
            cur_text += line + "\n"
        if cur_text:
            emb.add_field(name=f"Unit breakdown ({field_index})", value=cur_text.rstrip("\n"), inline=False)
    else:
        emb.add_field(name="Unit breakdown", value="(none)", inline=False)

    await conn.close()
    return {"ok": True, "embed": emb}
