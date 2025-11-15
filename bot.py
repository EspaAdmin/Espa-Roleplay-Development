# bot.py
# Full consolidated bot wiring. Heavy logic lives in services/*.py
import os
import json
import io
import logging
from typing import Optional, List
import math
import discord
from discord import app_commands
from dotenv import load_dotenv
from services import economy_modifiers as mod_service
from services import recruit as recruit_service
# DB helpers - adjust names if different in your project
from db import get_conn, get_nation_for_user, is_admin
from services import invite as invite_service
# Services
from services import autocomplete, build as build_service, trade as trade_service, goods as goods_service, economy as economy_service, tick as tick_service
from services import nation as nation_service
from services.nation import GAME_JSON_PATH
from services import starter as starter_service
import services.recruit as recruit_service
from discord import app_commands
import services.army as army_service




log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

load_dotenv(dotenv_path="key.env")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Safe fallback for autocomplete if not present
async def _ac_dummy(interaction, current: str):
    return []

class _ACDummy:
    state_autocomplete = staticmethod(_ac_dummy)
    building_autocomplete = staticmethod(_ac_dummy)
    resource_autocomplete = staticmethod(_ac_dummy)

try:
    import utils.auto_complete as ac  # preferred
except Exception:
    try:
        from services import autocomplete as ac  # alternate location
    except Exception:
        ac = _ACDummy()

# --------------------------
# Helpers for safe sending (avoid "already responded" issues)
# --------------------------




async def _filtered_send(target_send_coro, *, content=None, embed=None, view=None, ephemeral=False):
    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if ephemeral:
        kwargs["ephemeral"] = True
    await target_send_coro(**kwargs)

def make_autocomplete_func(attr_name: str, default_limit: int = 25):
    """
    Return an async autocomplete function. It will call ac.<attr_name> if present,
    otherwise return an empty list. This avoids AttributeError when modules are missing.
    The returned function signature matches discord.py expectations: async (interaction, current).
    """
    async def _ac(interaction: discord.Interaction, current: str):
        try:
            # prefer utils.auto_complete if it exists as 'ac' variable
            func = getattr(ac, attr_name, None)
            if func:
                # if the provided func is synchronous but returns list, handle both
                res = await func(interaction, current) if callable(func) and hasattr(func, "__call__") else []
                # Ensure the response is a list of app_commands.Choice objects or plain strings
                out = []
                for v in res or []:
                    if isinstance(v, app_commands.Choice):
                        out.append(v)
                    elif isinstance(v, dict) and "name" in v and "value" in v:
                        out.append(app_commands.Choice(name=str(v["name"]), value=v["value"]))
                    elif isinstance(v, tuple) and len(v) >= 2:
                        out.append(app_commands.Choice(name=str(v[0]), value=v[1]))
                    else:
                        # fallback: treat as string -> use as both name and value
                        out.append(app_commands.Choice(name=str(v), value=str(v)))
                return out[:25]
        except Exception:
            # fail silently and return empty choices (this keeps the bot stable)
            import traceback; traceback.print_exc()
        return []
    return _ac


async def safe_defer_and_followup(interaction: discord.Interaction, *, content: str = None, embed: discord.Embed = None, view=None, ephemeral: bool = False):
    """
    Defer if necessary, then followup.send. Only pass 'view' when it's not None.
    """
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
        await _filtered_send(interaction.followup.send, content=content, embed=embed, view=view, ephemeral=ephemeral)
    except Exception:
        log.exception("safe_defer_and_followup failed")
        try:
            await interaction.followup.send(content=content or "❌ Failed to send.", ephemeral=True)
        except Exception:
            pass

async def safe_send_or_followup(interaction: discord.Interaction, *, content: str = None, embed: discord.Embed = None, view=None, ephemeral: bool = False):
    """
    Use response.send_message if not yet responded, otherwise followup.send.
    """
    try:
        if not interaction.response.is_done():
            await _filtered_send(interaction.response.send_message, content=content, embed=embed, view=view, ephemeral=ephemeral)
        else:
            await _filtered_send(interaction.followup.send, content=content, embed=embed, view=view, ephemeral=ephemeral)
    except Exception:
        log.exception("safe_send_or_followup fallback")
        try:
            await interaction.followup.send(content=content or "❌ Failed to send.", ephemeral=True)
        except Exception:
            pass

async def safe_reply(interaction: discord.Interaction, *, content: str = None, embed: discord.Embed = None, view=None, ephemeral: bool = False):
    """
    Safely send either the initial response or a followup depending on whether the interaction has already been responded to.
    """
    await safe_send_or_followup(interaction, content=content, embed=embed, view=view, ephemeral=ephemeral)

