# utils/auto_complete.py
from typing import List
from discord import app_commands
import discord
from db import get_conn

async def state_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice]:
    try:
        from services.build import owned_states_for_nation
        discord_id = str(interaction.user.id)
        # get user's nation
        conn = await get_conn(); cur = await conn.cursor()
        await cur.execute("SELECT nation_id FROM playernations WHERE owner_discord_id=?", (discord_id,))
        r = await cur.fetchone(); await conn.close()
        if not r:
            return []
        nid = r["nation_id"]
        items = await owned_states_for_nation(nid, current)
        choices = [app_commands.Choice(name=i["label"], value=i["id"]) for i in items]
        return choices
    except Exception:
        return []

async def building_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice]:
    try:
        from services.build import available_buildings
        items = await available_buildings(current)
        choices = [app_commands.Choice(name=i["label"], value=i["id"]) for i in items]
        return choices
    except Exception:
        return []

async def resource_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice]:
    """
    Autocomplete resources from resources table (top 25). Returns app_commands.Choice.
    """
    try:
        conn = await get_conn(); cur = await conn.cursor()
        q = "%" + (current or "").lower() + "%"
        await cur.execute("SELECT resource FROM resources WHERE LOWER(resource) LIKE ? ORDER BY resource LIMIT 25", (q,))
        rows = await cur.fetchall(); await conn.close()
        choices = []
        for r in rows:
            name = r["resource"]
            choices.append(app_commands.Choice(name=name, value=name))
        return choices
    except Exception:
        return []
