# services/buildings.py
# Show building templates in a clean paged UI. Displays which buildings
# are unlocked for the player's nation (based on researched techs).
#
# Expectations:
# - There is a table `building_templates` (common columns used below)
# - player techs are in `player_technologies` or `playertechnology` (attempt both)
# - get_conn returns an aiosqlite connection (same as other services)
# - Interaction is deferred by caller; this function will defer if not already

import discord
import json
from typing import List, Dict, Any
from db import get_conn
from .audit import log_action  # optional; will be used in try/except if present

# formatting helpers
def _fmt_money(n: float) -> str:
    try:
        return f"${round(n):,}"
    except Exception:
        return f"${n}"

def _short_resources(d: Dict[str, Any]) -> str:
    if not d:
        return "(none)"
    parts = []
    for k, v in d.items():
        try:
            parts.append(f"{k}: {int(round(float(v))):,}")
        except Exception:
            parts.append(f"{k}: {v}")
    return " • ".join(parts)

async def _get_player_techs(nation_id: str) -> set:
    """Return a set of tech ids/names the player has researched (defensive)."""
    conn = await get_conn(); cur = await conn.cursor()
    techs = set()
    try:
        # try common table name
        await cur.execute("SELECT tech_id FROM player_technologies WHERE nation_id=?", (nation_id,))
        rows = await cur.fetchall()
        techs.update({str(r["tech_id"]) for r in rows})
    except Exception:
        try:
            await cur.execute("SELECT tech_id FROM playertechnology WHERE nation_id=?", (nation_id,))
            rows = await cur.fetchall()
            techs.update({str(r["tech_id"]) for r in rows})
        except Exception:
            techs = set()
    finally:
        await conn.close()
    return techs

async def _fetch_building_templates() -> List[Dict[str, Any]]:
    conn = await get_conn(); cur = await conn.cursor()
    try:
        await cur.execute("SELECT * FROM building_templates ORDER BY category, name")
        rows = await cur.fetchall()
        data = [dict(r) for r in rows]
    except Exception:
        data = []
    finally:
        await conn.close()
    return data