# --------------------------
# Require helpers
# --------------------------
async def require_nation(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    try:
        nation = await get_nation_for_user(discord_id)
    except Exception:
        log.exception("require_nation: db lookup failed")
        await safe_send_or_followup(interaction, content="Failed to look up your nation (db). Ask an admin.", ephemeral=True)
        return None
    if not nation:
        await safe_send_or_followup(interaction, content="You are not linked to a nation. Ask an admin to link your Discord account.", ephemeral=True)
        return None
    return nation

async def require_admin(interaction: discord.Interaction):
    try:
        ok = await is_admin(str(interaction.user.id))
    except Exception:
        log.exception("require_admin: db check failed")
        await safe_send_or_followup(interaction, content="Failed to check admin status (db).", ephemeral=True)
        return False
    if not ok:
        await safe_send_or_followup(interaction, content="You are not an admin.", ephemeral=True)
        return False
    return True

# --------------------------
# Global error handler for commands
# --------------------------







@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        if interaction and not interaction.response.is_done():
            await interaction.response.send_message("⚠️ An unexpected error occurred while processing your command.", ephemeral=True)
        elif interaction:
            await interaction.followup.send("⚠️ An unexpected error occurred while processing your command.", ephemeral=True)
    except Exception:
        pass
    cmd_name = interaction.command.name if (interaction and interaction.command) else "<unknown>"
    log.exception(f"[COMMAND ERROR] {cmd_name}: {error}")

# Single ACTION_CHOICES definition (used in /offer)
ACTION_CHOICES = [
    app_commands.Choice(name="send (you send resource/cash)", value="send"),
    app_commands.Choice(name="request (you request resource/cash)", value="request"),
]

# Single ACTION_CHOICES definition (used in /offer)
ACTION_CHOICES = [
    app_commands.Choice(name="send (you send resource/cash)", value="send"),
    app_commands.Choice(name="request (you request resource/cash)", value="request"),
]

# -----------------------
# Goods command (delegates to services.goods)
# -----------------------
@tree.command(name="goods", description="View your nation's goods (switch categories with buttons)")
async def goods_cmd(interaction: discord.Interaction):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    emb, view = await goods_service.get_goods_embed_and_view(nation["nation_id"], interaction.user.id)
    await interaction.followup.send(embed=emb, view=view)





# -----------------------
# Market (delegates to services.trade)
# -----------------------
@tree.command(name="market", description="View current market posts")
@app_commands.describe(resource="Optional resource filter")
async def market_cmd(interaction: discord.Interaction, resource: Optional[str] = None):
    await interaction.response.defer()
    try:
        emb, view = await trade_service.create_market_embed_and_view(interaction.user.id, resource=resource, page=0)
        # If view is None, send just embed; else send embed + view
        if view:
            await interaction.followup.send(embed=emb, view=view)
        else:
            await interaction.followup.send(embed=emb)
    except Exception as e:
        log.exception("market_cmd failed")
        await interaction.followup.send(f"❌ Failed to load market: {e}", ephemeral=True)
# -----------------------
# Offer (create direct offer to a user; not more than 3 outstanding per nation enforced in trade service)
# -----------------------
@tree.command(name="offer", description="Create a direct offer to another nation's leader (mention the user)")
@app_commands.describe(to_user="Mention recipient", action="send or request", resource="Resource (required)", quantity="Quantity (required)", offered_cash="Cash to escrow (optional)")
@app_commands.autocomplete(resource=ac.resource_autocomplete)
@app_commands.choices(action=ACTION_CHOICES)
async def offer_cmd(interaction: discord.Interaction, to_user: discord.User, action: app_commands.Choice[str], resource: str, quantity: float, offered_cash: float = 0.0):
    nation = await require_nation(interaction)
    if not nation:
        return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)
    if to_user is None:
        return await safe_send_or_followup(interaction, content="You must mention a recipient.", ephemeral=True)

    # resolve recipient nation
    try:
        target = await get_nation_for_user(str(to_user.id))
    except Exception:
        log.exception("offer_cmd: get_nation_for_user failed")
        target = None
    if not target:
        return await safe_send_or_followup(interaction, content="That user is not linked to a nation.", ephemeral=True)

    from_nation = nation["nation_id"]
    to_nation = target["nation_id"]

    offered = {}
    requested = {}
    if action.value == "send":
        offered[resource] = float(quantity)
    else:
        requested[resource] = float(quantity)

    # try to estimate transport cost
    try:
        est = await trade_service.estimate_transport_cost(from_nation, to_nation, offered, requested)
        est_cost = est.get("transport_cost", 0.0)
    except Exception:
        est_cost = 0.0

    emb = discord.Embed(title="Offer Preview", color=0x2ECC71)
    emb.add_field(name="From", value=str(from_nation), inline=True)
    emb.add_field(name="To", value=str(to_nation), inline=True)
    if offered:
        emb.add_field(name="You Offer", value="\n".join(f"{k} × {v}" for k, v in offered.items()), inline=False)
    if requested:
        emb.add_field(name="You Request", value="\n".join(f"{k} × {v}" for k, v in requested.items()), inline=False)
    emb.add_field(name="Cash escrowed", value=f"${offered_cash:.2f}", inline=True)
    emb.add_field(name="Estimated transport cost", value=f"${est_cost:.2f}", inline=True)
    emb.set_footer(text="Click Confirm to create the offer and notify recipient in this channel.")

    class ConfirmView(discord.ui.View):
        def __init__(self, timeout: int = 60):
            super().__init__(timeout=timeout)

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
        async def confirm(self, itx: discord.Interaction, button: discord.ui.Button):
            await itx.response.defer(ephemeral=True)
            res = await trade_service.create_offer(from_nation, to_nation, offered, requested, offered_cash=offered_cash, requested_cash=0.0, transport_mode="auto")
            if not res.get("ok"):
                await itx.followup.send(f"❌ {res.get('error')}", ephemeral=True)
                self.stop(); return
            offer_id = res.get("offer_id")
            # notify recipient in same channel (best-effort)
            try:
                if itx.channel:
                    await itx.channel.send(f"{to_user.mention} — you have received a trade offer (ID {offer_id}) from **{from_nation}**. Use `/offeraccept {offer_id}` to accept.")
                else:
                    try:
                        await to_user.send(f"You have received a trade offer (ID {offer_id}) from {from_nation}. Use `/offeraccept {offer_id}` to accept.")
                    except Exception:
                        pass
            except Exception:
                log.exception("offer confirm: notify failure")
            await itx.followup.send(f"✅ Offer created (id {offer_id}). The recipient was notified.", ephemeral=True)
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
        async def cancel(self, itx: discord.Interaction, button: discord.ui.Button):
            await itx.response.send_message("Cancelled offer.", ephemeral=True)
            self.stop()

        async def on_error(self, itx: discord.Interaction, error: Exception, item: discord.ui.Item):
            log.exception("ConfirmView error")
            try:
                if not itx.response.is_done():
                    await itx.response.send_message("⚠️ An error occurred.", ephemeral=True)
                else:
                    await itx.followup.send("⚠️ An error occurred.", ephemeral=True)
            except Exception:
                pass

    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
        await interaction.followup.send(embed=emb, view=ConfirmView(), ephemeral=True)
    except Exception:
        await safe_defer_and_followup(interaction, embed=emb, view=ConfirmView(), ephemeral=True)

# -----------------------
# Market accept, offer list, cancel
# -----------------------
@tree.command(name="marketaccept", description="Accept a market post (creates offer the poster must confirm)")
@app_commands.describe(post_id="Market post ID")
async def marketaccept_cmd(interaction: discord.Interaction, post_id: int):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    res = await trade_service.accept_market_post(nation["nation_id"], post_id)
    if not res.get("ok"):
        await interaction.followup.send(f"❌ {res.get('error')}")
        return
    seller_discord = res.get("seller_discord"); offer_id = res.get("offer_id")
    if seller_discord:
        try:
            user = await client.fetch_user(int(seller_discord))
            await user.send(f"You have a market purchase request (offer id {offer_id}). Use `/offeraccept {offer_id}` to accept.")
            await interaction.followup.send(f"✅ Offer created and seller notified (offer id {offer_id})")
        except Exception as e:
            await interaction.followup.send(f"✅ Offer created ({offer_id}) but failed to DM seller: {e}")
    else:
        await interaction.followup.send(f"✅ Offer created as id {offer_id}. Seller has no linked Discord to DM.")

@tree.command(name="offerlist", description="List your offers; default shows open offers only. Use full=True to show all.")
@app_commands.describe(full="Set to true to show all offers (not only open)")
async def offerlist_cmd(interaction: discord.Interaction, full: bool = False):
    nation = await require_nation(interaction)
    if not nation:
        return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
        offers = await trade_service.list_offers_for_nation(nation["nation_id"])
        if not offers:
            return await interaction.followup.send("You have no offers.", ephemeral=True)

        if not full:
            offers = [o for o in offers if (o.get("status") or "").lower() == "open"]

        per_page = 5
        pages = []
        for i in range(0, len(offers), per_page):
            chunk = offers[i:i+per_page]
            emb = discord.Embed(title="Your Offers", color=0x7289DA)
            for o in chunk:
                oid = o.get("id") or o.get("offer_id") or "?"
                to_n = o.get("to_nation")
                status = o.get("status") or "unknown"
                created = o.get("created_at") or ""
                offered_cash = float(o.get("offered_cash") or 0)
                try:
                    offered_js = json.loads(o.get("offered_json") or "{}")
                except Exception:
                    offered_js = o.get("offered_json") or {}
                try:
                    requested_js = json.loads(o.get("requested_json") or "{}")
                except Exception:
                    requested_js = o.get("requested_json") or {}
                parts = []
                if offered_js:
                    parts.append("Offered: " + ", ".join(f"{k}×{v}" for k,v in offered_js.items()))
                if requested_js:
                    parts.append("Requested: " + ", ".join(f"{k}×{v}" for k,v in requested_js.items()))
                if offered_cash:
                    parts.append(f"Cash escrowed: ${offered_cash:.2f}")
                parts.append(f"Status: {status} • Created: {created}")
                emb.add_field(name=f"Offer ID {oid} → {to_n}", value="\n".join(parts), inline=False)
            pages.append(emb)

        class OfferPaginator(discord.ui.View):
            def __init__(self, pages, owner_id: int, timeout=120):
                super().__init__(timeout=timeout)
                self.pages = pages
                self.idx = 0
                self.owner_id = owner_id

            def _page(self):
                e = self.pages[self.idx]
                e.set_footer(text=f"Page {self.idx+1}/{len(self.pages)}")
                return e

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                if interaction.user.id != self.owner_id:
                    await interaction.response.send_message("This paginator isn't for you.", ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="◀️", style=discord.ButtonStyle.blurple)
            async def prev(self, itx: discord.Interaction, button: discord.ui.Button):
                self.idx = max(0, self.idx - 1)
                try:
                    await itx.response.edit_message(embed=self._page(), view=self)
                except Exception:
                    await itx.followup.send(embed=self._page(), ephemeral=True)

            @discord.ui.button(label="▶️", style=discord.ButtonStyle.blurple)
            async def next(self, itx: discord.Interaction, button: discord.ui.Button):
                self.idx = min(len(self.pages)-1, self.idx + 1)
                try:
                    await itx.response.edit_message(embed=self._page(), view=self)
                except Exception:
                    await itx.followup.send(embed=self._page(), ephemeral=True)

        view = OfferPaginator(pages, owner_id=interaction.user.id)
        await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)
    except Exception as e:
        log.exception("offerlist_cmd failed")
        await safe_defer_and_followup(interaction, content=f"❌ Failed to list offers: {e}", ephemeral=True)

