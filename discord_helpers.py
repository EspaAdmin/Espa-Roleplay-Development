# utils/discord_helpers.py
# Small helper utilities for interacting with discord.Interaction objects safely.

async def safe_reply(interaction, /, **send_kwargs):
    """
    Send a message to an interaction safely. If the interaction response has already been
    completed (deferred or replied), use followup.send, otherwise use response.send_message.
    This avoids "This interaction has already been responded to" errors.
    """
    try:
        # interaction may be a discord.Interaction
        if interaction.response.is_done():
            return await interaction.followup.send(**send_kwargs)
        return await interaction.response.send_message(**send_kwargs)
    except Exception:
        # best-effort fallback: try followup
        try:
            return await interaction.followup.send(**send_kwargs)
        except Exception:
            # give up silently; calling code should handle failure
            return None