def _parse_json_field(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        # maybe it's already a python repr or semi-colon list -> fallback empty
        return {}

async def _build_embeds_for_nation(nation_id: str) -> List[discord.Embed]:
    templates = await _fetch_building_templates()
    player_techs = await _get_player_techs(nation_id)

    # audit (best effort)
    try:
        await log_action(nation_id, "view_buildings", {"templates": len(templates)}, turn=None)
    except Exception:
        pass

    total = len(templates)
    unlocked = 0
    rows = []
    for t in templates:
        tid = t.get("id") or t.get("building_id") or t.get("template_id") or t.get("name")
        name = t.get("name") or t.get("display_name") or str(tid)
        category = t.get("category") or "Misc"
        tier = t.get("tier") or t.get("level") or t.get("building_tier") or ""
        # costs and IO fields might be JSON
        build_cash = float(t.get("build_cost_cash") or t.get("build_cost") or 0)
        build_res = _parse_json_field(t.get("build_cost_resources") or t.get("build_cost_resources_json") or t.get("build_costs") or "{}")
        maintenance_cash = float(t.get("maintenance_cash") or t.get("maint_cost") or 0)
        maintenance_manpower = int(t.get("maintenance_manpower") or t.get("maint_manpower") or 0)
        inputs = _parse_json_field(t.get("inputs") or t.get("input") or "{}")
        outputs = _parse_json_field(t.get("outputs") or t.get("output") or "{}")
        notes = t.get("notes") or t.get("description") or ""
        tech_required_raw = t.get("tech_required") or t.get("requires") or ""
        tech_required = []
        if tech_required_raw:
            if isinstance(tech_required_raw, (list, tuple)):
                tech_required = [str(x) for x in tech_required_raw]
            else:
                try:
                    tech_required = list(json.loads(tech_required_raw)) if tech_required_raw else []
                except Exception:
                    tech_required = [s.strip() for s in str(tech_required_raw).split(",") if s.strip()]

        # unlocked check: if no tech required -> unlocked; else require all techs present
        is_unlocked = True
        if tech_required:
            for tr in tech_required:
                if str(tr) not in player_techs:
                    is_unlocked = False
                    break
        if is_unlocked:
            unlocked += 1

        rows.append({
            "id": str(tid),
            "name": name,
            "category": category,
            "tier": tier,
            "build_cash": build_cash,
            "build_resources": build_res,
            "maintenance_cash": maintenance_cash,
            "maintenance_manpower": maintenance_manpower,
            "inputs": inputs,
            "outputs": outputs,
            "notes": notes,
            "tech_required": tech_required,
            "unlocked": is_unlocked
        })

    # Build a summary embed and then paged detailed embeds
    emb_summary = discord.Embed(title="🏗️ Buildings — Available", color=0x2ECC71)
    emb_summary.description = (
        f"Total templates: **{total}**\n"
        f"Unlocked for you: **{unlocked}**\n"
        f"Use the buttons to page through building templates. Locked buildings are shown but marked 🔒.\n"
    )
    emb_summary.set_footer(text="Showing data from building_templates table")

    # Prepare pages (8 items per page)
    pages = []
    page_size = 8
    for i in range(0, len(rows), page_size):
        chunk = rows[i:i+page_size]
        emb = discord.Embed(title=f"Buildings (templates) — page {i//page_size + 1}", color=0x3498DB)
        for it in chunk:
            lock_emoji = "" if it["unlocked"] else "🔒 "
            header = f"{lock_emoji}**{it['name']}** — id: `{it['id']}`"
            sublines = []
            if it["tier"]:
                sublines.append(f"Tier: {it['tier']}")
            sublines.append(f"Build cash: {_fmt_money(it['build_cash'])}")
            if it["build_resources"]:
                sublines.append(f"Build resources: {_short_resources(it['build_resources'])}")
            if it["maintenance_cash"]:
                sublines.append(f"Maintenance: {_fmt_money(it['maintenance_cash'])} / turn")
            if it["maintenance_manpower"]:
                sublines.append(f"Manpower: {it['maintenance_manpower']:,}")
            if it["inputs"]:
                sublines.append(f"Inputs: {_short_resources(it['inputs'])}")
            if it["outputs"]:
                sublines.append(f"Outputs: {_short_resources(it['outputs'])}")
            if it["tech_required"]:
                sublines.append(f"Requires: {', '.join(it['tech_required'])}")
            if it["notes"]:
                sublines.append(f"Notes: {it['notes'][:200]}{'...' if len(it['notes'])>200 else ''}")
            emb.add_field(name=header, value="\n".join(sublines) or "(none)", inline=False)
        pages.append(emb)

    # Return summary + pages
    return [emb_summary] + pages

# ---------- Public handler ----------
async def handle_buildings_command(interaction: discord.Interaction, nation_id: str = None, timeout: float = 120.0):
    """
    Main entrypoint. If nation_id provided, will mark unlocked based on that nation's techs.
    Otherwise treat as generic list (unlocked==all).
    """
    await interaction.response.defer()

    try:
        # NB: function _build_embeds_for_nation will fetch templates and techs
        embeds = await _build_embeds_for_nation(nation_id)
    except Exception as e:
        await interaction.followup.send(f"Error building templates view: {e}")
        return

    if not embeds:
        await interaction.followup.send("No building templates found.")
        return

    # pager view
    class Pager(discord.ui.View):
        def __init__(self, embeds_list: List[discord.Embed], timeout: float = timeout):
            super().__init__(timeout=timeout)
            self.embeds = embeds_list
            self.index = 0

        async def _update_message(self, interaction: discord.Interaction):
            # Edit the interaction message (component interaction)
            content = f"Page {self.index+1}/{len(self.embeds)}"
            try:
                await interaction.response.edit_message(embed=self.embeds[self.index], content=content, view=self)
            except Exception:
                # fallback: send as followup
                try:
                    await interaction.followup.send(embed=self.embeds[self.index], content=content, view=self)
                except Exception:
                    pass

        @discord.ui.button(label="⏮ Prev", style=discord.ButtonStyle.secondary)
        async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.index = (self.index - 1) % len(self.embeds)
            await self._update_message(interaction)

        @discord.ui.button(label="Next ⏭", style=discord.ButtonStyle.secondary)
        async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.index = (self.index + 1) % len(self.embeds)
            await self._update_message(interaction)

        @discord.ui.button(label="Close ✖", style=discord.ButtonStyle.danger)
        async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            try:
                await interaction.response.edit_message(content="Closed.", embed=None, view=None)
            except Exception:
                try:
                    await interaction.followup.send("Closed.", ephemeral=True)
                except Exception:
                    pass
            self.stop()

        async def on_timeout(self):
            for c in self.children:
                c.disabled = True
            # try to edit to reflect disabled
            try:
                msg = await interaction.original_response()
                await msg.edit(view=self)
            except Exception:
                pass

    view = Pager(embeds)
    # initial send (we already deferred)
    try:
        await interaction.followup.send(content=f"Page 1/{len(embeds)}", embed=embeds[0], view=view)
    except Exception as e:
        # fallback: send summary only
        await interaction.followup.send(f"Could not create interactive view: {e}", embed=embeds[0])