@tree.command(name="canceloffer", description="Cancel an outstanding offer you created (id)")
@app_commands.describe(offer_id="Offer ID to cancel")
async def canceloffer_cmd(interaction: discord.Interaction, offer_id: int):
    nation = await require_nation(interaction)
    if not nation:
        return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
        res = await trade_service.cancel_offer(offer_id, nation["nation_id"])
        if res.get("ok"):
            await interaction.followup.send(f"✅ Offer {offer_id} cancelled. Refunded: ${res.get('refunded',0):.2f}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)
    except Exception as e:
        log.exception("canceloffer_cmd failed")
        await safe_defer_and_followup(interaction, content=f"❌ Cancel failed: {e}", ephemeral=True)

@tree.command(name="offeraccept", description="Accept a trade offer (id)")
@app_commands.describe(offer_id="Offer ID")
async def offeraccept_cmd(interaction: discord.Interaction, offer_id: int):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    res = await trade_service.accept_offer(offer_id, nation["nation_id"])
    if res.get("ok"):
        await interaction.followup.send(f"✅ Offer accepted. Transport cost: ${res.get('transport_cost'):.2f}")
    else:
        await interaction.followup.send(f"❌ {res.get('error')}")

# -----------------------
# Building commands (delegate to services.build)
# -----------------------
@tree.command(name="startbuild", description="Start a build in a state")
@app_commands.describe(state="State ID", building="Building template id", tier="Tier (1-3)")
@app_commands.autocomplete(state=autocomplete.state_autocomplete)
@app_commands.autocomplete(building=autocomplete.building_autocomplete)
async def startbuild_cmd(interaction: discord.Interaction, state: str, building: str, tier: int = 1):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    res = await build_service.start_build(nation["nation_id"], state, building, tier)
    if res.get("ok"):
        await interaction.followup.send(f"✅ Build queued. Build id: {res.get('build_id')}. Complete turn: {res.get('complete_turn')}")
    else:
        await interaction.followup.send(f"❌ {res.get('error')}")

@tree.command(name="cancelbuild", description="Cancel a pending build (frees reservations)")
@app_commands.describe(build_id="Pending build id")
async def cancelbuild_cmd(interaction: discord.Interaction, build_id: int):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    res = await build_service.cancel_build(nation["nation_id"], build_id)
    if res.get("ok"):
        await interaction.followup.send("✅ Build cancelled and resources freed.")
    else:
        await interaction.followup.send(f"❌ {res.get('error')}")

@tree.command(name="demolish", description="Demolish an installed building (no refund). Use state + building + tier.")
@app_commands.describe(state="State (autocomplete)", building="Building (autocomplete)", tier="Tier (1-3)")
@app_commands.autocomplete(state=ac.state_autocomplete)
@app_commands.autocomplete(building=ac.building_autocomplete)
async def demolish_cmd(interaction: discord.Interaction, state: str, building: str, tier: int = 1):
    nation = await require_nation(interaction)
    if not nation:
        return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)

    emb = discord.Embed(title="Confirm Demolish", color=0xE74C3C)
    emb.description = f"Demolish one **{building}** (tier {tier}) in **{state}**. This gives no refund but frees up manpower."

    class ConfirmDemolishView(discord.ui.View):
        def __init__(self, timeout=30):
            super().__init__(timeout=timeout)

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
        async def confirm(self, itx: discord.Interaction, button: discord.ui.Button):
            await itx.response.defer(ephemeral=True)
            res = await build_service.demolish_by_spec(nation["nation_id"], state, building, tier)
            if res.get("ok"):
                await itx.followup.send(f"✅ Demolished {res.get('removed')} in province {res.get('province_id')}. No refund.", ephemeral=True)
            else:
                await itx.followup.send(f"❌ {res.get('error')}", ephemeral=True)
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, itx: discord.Interaction, button: discord.ui.Button):
            await itx.response.send_message("Cancelled demolish.", ephemeral=True)
            self.stop()

        async def on_error(self, itx: discord.Interaction, error: Exception, item: discord.ui.Item):
            log.exception("ConfirmDemolishView error")
            try:
                if not itx.response.is_done():
                    await itx.response.send_message("Error while demolishing.", ephemeral=True)
                else:
                    await itx.followup.send("Error while demolishing.", ephemeral=True)
            except Exception:
                pass

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=emb, view=ConfirmDemolishView(), ephemeral=True)
        else:
            await interaction.followup.send(embed=emb, view=ConfirmDemolishView(), ephemeral=True)
    except Exception:
        await safe_send_or_followup(interaction, content="Unable to show demolish confirmation.", ephemeral=True)

@tree.command(name="buildingqueue", description="Show your nation's building queue")
async def buildingqueue_cmd(interaction: discord.Interaction):
    nation = await require_nation(interaction)
    if not nation:
        return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)

    if not hasattr(build_service, "get_build_queue"):
        return await safe_defer_and_followup(interaction, content="Build service missing `get_build_queue`. Ask an admin to add it.", ephemeral=True)

    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
        queue = await build_service.get_build_queue(nation["nation_id"])
    except Exception as e:
        log.exception("buildingqueue: failed to fetch queue")
        return await safe_defer_and_followup(interaction, content=f"❌ Failed to load build queue: {e}", ephemeral=True)

    if not queue:
        return await safe_defer_and_followup(interaction, content="No buildings currently queued.", ephemeral=True)

    try:
        if hasattr(build_service, "build_buildqueue_embed"):
            emb = build_service.build_buildqueue_embed(queue)
            await safe_defer_and_followup(interaction, embed=emb, ephemeral=True)
            return
    except Exception:
        log.exception("buildingqueue: embed builder failed; falling back")

    emb = discord.Embed(title="Building Queue", color=0x95A5A6)
    for b in queue[:50]:
        bname = b.get("building_name") or b.get("building_template") or str(b.get("building"))
        state = b.get("state_name") or b.get("state_id") or "unknown"
        tier = b.get("tier") or "?"
        complete = b.get("complete_turn") or b.get("complete_at") or "?"
        emb.add_field(name=f"{bname} (Tier {tier})", value=f"State: {state}\nComplete turn: {complete}", inline=False)
    await safe_defer_and_followup(interaction, embed=emb, ephemeral=True)

