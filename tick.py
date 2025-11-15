# services/tick.py
from typing import Any
import discord

from services import economy as economy_service

async def register_tick_commands(tree):
    @tree.command(name="endturn", description="Process end-of-turn economy updates (admin only)")
    async def endturn_cmd(interaction: discord.Interaction):
        from db import is_admin
        if not await is_admin(str(interaction.user.id)):
            await interaction.response.send_message("You are not an admin.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            next_turn = await economy_service.run_end_turn()
        except Exception as e:
            await interaction.followup.send(f"❌ End turn failed: {e}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Advanced to turn {next_turn}", ephemeral=True)