# -----------------------
# Nation & State commands
# -----------------------
@tree.command(name="nation", description="Show nation overview (population, manpower, income estimate). Optionally @ a user to view theirs.")
@app_commands.describe(user="Mention a user to view their nation instead of yourself")
async def nation_cmd(interaction: discord.Interaction, user: Optional[discord.User] = None):
    """
    Show nation overview. If `user` is provided (mention), attempt to show that user's nation.
    This resolves nations by:
      1) calling get_nation_for_user(discord_id) if available,
      2) falling back to direct DB queries on playernations.owner_discord_id,
         then to nation_players membership.
    """

    # determine target discord id (string)
    target_discord_id = str(user.id) if user else str(interaction.user.id)

    # try preferred helper first (if it exists)
    target_nation = None
    try:
        target_nation = await get_nation_for_user(target_discord_id)
    except Exception:
        # if helper isn't available or errors, we'll fallback to DB queries below
        target_nation = None

    # fallback: direct DB lookup (check owner_discord_id first, then nation_players)
    if not target_nation:
        conn = await get_conn()
        try:
            # 1) owner_discord_id match
            cur = await conn.execute("SELECT * FROM playernations WHERE owner_discord_id = ? LIMIT 1", (target_discord_id,))
            row = await cur.fetchone()
            if row:
                target_nation = dict(row)
            else:
                # 2) membership in nation_players
                cur = await conn.execute("""
                    SELECT pn.* FROM playernations pn
                    JOIN nation_players np ON pn.nation_id = np.nation_id
                    WHERE np.discord_id = ?
                    LIMIT 1
                """, (target_discord_id,))
                row2 = await cur.fetchone()
                if row2:
                    target_nation = dict(row2)
        finally:
            await conn.close()

    # if still not found, inform user
    if not target_nation:
        if user:
            await safe_send_or_followup(interaction, content=f"{user.mention} is not linked to a nation (owner or member).", ephemeral=True)
        else:
            await safe_send_or_followup(interaction, content="You are not linked to a nation (owner or member). Ask an admin to add you.", ephemeral=True)
        return

    # resolved: nation_id
    nation_id = target_nation.get("nation_id") or target_nation.get("id") or target_nation.get("name") or None
    if not nation_id:
        # defensive fallback
        await safe_send_or_followup(interaction, content="Could not determine nation id for that player. Ask an admin to check the DB.", ephemeral=True)
        return

    # fetch overview from service
    await interaction.response.defer()
    overview = await nation_service.get_nation_overview(str(nation_id))
    if overview.get("error"):
        await interaction.followup.send(f"❌ Failed to load nation overview: {overview.get('error')}", ephemeral=True)
        return

    # Build compact embed (no per-province listing; only counts)
    title_name = target_nation.get("name") or str(nation_id)
    emb = discord.Embed(title=f"Nation — {title_name}", color=0x3498DB)

    cash = overview.get("cash")
    pop = overview.get("population_total") or 0
    manpower_total = overview.get("manpower_total")
    manpower_used = overview.get("manpower_used")
    est_tax = overview.get("estimated_tax_income")

    emb.add_field(name="Cash", value=f"${int(cash):,}" if cash is not None else "N/A", inline=True)
    emb.add_field(name="Population", value=f"{int(pop):,}", inline=True)
    if manpower_total is not None or manpower_used is not None:
        emb.add_field(name="Manpower", value=f"{int(manpower_used or 0):,} used / {int(manpower_total or 0):,} total", inline=True)
    if est_tax is not None:
        emb.add_field(name="Est. tax / turn", value=f"${int(est_tax):,}", inline=True)

    # players (primary + secondaries)
    players = overview.get("players", []) or []
    if players:
        player_lines = []
        for p in players[:20]:
            did = str(p.get("discord_id"))
            role = p.get("role", "secondary")
            label = "Primary" if role == "primary" else (str(role).capitalize() or "Secondary")
            player_lines.append(f"<@{did}> — {label}")
        emb.add_field(name=f"Players ({len(players)})", value="\n".join(player_lines[:10]) + ("" if len(player_lines) <= 10 else f"\n...and {len(player_lines)-10} more"), inline=False)

    # provinces count only
    provinces = overview.get("provinces", []) or []
    emb.add_field(name="Provinces controlled", value=str(len(provinces)), inline=True)

    # states summary (compact)
    states = overview.get("states", {}) or {}
    if states:
        lines = []
        for sid, sdata in list(states.items())[:8]:
            sname = sdata.get("state_name") or str(sid)
            pc = sdata.get("province_count", 0)
            lines.append(f"**{sname}** — {pc} prov(s)")
        more = "" if len(states) <= 8 else f"\n...and {len(states)-8} more"
        emb.add_field(name=f"States ({len(states)})", value="\n".join(lines) + more, inline=False)

    await interaction.followup.send(embed=emb)



@tree.command(name="state", description="Show state overview (population, stockpiles, manpower, buildings, production)")
@app_commands.describe(state="State ID")
@app_commands.autocomplete(state=ac.state_autocomplete)
async def state_cmd(interaction: discord.Interaction, state: str):
    nation = await require_nation(interaction)
    if not nation:
        return
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except Exception:
        try:
            await interaction.response.defer()
        except Exception:
            pass

    info = await build_service.get_state_info(nation["nation_id"], state)
    if info.get("error"):
        await interaction.followup.send(f"❌ {info.get('error')}")
        return
    try:
        emb = build_service.build_state_embed(info)
    except Exception:
        emb = discord.Embed(title=f"State — {info.get('name')}", color=0x1ABC9C)
        emb.add_field(name="Provinces", value=str(info.get("provinces_count", 0)))
        emb.add_field(name="Population", value=f"{info.get('population_total'):,}")
    await interaction.followup.send(embed=emb)


# in bot.py, add near other command handlers
import services.buildings as buildings_service

@tree.command(name="buildings", description="Show building templates available and their costs/outputs.")
async def buildings_cmd(interaction: discord.Interaction):
    nation = await require_nation(interaction)
    if not nation:
        return
    # defer & call service
    await buildings_service.handle_buildings_command(interaction, nation_id=nation["nation_id"])





# -----------------------
# Recruit Commands
# -----------------------

# in services/autocomplete.py (example)
from db import get_conn

# -------------------------
# Autocomplete callbacks for units & armies
# -------------------------
# ----- Autocomplete callbacks (add these or replace previous) -----
async def unit_autocomplete(interaction: discord.Interaction, current: str):
    """
    Provide drilldown autocomplete: categories first; if a category exactly matched,
    returns unit templates in that category.
    """
    discord_id = str(interaction.user.id)
    nation = await get_nation_for_user(discord_id)
    if not nation:
        return []
    opts = await recruit_service.unit_autocomplete_for_nation(nation["nation_id"], prefix=current)
    return [app_commands.Choice(name=label, value=tid) for tid, label in opts]

async def army_autocomplete(interaction: discord.Interaction, current: str):
    discord_id = str(interaction.user.id)
    nation = await get_nation_for_user(discord_id)
    if not nation:
        return []
    opts = await recruit_service.list_armies_for_nation(nation["nation_id"], prefix=current)
    return [app_commands.Choice(name=label, value=str(aid)) for aid, label in opts]

async def state_autocomplete(interaction: discord.Interaction, current: str):
    """
    Autocomplete for states owned by the calling user's nation.
    Returns a list of app_commands.Choice(name=label, value=state_id).
    """
    discord_id = str(interaction.user.id)
    nation = await get_nation_for_user(discord_id)
    if not nation:
        return []  # user not linked to a nation

    # build_service.owned_states_for_nation returns list of (state_id, label)
    opts = await build_service.owned_states_for_nation(nation["nation_id"], prefix=current)
    choices = [app_commands.Choice(name=label, value=sid) for sid, label in opts]
    return choices

@tree.command(name="units", description="List recruitable unit templates for your nation (autocomplete).")
async def units_cmd(interaction: discord.Interaction):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    options = await recruit_service.list_available_units(nation["nation_id"], prefix="")
    if not options:
        await interaction.followup.send("No unit templates available.")
        return
    # build a compact reply listing top N templates
    lines = [f"{label}" for tid, label in options[:50]]
    await interaction.followup.send("Available units:\n" + "\n".join(lines))



# ----- army create command (if you don't have one already) -----
@tree.command(name="army_create", description="Create a new army in a province (state drilldown shown in reply).")
@app_commands.describe(name="Army name", province="Province id where to base the army (autocomplete with state/province helper if available)")
@app_commands.autocomplete(province=state_autocomplete)
async def army_create_cmd(interaction: discord.Interaction, name: str, province: str):
    nation = await require_nation(interaction)
    if not nation: return
    await interaction.response.defer()
    res = await recruit_service.create_army(nation["nation_id"], name, province)
    if res.get("ok"):
        await interaction.followup.send(f"✅ Army created: id {res.get('army_id')}")
    else:
        await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)




# -------------------------
# /army create - create a new army (required before recruiting into it)
# -------------------------
@tree.command(name="army", description="Show army details (units, manpower totals).")
@app_commands.describe(army="Army id or name (autocomplete)")
@app_commands.autocomplete(army=army_autocomplete)
async def army_cmd(interaction: discord.Interaction, army: str):
    nation = await require_nation(interaction)
    if not nation: return
    await interaction.response.defer()
    res = await recruit_service.get_army_details(nation["nation_id"], army)
    if res.get("error"):
        await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True); return
    army = res["army"]
    units = res["units"]
    emb = discord.Embed(title=f"Army — {army.get('name')} (id {army.get('id')})", color=0x34495E)
    emb.add_field(name="Province", value=str(army.get("province_id") or "—"), inline=True)
    total_manpower = 0
    total_units = 0
    by_type = {}
    for u in units:
        tid = u.get("template_id")
        qty = int(u.get("quantity") or 0)
        total_units += qty
        # fetch template for category lookup
        tpl = await recruit_service.get_unit_template(tid)
        cat = tpl.get("category") if tpl else "Unknown"
        by_type.setdefault(cat, 0)
        by_type[cat] += qty
        total_manpower += int((tpl.get("manpower_cost") or 0) * qty)
    emb.add_field(name="Total units", value=str(total_units), inline=True)
    emb.add_field(name="Total manpower (est)", value=f"{total_manpower:,}", inline=True)
    if by_type:
        lines = [f"{k}: {v}" for k,v in by_type.items()]
        emb.add_field(name="By category", value="\n".join(lines), inline=False)
    if units:
        u_lines = [f"{(await recruit_service.get_unit_template(u['template_id']))['display_name'] or u['template_id']} x{u['quantity']}" for u in units]
        # chunk if too long
        text = "\n".join(u_lines)
        if len(text) > 800:
            text = text[:790] + "\n…"
        emb.add_field(name="Units", value=text, inline=False)
    await interaction.followup.send(embed=emb)





# -------------------------
# /recruit - recruit units into an army (requires army id & quantity)
# -------------------------
@tree.command(name="recruit", description="Recruit units into an army (state used for manpower & resources).")
@app_commands.describe(unit="Unit template (autocomplete: type -> name)", quantity="Quantity to recruit", state="State to draw manpower from (autocomplete)", army="Army to assign to (optional, autocomplete)")
@app_commands.autocomplete(unit=unit_autocomplete, state=state_autocomplete, army=army_autocomplete)
async def recruit_cmd(interaction: discord.Interaction, unit: str, quantity: int, state: str, army: Optional[str] = None):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    # unit is template_id (string), validate
    template_id = str(unit)
    # check template exists
    tpl = await recruit_service.get_unit_template(template_id)
    if not tpl:
        await interaction.followup.send("Unit template not found.", ephemeral=True); return
    # run recruit action
    res = await recruit_service.recruit_unit(nation["nation_id"], template_id, quantity, state, army)
    if res.get("ok"):
        await interaction.followup.send(f"✅ Recruit queued (id {res.get('recruit_id')}). Manpower reserved: {res.get('manpower_reserved')}. Cash spent: ${res.get('cash_spent'):.2f}")
    else:
        if res.get("missing"):
            missing = ", ".join([f"{k}: {v}" for k, v in res["missing"].items()])
            await interaction.followup.send(f"❌ Missing: {missing}")
        else:
            await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)




@tree.command(name="list_recruits", description="List pending recruitments (optional: state).")
@app_commands.describe(state="State ID (optional, autocomplete)")
@app_commands.autocomplete(state=state_autocomplete)
async def list_recruits_cmd(interaction: discord.Interaction, state: Optional[str] = None):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    rows = await recruit_service.list_recruits(nation["nation_id"], state)
    if not rows:
        await interaction.followup.send("No pending recruits found.", ephemeral=True)
        return

    # format into an embed (or text if small)
    emb = discord.Embed(title="⏳ Pending Recruits", color=0xF1C40F)
    lines = []
    for r in rows[:80]:
        tpl = r.get("template_id") or "(unknown)"
        name = tpl
        qty = r.get("quantity") or 0
        turn = r.get("complete_turn") or 0
        army = f"{r.get('army_name') or r.get('army_id') or '—'}"
        st = f"{r.get('state_name') or r.get('state_id') or '—'}"
        lines.append(f"**{name}** x{qty} — turn {turn} — army: {army} — state: {st}")
    text = "\n".join(lines)
    # chunk into fields if too large
    CHUNK = 900
    for i in range(0, len(text), CHUNK):
        emb.add_field(name="Recruits", value=text[i:i+CHUNK], inline=False)
    await interaction.followup.send(embed=emb)




# -----------------------
# /disband
# -----------------------
@tree.command(name="disband", description="Disband one of your recruits by ID")
@app_commands.describe(recruit_id="Recruit ID to disband")
async def disband_cmd(interaction: discord.Interaction, recruit_id: int):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    try:
        res = await recruit_service.disband_recruit(nation["nation_id"], recruit_id)
        if res.get("ok"):
            await interaction.followup.send(f"✅ Recruit {recruit_id} disbanded.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)
    except Exception as e:
        log.exception("disband_cmd failed")
        await interaction.followup.send(f"❌ Failed to disband: {e}", ephemeral=True)









# -----------------------
# Resources (state rollup) paginated 5 states/page
# -----------------------
@tree.command(name="resources", description="Show resource rollup by state (paginated, 5 states/page)")
async def resources_cmd(interaction: discord.Interaction):
    nation = await require_nation(interaction)
    if not nation:
        return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)
    await interaction.response.defer()
    try:
        rollup = await build_service.get_resources_rollup(nation["nation_id"])
        if not rollup:
            return await safe_defer_and_followup(interaction, content="No resources found for your nation.", ephemeral=True)

        RESOURCE_EMOJI = {"Raw Ore": "⛏️", "Coal": "🪨", "Oil": "🛢️", "Food": "🌾", "Raw Uranium": "☢️", None: "◻️"}
        state_items = []
        for sid, sdata in rollup.items():
            sname = sdata.get("state_name", sid)
            total = sdata.get("total_provinces", 0)
            resless = sdata.get("resourceless", 0)
            lines = [f"**{sname}** — Provinces: **{total}**"]
            resources = sdata.get("resources", {})
            if resources:
                for rname, rinfo in sorted(resources.items(), key=lambda x: (-x[1].get("provinces",0), x[0])):
                    emoji = RESOURCE_EMOJI.get(rname, "📦")
                    provinces = rinfo.get("provinces", 0)
                    utilized = rinfo.get("utilized", 0)
                    total_avail = rinfo.get("total_available", 0)
                    qualities = rinfo.get("qualities", {})
                    qual_parts = []
                    for qlabel in ("Rich", "Common", "Poor", "Unknown"):
                        qcount = qualities.get(qlabel, 0)
                        if qcount:
                            qual_parts.append(f"{qlabel}:{qcount}")
                    qual_str = ", ".join(qual_parts) if qual_parts else "Unknown"
                    lines.append(f"{emoji} **{rname}** — {provinces} prov(s) — Available: {total_avail} — Utilized: {utilized} — {qual_str}")
            if resless:
                lines.append(f"◻️ **Resourceless** — {resless} prov(s)")
            state_items.append("\n".join(lines))

        pages_embeds = []
        per_page = 5
        for i in range(0, len(state_items), per_page):
            chunk = state_items[i:i+per_page]
            desc = "\n\n".join(chunk)
            emb = discord.Embed(title="Resources — Rollup", description=desc, color=0xF1C40F)
            pages_embeds.append(emb)

        if not pages_embeds:
            return await safe_defer_and_followup(interaction, content="No resource data to display.", ephemeral=True)

        class ResourcesPaginator(discord.ui.View):
            def __init__(self, pages: List[discord.Embed], owner_id: int, timeout: int = 120):
                super().__init__(timeout=timeout)
                self.pages = pages
                self.idx = 0
                self.owner_id = owner_id

            def _page(self) -> discord.Embed:
                e = self.pages[self.idx]
                e.set_footer(text=f"Page {self.idx+1}/{len(self.pages)}")
                return e

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                if interaction.user.id != self.owner_id:
                    await interaction.response.send_message("This paginator isn't for you.", ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="◀️", style=discord.ButtonStyle.blurple)
            async def prev(self, itx: discord.Interaction, button: discord.ui.Button):
                self.idx = max(0, self.idx - 1)
                try:
                    await itx.response.edit_message(embed=self._page(), view=self)
                except Exception:
                    await itx.followup.send(embed=self._page(), ephemeral=True)

            @discord.ui.button(label="▶️", style=discord.ButtonStyle.blurple)
            async def nxt(self, itx: discord.Interaction, button: discord.ui.Button):
                self.idx = min(len(self.pages)-1, self.idx + 1)
                try:
                    await itx.response.edit_message(embed=self._page(), view=self)
                except Exception:
                    await itx.followup.send(embed=self._page(), ephemeral=True)

        view = ResourcesPaginator(pages_embeds, owner_id=interaction.user.id)
        await safe_defer_and_followup(interaction, embed=pages_embeds[0], view=view)
    except Exception as e:
        log.exception("resources_cmd failed")
        await safe_defer_and_followup(interaction, content=f"❌ Failed to load resources: {e}", ephemeral=True)

# -----------------------
# Findbuildings
# -----------------------
@tree.command(name="findbuildings", description="Find which states contain the given building (aggregated)")
@app_commands.describe(building="Building name or id")
@app_commands.autocomplete(building=ac.building_autocomplete)
async def findbuildings_cmd(interaction: discord.Interaction, building: str):
    nation = await require_nation(interaction)
    if not nation:
        return
    await interaction.response.defer()
    agg = await build_service.find_buildings_aggregated(nation["nation_id"], building)
    if not agg:
        return await interaction.followup.send("No matching buildings found.")
    try:
        emb = build_service.build_findbuildings_embed(agg, sum(v.get("count",0) for v in agg.values()), building)
        await interaction.followup.send(embed=emb)
    except Exception:
        lines = []
        for state_name, buildings in agg.items():
            for bname, binfo in buildings.items():
                count = int(binfo.get("count",0))
                tiers = binfo.get("tiers", [])
                lines.append(f"{state_name} — {bname} ×{count} (tiers: {', '.join(map(str, tiers))})")
        await interaction.followup.send("\n".join(lines[:1500]))

# -----------------------
# Endturn (admin)
# -----------------------
@tree.command(name="endturn", description="Advance the game by one turn (admin only)")
async def endturn_cmd(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        next_turn = await economy_service.run_end_turn()
    except Exception as e:
        await interaction.followup.send(f"❌ End turn failed: {e}", ephemeral=True)
        return
    await interaction.followup.send(f"✅ Advanced to turn {next_turn}", ephemeral=True)

# -----------------------
# Admin: JSON import from local path
# -----------------------
@tree.command(name="jsonimport", description="Import local game JSON and apply province-state mapping (admin only)")
async def jsonimport_cmd(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        res = await nation_service.import_json_from_path(GAME_JSON_PATH)
        if not res.get("ok"):
            await interaction.followup.send(f"❌ Import failed: {res.get('error') or res}", ephemeral=True)
            return
        msg = (
            f"✅ JSON import completed.\n"
            f"Provinces updated: {res.get('provinces_updated',0)}\n"
            f"States updated: {res.get('states_updated',0)}\n"
        )
        if res.get("errors"):
            msg += "Some errors occurred:\n" + "\n".join(res["errors"][:10])
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        log.exception("jsonimport_cmd failed")
        await interaction.followup.send(f"❌ Exception: {e}", ephemeral=True)

@tree.command(name="jsondownload", description="Import a JSON file uploaded to Discord (admin only). Attach a .json file to the command.")
@app_commands.describe(attachment="Attach a JSON file to import (country/state/province mappings)")
async def jsondownload_cmd(interaction: discord.Interaction, attachment: discord.Attachment):
    if not await require_admin(interaction):
        return
    if not attachment:
        return await safe_send_or_followup(interaction, content="You must attach a JSON file.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        raw = await attachment.read()
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to read attachment: {e}", ephemeral=True)
        return

    res = await nation_service.import_json_from_bytes(raw)
    if not res.get("ok"):
        await interaction.followup.send(f"❌ Import failed: {res.get('error') or res}", ephemeral=True)
        return

    msg = (
        f"✅ JSON import from attachment completed.\n"
        f"Provinces updated: {res.get('provinces_updated',0)}\n"
        f"States updated: {res.get('states_updated',0)}\n"
    )
    if res.get("errors"):
        msg += "Some errors occurred:\n" + "\n".join(res["errors"][:10])
    await interaction.followup.send(msg, ephemeral=True)

# -----------------------
# Invite Python Scripts
# -----------------------

ROLE_CHOICES = [
    app_commands.Choice(name="Secondary", value="Secondary"),
    app_commands.Choice(name="General", value="General"),
    app_commands.Choice(name="Diplomat", value="Diplomat"),
    app_commands.Choice(name="Citizen", value="Citizen"),
]


# /invite - owner only
@tree.command(name="invite", description="Invite a player to your nation (owner only). Include a role.")
@app_commands.describe(user="User to invite (mention)", role="Role to assign on join")
@app_commands.choices(role=ROLE_CHOICES)
async def invite_cmd(interaction: discord.Interaction, user: discord.User, role: Optional[app_commands.Choice[str]] = None):
    nation = await get_nation_for_user(str(interaction.user.id))
    if not nation:
        return await safe_send_or_followup(interaction, content="You are not linked to any nation.", ephemeral=True)

    owner_id = str(nation.get("owner_discord_id") or "")
    if owner_id != str(interaction.user.id):
        return await safe_send_or_followup(interaction, content="Only the primary owner of the nation can invite players.", ephemeral=True)

    if not user:
        return await safe_send_or_followup(interaction, content="You must mention a user to invite.", ephemeral=True)

    # prevent inviting a primary owner quickly
    conn = await get_conn()
    try:
        cur = await conn.execute("SELECT 1 FROM playernations WHERE owner_discord_id = ? LIMIT 1", (str(user.id),))
        if await cur.fetchone():
            return await safe_send_or_followup(interaction, content="That user is already a primary owner of a nation and cannot be invited.", ephemeral=True)
    finally:
        await conn.close()

    chosen_role = role.value if role else "Secondary"
    await interaction.response.defer(ephemeral=True)
    res = await invite_service.create_invite(str(interaction.user.id), str(user.id), str(nation.get("nation_id")), chosen_role)
    if not res.get("ok"):
        return await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)

    code = res.get("code")
    # DM the user with the code (best-effort)
    dm_sent = True
    try:
        await user.send(f"You have been invited to join **{nation.get('name') or nation.get('nation_id')}** as **{res.get('role')}**. Use `/join {code}` to accept.")
    except Exception:
        dm_sent = False

    msg = f"✅ Invitation created for {user.mention} as **{res.get('role')}**."
    if not dm_sent:
        msg += " Could not DM the user (maybe DMs disabled). They will still be able to join with the code."
    await interaction.followup.send(msg, ephemeral=True)

@tree.command(name="addplayer", description="(Staff) Add a user to a nation (Staff-Visit role). Admin only.")
@app_commands.describe(nation_id="Nation ID to add the user to", user="User to add (mention)", role="Optional role (defaults to Staff-Visit)")
async def addplayer_cmd(interaction: discord.Interaction, nation_id: str, user: discord.User, role: Optional[str] = None):
    if not await require_admin(interaction):
        return
    if not nation_id:
        return await safe_send_or_followup(interaction, content="You must provide a nation id.", ephemeral=True)
    if not user:
        return await safe_send_or_followup(interaction, content="You must mention a user to add.", ephemeral=True)

    chosen_role = role or "Staff-Visit"
    # Normalize role formatting (capitalize first letter, etc.)
    chosen_role = chosen_role.strip()
    # Safety: if role not allowed, return error
    if chosen_role not in invite_service.ALLOWED_ROLES:
        return await safe_send_or_followup(interaction, content=f"Invalid role. Allowed: {', '.join(sorted(invite_service.ALLOWED_ROLES))}", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    res = await invite_service.add_member_by_staff(str(nation_id), str(user.id), chosen_role)
    if res.get("ok"):
        await interaction.followup.send(f"✅ {user.mention} added to nation **{nation_id}** as **{chosen_role}**.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {res.get('error') or res.get('message')}", ephemeral=True)

# /promote — owner (or admin) can change a member's role
@tree.command(name="promote", description="Promote a member's role in your nation (owners) or specify nation_id if admin.")
@app_commands.describe(user="User to promote", role="Role to assign", nation_id="(Admins) Nation ID to operate on")
@app_commands.choices(role=ROLE_CHOICES)
async def promote_cmd(interaction: discord.Interaction, user: discord.User, role: app_commands.Choice[str], nation_id: Optional[str] = None):
    # determine who can act
    is_admin = await require_admin(interaction)
    if is_admin is False:
        # require_admin already sent an ephemeral response; treat as False
        return

    # If not admin, require owner: check invoker's nation
    invoker_row = await get_nation_for_user(str(interaction.user.id))
    invoker_nation_id = invoker_row.get("nation_id") if invoker_row else None
    # If nation_id param provided and invoker is not admin, deny
    if nation_id and not is_admin:
        return await safe_send_or_followup(interaction, content="Only admins may specify a nation_id.", ephemeral=True)

    # pick target nation
    target_nation_id = None
    if is_admin and nation_id:
        target_nation_id = nation_id
    else:
        # ensure invoker is primary owner of invoker_nation_id
        if not invoker_row:
            return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)
        # verify invoker is primary owner
        owner_id = str(invoker_row.get("owner_discord_id") or invoker_row.get("owner") or "")
        if owner_id != str(interaction.user.id):
            return await safe_send_or_followup(interaction, content="Only the primary owner can promote members in your nation.", ephemeral=True)
        target_nation_id = invoker_nation_id

    # final checks
    if not target_nation_id:
        return await safe_send_or_followup(interaction, content="Could not determine target nation id.", ephemeral=True)
    chosen_role = role.value

    await interaction.response.defer(ephemeral=True)
    res = await invite_service.promote_member(target_nation_id, str(user.id), chosen_role)
    if res.get("ok"):
        await interaction.followup.send(f"✅ {user.mention} promoted to **{chosen_role}** in nation **{target_nation_id}**.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)


# /removeplayer — owner or admin can remove a member from a nation
@tree.command(name="removeplayer", description="Remove a member from your nation (owners) or specify nation_id if admin.")
@app_commands.describe(user="User to remove", nation_id="(Admins) Nation ID to operate on")
async def removeplayer_cmd(interaction: discord.Interaction, user: discord.User, nation_id: Optional[str] = None):
    # admin check
    is_admin = await require_admin(interaction)
    if is_admin is False:
        return

    # determine target nation
    invoker_row = await get_nation_for_user(str(interaction.user.id))
    invoker_nation_id = invoker_row.get("nation_id") if invoker_row else None

    if nation_id and not is_admin:
        return await safe_send_or_followup(interaction, content="Only admins may specify a nation_id.", ephemeral=True)

    if is_admin and nation_id:
        target_nation_id = nation_id
    else:
        if not invoker_row:
            return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)
        owner_id = str(invoker_row.get("owner_discord_id") or invoker_row.get("owner") or "")
        if owner_id != str(interaction.user.id):
            return await safe_send_or_followup(interaction, content="Only the primary owner can remove members in your nation.", ephemeral=True)
        target_nation_id = invoker_nation_id

    await interaction.response.defer(ephemeral=True)
    res = await invite_service.remove_member_by_staff(target_nation_id, str(user.id))
    if res.get("ok"):
        await interaction.followup.send(f"✅ {user.mention} removed from nation **{target_nation_id}** (was {res.get('removed_role')}).", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)




# /join - accept via code
@tree.command(name="join", description="Join a nation with an invite code")
@app_commands.describe(code="Invite code received by DM")
async def join_cmd(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)
    res = await invite_service.accept_invite(code, str(interaction.user.id))
    if res.get("ok"):
        await interaction.followup.send(res.get("message", "Joined."), ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {res.get('message')}", ephemeral=True)


# /invitelist - owner only, paginated friendly output
@tree.command(name="invitelist", description="List pending invites for your nation (owner only)")
async def invitelist_cmd(interaction: discord.Interaction):
    nation = await get_nation_for_user(str(interaction.user.id))
    if not nation:
        return await safe_send_or_followup(interaction, content="You are not linked to a nation.", ephemeral=True)
    owner_id = str(nation.get("owner_discord_id") or "")
    if owner_id != str(interaction.user.id):
        return await safe_send_or_followup(interaction, content="Only the primary owner of the nation can view pending invites.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    res = await invite_service.list_pending_invites_for_nation(str(interaction.user.id), str(nation.get("nation_id")))
    if not res.get("ok"):
        return await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)

    invites = res.get("invites") or []
    if not invites:
        return await interaction.followup.send("No pending invites.", ephemeral=True)

    # chunk to pages of 8 invites
    per_page = 8
    pages = []
    for i in range(0, len(invites), per_page):
        chunk = invites[i:i+per_page]
        desc_lines = []
        for itm in chunk:
            invited = itm.get("invited_id")
            code = itm.get("invite_code")
            attempts = itm.get("invite_count", 0)
            desc_lines.append(f"<@{invited}> — `{code}` • attempts: {attempts}")
        emb = discord.Embed(title=f"Pending Invites — {nation.get('name') or nation.get('nation_id')}", description="\n".join(desc_lines), color=0xFAA61A)
        pages.append(emb)

    # Simple paginator using buttons — reuse your existing paginator class if present.
    # If you don't have one, just send first page:
    try:
        # if you have a paginator view class called OfferPaginator or ResourcesPaginator, reuse it.
        view = None
        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0], ephemeral=True)
        else:
            # naive: send first page (without buttons) to avoid adding more UI code here.
            await interaction.followup.send(embed=pages[0], ephemeral=True)
            # (you can later replace this with a proper View-based paginator)
    except Exception:
        await interaction.followup.send("\n".join([f"<@{i['invited_id']}> — `{i['invite_code']}`" for i in invites]), ephemeral=True)


# -----------------------
# Admin: Nation creation & starter config
# -----------------------
# Autocomplete for country names (unclaimed)

async def unowned_playernation_name_autocomplete(interaction: discord.Interaction, current: str):
    """
    Autocomplete returns visible choices like 'Nation Name' (value is the exact name string).
    Pulls live list from DB so new unowned nations appear immediately.
    """
    try:
        names = await nation_service.get_unowned_playernation_names(limit=50)
    except Exception:
        return []
    q = (current or "").strip().lower()
    choices = []
    for nm in names:
        if not nm:
            continue
        if q and q not in nm.lower():
            continue
        try:
            # value must be string; we use name itself as the value
            choices.append(app_commands.Choice(name=nm, value=nm))
        except Exception:
            continue
        if len(choices) >= 25:
            break
    return choices

# Autocomplete for unclaimed JSON countries (existing behaviour kept)
async def json_country_autocomplete(interaction: discord.Interaction, current: str):
    try:
        countries = await nation_service.get_unclaimed_countries()
    except Exception:
        return []
    choices = []
    for c in countries:
        name = str(c.get("name") or "")
        if current and current.lower() not in name.lower():
            continue
        try:
            choices.append(app_commands.Choice(name=name, value=name))
        except Exception:
            continue
        if len(choices) >= 25:
            break
    return choices

@tree.command(name="createnation", description="Assign an existing unowned nation to a user and apply starter resources (admin only)")
@app_commands.describe(user="Mention the user to assign the nation to", existing_nation="Select an unowned nation (autocomplete)")
@app_commands.autocomplete(existing_nation=unowned_playernation_name_autocomplete)
async def createnation_cmd(interaction: discord.Interaction, user: discord.User, existing_nation: Optional[str] = None):
    """
    If existing_nation (name) provided, assign that unowned playernation to 'user' and apply starters.
    Admin-only.
    """
    if not await require_admin(interaction):
        return
    if not existing_nation:
        await safe_defer_and_followup(interaction, content="You must pick an existing unowned nation from the autocomplete.", ephemeral=True)
        return

    # Run assignment
    await interaction.response.defer(ephemeral=True)
    try:
        res = await nation_service.assign_existing_nation_by_name(str(interaction.user.id), str(user.id), existing_nation, apply_starters=True)
    except Exception as e:
        log.exception("createnation_cmd assign failed")
        await interaction.followup.send(f"❌ Exception during assign: {e}", ephemeral=True)
        return

    if not res.get("ok"):
        # Provide helpful debug: if matches suggested, show them
        msg = f"❌ Failed to assign: {res.get('errors') or res}"
        if res.get("matches"):
            # show short list of matches for admin to choose from
            sample = res["matches"]
            lines = []
            for r in sample[:8]:
                nm = r.get("name") or r.get("nation") or str(r.get("rowid"))
                rid = r.get("rowid") or r.get("nation_id") or r.get("id")
                lines.append(f"- {nm} (rowid:{rid})")
            msg += "\nCandidates:\n" + "\n".join(lines)
        await interaction.followup.send(msg, ephemeral=True)
        return

    # Success — show summary embed
    row = res.get("nation_row") or {}
    emb = discord.Embed(title="Nation Assigned", color=0x2ECC71)
    emb.add_field(name="Nation name", value=str(row.get("name") or existing_nation), inline=True)
    emb.add_field(name="Assigned to", value=f"<@{user.id}>", inline=True)
    # include canonical nation id if present
    canonical = row.get("nation_id") or row.get("id") or row.get("rowid")
    emb.add_field(name="Nation id", value=str(canonical), inline=True)

    # starters summary
    sr = res.get("starter_resources_result") or {}
    sb = res.get("starter_buildings_result") or {}
    if sr:
        emb.add_field(name="Resources distributed", value=json.dumps(sr.get("distributed", {}))[:1000], inline=False)
        if sr.get("errors"):
            emb.add_field(name="Resource errors", value="\n".join(sr["errors"][:6])[:1000], inline=False)
    if sb:
        emb.add_field(name="Buildings inserted", value=str(sb.get("inserted", 0)), inline=True)
        if sb.get("skipped"):
            emb.add_field(name="Skipped", value=json.dumps(sb.get("skipped"))[:1000], inline=False)
        if sb.get("errors"):
            emb.add_field(name="Building errors", value="\n".join(sb["errors"][:6])[:1000], inline=False)

    try:
        await interaction.followup.send(embed=emb, ephemeral=True)
    except Exception:
        # fallback
        await interaction.followup.send("✅ Assigned (embed failed to render).", ephemeral=True)

# /starter combined admin command
from discord import app_commands

STARTER_ACTIONS = [
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="subtract", value="subtract"),
    app_commands.Choice(name="set", value="set"),
    app_commands.Choice(name="list", value="list"),
]

STARTER_TYPES = [
    app_commands.Choice(name="cash", value="cash"),
    app_commands.Choice(name="population", value="population"),
    app_commands.Choice(name="resource", value="resource"),
    app_commands.Choice(name="building", value="building"),
    app_commands.Choice(name="tech", value="tech"),
]

@tree.command(name="starter", description="Admin: manage starter cash/population/resources/buildings/tech for a nation")
@app_commands.describe(action="add/subtract/set/list", type="cash/population/resource/building/tech", nation="Nation id or name",
                       amount="Amount for cash or resource (numeric)", resource="Resource name (for resource ops)",
                       building="Building template id/name (for building ops)", count="Number (for buildings)", tier="Tier for building", tech="Tech id (for tech ops)")
@app_commands.choices(action=STARTER_ACTIONS, type=STARTER_TYPES)
async def starter_cmd(interaction: discord.Interaction,
                      action: app_commands.Choice[str],
                      type: app_commands.Choice[str],
                      nation: str,
                      amount: Optional[float] = None,
                      resource: Optional[str] = None,
                      building: Optional[str] = None,
                      count: Optional[int] = 1,
                      tier: Optional[int] = 1,
                      tech: Optional[str] = None):
    # Admin-only guard
    if not await require_admin(interaction):
        return

    # Defer early (admin ephemeral)
    await interaction.response.defer(ephemeral=True)

    # map choices
    act = action.value
    typ = type.value

    try:
        res = await starter_service.manage_starter(
            action=act,
            typ=typ,
            nation_identifier=nation,
            amount=float(amount) if amount is not None else None,
            resource=resource,
            building_template=building,
            count=int(count) if count is not None else None,
            tier=int(tier) if tier is not None else None,
            tech_id=tech
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Starter operation failed: {e}", ephemeral=True)
        return

    # Format response
    if not res.get("ok"):
        msg = res.get("error") or str(res)
        await interaction.followup.send(f"❌ {msg}", ephemeral=True)
        return

    # Build a readable success message
    import json as _json
    pretty = _json.dumps(res, indent=2, default=str)
    # If too long, send as file; else send as ephemeral message
    if len(pretty) > 1800:
        # send as file attachment
        from io import StringIO
        buf = StringIO(pretty)
        buf.seek(0)
        await interaction.followup.send(file=discord.File(buf, filename="starter_result.json"), ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Starter op result:\n```\n{pretty}\n```", ephemeral=True)


@tree.command(name="sync", description="Admin: clear guild commands then re-sync (no params).")
async def sync_cmd(interaction: discord.Interaction):
    caller = str(interaction.user.id)
    if not await is_admin(caller):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    # respond immediately so we can follow up
    await interaction.response.send_message("Clearing and re-syncing commands for guild...", ephemeral=True)
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.clear_commands(guild=guild_obj)
            await tree.sync(guild=guild_obj)
            await interaction.followup.send("Cleared and re-synced guild commands.", ephemeral=True)
        else:
            tree.clear_commands()
            await tree.sync()
            await interaction.followup.send("Cleared and re-synced global commands. Note: global changes may take time to appear.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Sync failed: {e}. If this persists, remove old commands manually via the Developer Portal.", ephemeral=True)



# ---------------------------------------------------------------------------

@tree.command(name="add_modifier", description="(Admin) Add an economy modifier")
@app_commands.describe(scope="global|nation|state|province", scope_id="ID for the scope (omit for global)", effect="production|population|tax|all", kind="mul|add", value="Multiplier (mul) or additive fraction (add)", expires_turn="Optional turn where modifier expires")
async def add_modifier_cmd(interaction: discord.Interaction, scope: str, scope_id: Optional[str], effect: str, kind: str, value: float, expires_turn: Optional[int] = None):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    res = await mod_service.add_modifier(scope, scope_id, effect, kind, value, source=f"admin:{interaction.user.id}", created_turn=None, expires_turn=expires_turn)
    if res.get("ok"):
        await interaction.followup.send(f"✅ Modifier added (id {res.get('id')})", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)


@tree.command(name="list_modifiers", description="(Admin) List modifiers")
async def list_modifiers_cmd(interaction: discord.Interaction, scope: Optional[str] = None, scope_id: Optional[str] = None):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    rows = await mod_service.list_modifiers(scope, scope_id, only_active=True)
    if not rows:
        await interaction.followup.send("No modifiers found.", ephemeral=True); return
    lines = []
    for r in rows[:40]:
        lines.append(f"#{r['id']} [{r['scope']}/{r.get('scope_id')}] {r['effect']} {r['kind']}={r['value']} src={r.get('source')} expires={r.get('expires_turn')}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@tree.command(name="remove_modifier", description="(Admin) Remove modifier by id")
@app_commands.describe(mod_id="Modifier id")
async def remove_modifier_cmd(interaction: discord.Interaction, mod_id: int):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    res = await mod_service.remove_modifier(mod_id)
    if res.get("ok"):
        await interaction.followup.send("✅ Removed modifier.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {res.get('error')}", ephemeral=True)


TEST_GUILD_ID = 973051008777326612

GUILD_ID = 973051008777326612

# -----------------------
# on_ready: sync commands
# -----------------------

@client.event
async def on_ready():
    # Print local commands currently attached to the tree
    try:
        local_cmds = list(tree.walk_commands())
        log.info("Local commands defined on tree: %d", len(local_cmds))
        for c in local_cmds:
            log.info(" - %s (%s)", c.name, type(c).__name__)
    except Exception:
        log.exception("Failed enumerating tree commands")

    # try safe sync to a guild for quick visibility (admin test guild)
    try:
        if TEST_GUILD_ID:
            g = discord.Object(id=TEST_GUILD_ID)
            await tree.sync(guild=g)
            log.info("Successfully synced commands to guild %s", TEST_GUILD_ID)
        else:
            # fallback global sync (may take time to appear)
            await tree.sync()
            log.info("Successfully performed global sync")
    except Exception:
        log.error("tree.sync() failed")
        traceback.print_exc()

    print(f"Logged in as {client.user} ({client.user.id})")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Missing DISCORD_TOKEN in key.env")
    else:
        client.run(DISCORD_TOKEN)
