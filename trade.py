# services/trade.py
"""
Trade service (full). Defensive; includes market UI helpers, market posting, offers,
accept/cancel flows, and transport estimation.
Assumes db.get_conn() returns an aiosqlite connection with row_factory set.
"""

import json
import math
import datetime
import logging
import sqlite3
from typing import Dict, Any, List, Tuple, Optional

from db import get_conn
import discord

POSTS_PER_PAGE = 5
RESOURCE_EMOJI = {
    "Raw Ore": "⛏️",
    "Coal": "🪨",
    "Oil": "🛢️",
    "Food": "🌾",
    "Raw Uranium": "☢️",
    "Iron": "🔩",
    "Fuel": "⛽",
    "Military Goods": "🪖",
    "Steel": "🏗️",
    None: "📦"
}

log = logging.getLogger(__name__)

BASE_RATE_PER_KG_KM = 0.00008
MODE_FACTOR = {"land": 1.0, "rail": 0.7, "sea": 0.4, "auto": 1.0}


def compute_transport_cost(weight_kg: float, distance_km: float, mode: str = "auto") -> float:
    mf = MODE_FACTOR.get(mode, 1.0)
    try:
        return float(weight_kg) * float(distance_km) * BASE_RATE_PER_KG_KM * float(mf)
    except Exception:
        return 0.0


def _row_to_dict(row) -> Dict[str, Any]:
    """Convert a sqlite row (or dict) to a plain dict robustly."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        # row.keys() works for sqlite3.Row
        return {k: row[k] for k in row.keys()}
    except Exception:
        try:
            return dict(row)
        except Exception:
            # final fallback: try as iterable of pairs
            try:
                return {k: v for k, v in row}
            except Exception:
                return {}


# ------------------------
# Market (UI)
# ------------------------
async def _fetch_market_posts(resource: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return list of market_posts as dicts, newest first.
    """
    conn = await get_conn()
    try:
        if resource:
            cur = await conn.execute("SELECT * FROM market_posts WHERE resource = ? ORDER BY created_at DESC", (resource,))
        else:
            cur = await conn.execute("SELECT * FROM market_posts ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        try:
            await conn.close()
        except Exception:
            pass


def _format_money(v: float) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v)


def _build_post_line(post: Dict[str, Any]) -> str:
    """Render one-line description of a post robustly."""
    pid = post.get("id") or post.get("post_id") or post.get("rowid") or post.get("market_id") or "?"
    poster = post.get("poster_nation") or post.get("nation_id") or post.get("poster") or post.get("owner") or "Unknown"
    # find resource name
    rname = post.get("resource")
    if not rname:
        try:
            offered = json.loads(post.get("offered_json") or "{}")
            rname = next(iter(offered.keys())) if offered else "Resource"
        except Exception:
            rname = "Resource"
    qty = post.get("quantity") or post.get("amount") or post.get("units") or 0
    price = post.get("price_per_unit") or post.get("price") or post.get("ask_price") or post.get("unit_price") or 0.0
    transport = post.get("transport_mode") or post.get("mode") or "auto"
    emoji = RESOURCE_EMOJI.get(rname, "📦")
    try:
        qty_display = f"{int(qty):,}"
    except Exception:
        qty_display = str(qty)
    return f"**ID {pid}** • {emoji} **{rname}** ×{qty_display} • {_format_money(price)}/unit • Seller: **{poster}** • {transport}"


async def create_market_embed_and_view(user_id: int, resource: Optional[str] = None, page: int = 0) -> Tuple[discord.Embed, Optional[discord.ui.View]]:
    """
    Polished market embed + interactive View with pagination and select menu.
    Returns (embed, view) where view can be None when no posts.
    page is 0-based.
    """
    try:
        page = int(page or 0)
    except Exception:
        page = 0

    posts = await _fetch_market_posts(resource=resource)
    total = len(posts)
    pages = max(1, math.ceil(total / POSTS_PER_PAGE)) if total else 1
    page = max(0, min(page, pages - 1))

    start = page * POSTS_PER_PAGE
    end = start + POSTS_PER_PAGE
    page_posts = posts[start:end]

    if not page_posts:
        desc = "No market posts found." if total == 0 else "No posts on this page."
    else:
        lines = [_build_post_line(p) for p in page_posts]
        desc = "\n\n".join(lines)

    title = "Market Listings"
    if resource:
        title += f" — {resource}"

    emb = discord.Embed(title=title, description=desc, color=0xFAA61A)
    emb.set_footer(text=f"Page {page+1}/{pages} — {total} posts total")

    # If no posts, don't build a view
    if not page_posts:
        return emb, None

    class _PostSelect(discord.ui.Select):
        def __init__(self, options: List[discord.SelectOption]):
            super().__init__(placeholder="Select a market post...", min_values=1, max_values=1, options=options, custom_id="market_post_select")

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_message(f"Selected {self.values[0]}. Click Accept selected to proceed.", ephemeral=True)

    class MarketView(discord.ui.View):
        def __init__(self, posts_slice: List[Dict[str, Any]], all_posts: List[Dict[str, Any]], page_idx: int, pages_count: int, owner_id: int, resource_filter: Optional[str]):
            super().__init__(timeout=180)
            self.posts_slice = posts_slice
            self.all_posts = all_posts
            self.page_idx = page_idx
            self.pages_count = pages_count
            self.owner_id = owner_id
            self.resource_filter = resource_filter

            options = []
            for p in posts_slice:
                pid = p.get("id") or p.get("post_id") or p.get("rowid") or p.get("market_id") or "?"
                rname = p.get("resource")
                if not rname:
                    try:
                        offered = json.loads(p.get("offered_json") or "{}")
                        rname = next(iter(offered.keys())) if offered else "Resource"
                    except Exception:
                        rname = "Resource"
                qty = int(p.get("quantity") or p.get("amount") or 0)
                price = p.get("price_per_unit") or p.get("price") or 0.0
                label = f"ID {pid} • {rname} ×{qty}"
                description = f"{_format_money(price)}/unit"
                options.append(discord.SelectOption(label=label[:100], description=description[:100], value=str(pid)))
            if options:
                self.add_item(_PostSelect(options))

        async def on_error(self, interaction: discord.Interaction, error: Exception, item):
            log.exception("MarketView.on_error")
            try:
                await interaction.response.send_message("⚠️ Market UI error. Try again.", ephemeral=True)
            except Exception:
                pass

        @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary)
        async def prev_page(self, itx: discord.Interaction, btn: discord.ui.Button):
            if itx.user.id != self.owner_id:
                await itx.response.send_message("This paginator isn't for you.", ephemeral=True)
                return
            new_page = max(0, self.page_idx - 1)
            try:
                new_emb, new_view = await create_market_embed_and_view(self.owner_id, resource=self.resource_filter, page=new_page)
                await itx.response.edit_message(embed=new_emb, view=new_view)
            except Exception:
                await itx.followup.send("Failed to paginate. Try again.", ephemeral=True)

        @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary)
        async def next_page(self, itx: discord.Interaction, btn: discord.ui.Button):
            if itx.user.id != self.owner_id:
                await itx.response.send_message("This paginator isn't for you.", ephemeral=True)
                return
            new_page = min(self.pages_count - 1, self.page_idx + 1)
            try:
                new_emb, new_view = await create_market_embed_and_view(self.owner_id, resource=self.resource_filter, page=new_page)
                await itx.response.edit_message(embed=new_emb, view=new_view)
            except Exception:
                await itx.followup.send("Failed to paginate. Try again.", ephemeral=True)

        @discord.ui.button(label="Accept selected", style=discord.ButtonStyle.success)
        async def accept_selected(self, itx: discord.Interaction, btn: discord.ui.Button):
            if itx.user.id != self.owner_id:
                await itx.response.send_message("Only the requester can accept from this UI.", ephemeral=True)
                return
            selected_pid = None
            for child in self.children:
                if isinstance(child, _PostSelect):
                    selected_pid = child.values[0] if child.values else None
                    break
            if not selected_pid:
                await itx.response.send_message("No post selected — choose a post from the dropdown first.", ephemeral=True)
                return
            # call accept_market_post in this module if available
            try:
                accept_fn = globals().get("accept_market_post")
                if callable(accept_fn):
                    res = await accept_fn(buyer_nation=str(itx.user.id), post_id=int(selected_pid))  # note: bot code should map user->nation before calling if desired
                else:
                    res = {"ok": False, "error": "accept_market_post not implemented in service."}
            except Exception as e:
                log.exception("accept_selected failed")
                res = {"ok": False, "error": str(e)}

            if res.get("ok"):
                cost = res.get("transport_cost", 0.0)
                text = f"✅ Market purchase staged (offer id {res.get('offer_id')}). Transport cost: ${float(cost):.2f}"
                await itx.response.send_message(text, ephemeral=True)
                try:
                    new_emb, new_view = await create_market_embed_and_view(self.owner_id, resource=self.resource_filter, page=self.page_idx)
                    await itx.message.edit(embed=new_emb, view=new_view)
                except Exception:
                    pass
            else:
                await itx.response.send_message(f"❌ {res.get('error')}", ephemeral=True)

    view = MarketView(page_posts, posts, page, pages, owner_id=user_id, resource_filter=resource)
    return emb, view


# ------------------------
# Market backend helpers
# ------------------------
async def list_market_posts(limit: int = 100) -> List[Dict[str, Any]]:
    conn = await get_conn()
    try:
        cur = await conn.execute("SELECT * FROM market_posts ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def post_market(poster_nation: str, resource: str, quantity: float, price_per_unit: float, is_sell: bool = True, transport_mode: str = "auto"):
    conn = await get_conn()
    try:
        # confirm resource exists (best-effort)
        cur = await conn.execute("SELECT resource FROM resources WHERE resource = ? LIMIT 1", (resource,))
        if not await cur.fetchone():
            return {"error": "Unknown resource"}

        # if sell, ensure they have available stock
        if is_sell:
            cur = await conn.execute("""
                SELECT COALESCE(SUM(ps.amount),0) as total
                FROM province_stockpiles ps
                JOIN provinces p ON ps.province_id = p.province_id
                WHERE p.controller_id=? AND ps.resource=?
            """, (poster_nation, resource))
            r = await cur.fetchone()
            total = float(r["total"] or 0) if r else 0.0

            cur = await conn.execute("""
                SELECT COALESCE(SUM(r.amount),0) as reserved
                FROM province_reservations r
                JOIN provinces p ON r.province_id = p.province_id
                WHERE p.controller_id=? AND r.resource=?
            """, (poster_nation, resource))
            rr = await cur.fetchone()
            reserved = float(rr["reserved"] or 0) if rr else 0.0

            available = total - reserved
            if available + 1e-9 < float(quantity):
                return {"error": f"Not enough available {resource} to post sell order (have {available})"}

        created_at = datetime.datetime.datetime.utcnow().isoformat() if hasattr(datetime, "datetime") else datetime.datetime.utcnow().isoformat()
        # try canonical columns
        try:
            await conn.execute("""
                INSERT INTO market_posts (poster_nation, resource, quantity, price_per_unit, is_sell, transport_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (poster_nation, resource, quantity, price_per_unit, 1 if is_sell else 0, transport_mode, created_at))
        except sqlite3.OperationalError:
            # fallback: insert fewer columns if schema differs
            await conn.execute("INSERT INTO market_posts (poster_nation, resource, quantity, price_per_unit) VALUES (?, ?, ?, ?)",
                               (poster_nation, resource, quantity, price_per_unit))
        await conn.commit()
        return {"ok": True}
    except Exception as e:
        log.exception("post_market failed")
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def cancel_market_post(poster_nation: str, post_id: int) -> Dict[str, Any]:
    """
    Cancel a market post if it belongs to poster_nation and is still open.
    """
    conn = await get_conn()
    try:
        cur = await conn.execute("SELECT * FROM market_posts WHERE id=?", (post_id,))
        row = await cur.fetchone()
        if not row:
            return {"error": "Market post not found"}
        r = _row_to_dict(row)
        if r.get("poster_nation") != poster_nation and str(r.get("poster_nation")) != str(poster_nation):
            return {"error": "You are not the poster of this market post"}
        try:
            await conn.execute("DELETE FROM market_posts WHERE id=?", (post_id,))
            await conn.commit()
            return {"ok": True}
        except Exception as e:
            try:
                await conn.rollback()
            except Exception:
                pass
            return {"error": f"Failed to cancel market post: {e}"}
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def accept_market_post(buyer_nation: str, post_id: int) -> Dict[str, Any]:
    """
    Buyer accepts a market post -> creates a trade_offer that the seller must confirm.
    Deducts buyer cash for the price and returns an offer_id (seller must accept to finalize).
    """
    conn = await get_conn()
    cur = await conn.cursor()
    try:
        await cur.execute("SELECT * FROM market_posts WHERE id=?", (post_id,))
        post_row = await cur.fetchone()
        if not post_row:
            return {"error": "Market post not found"}
        post = _row_to_dict(post_row)
        poster = post.get("poster_nation")
        resource = post.get("resource")
        qty = float(post.get("quantity") or 0)
        price = float(post.get("price_per_unit") or post.get("price") or 0)
        total_price = price * qty

        # Check buyer cash
        await cur.execute("SELECT COALESCE(cash,0) as cash FROM playernations WHERE nation_id=?", (buyer_nation,))
        br = await cur.fetchone()
        buyer_cash = float(br["cash"] or 0) if br else 0
        if buyer_cash + 1e-9 < total_price:
            return {"error": "Buyer lacks cash to escrow for this purchase"}

        # Deduct buyer cash (escrow)
        await cur.execute("UPDATE playernations SET cash = cash - ? WHERE nation_id=?", (total_price, buyer_nation))

        offered_json = json.dumps({})
        requested_json = json.dumps({resource: qty})
        transport_mode = post.get("transport_mode") or post.get("mode") or "auto"
        created_at = datetime.datetime.utcnow().isoformat()

        # Insert into trade_offers as open offer for seller to accept
        try:
            await cur.execute("""
                INSERT INTO trade_offers (from_nation, to_nation, offered_json, requested_json, offered_cash, requested_cash, status, transport_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """, (buyer_nation, poster, offered_json, requested_json, total_price, 0.0, transport_mode, created_at))
            offer_id = cur.lastrowid
        except sqlite3.OperationalError:
            # Some schemas use different column names; try minimal
            await cur.execute("INSERT INTO trade_offers (from_nation, to_nation, offered_json, requested_json) VALUES (?, ?, ?, ?)",
                              (buyer_nation, poster, offered_json, requested_json))
            offer_id = cur.lastrowid

        # Fetch seller discord if present
        await cur.execute("SELECT owner_discord_id FROM playernations WHERE nation_id=?", (poster,))
        prow = await cur.fetchone()
        seller_discord = None
        if prow:
            prow_d = _row_to_dict(prow)
            seller_discord = prow_d.get("owner_discord_id") or prow_d.get("owner") or None

        await conn.commit()
        return {"ok": True, "offer_id": offer_id, "seller_discord": seller_discord, "poster_nation": poster, "resource": resource, "quantity": qty, "price_total": total_price}
    except Exception as e:
        log.exception("accept_market_post failed")
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        try:
            await conn.close()
        except Exception:
            pass


# ------------------------
# Offers list / create / cancel / accept
# ------------------------
async def list_offers_for_nation(nation_id: str) -> List[Dict[str, Any]]:
    conn = await get_conn()
    try:
        cur = await conn.execute("SELECT * FROM trade_offers WHERE from_nation=? ORDER BY created_at DESC LIMIT 200", (nation_id,))
        rows = await cur.fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d["offered_json"] = d.get("offered_json") or "{}"
            d["requested_json"] = d.get("requested_json") or "{}"
            out.append(d)
        return out
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def create_offer(from_nation: str, to_nation: str, offered_json: Dict[str, float], requested_json: Dict[str, float], offered_cash: float = 0.0, requested_cash: float = 0.0, transport_mode: str = "auto") -> Dict[str, Any]:
    conn = await get_conn()
    cur = await conn.cursor()
    try:
        # Per-nation outstanding limit
        await cur.execute("SELECT COUNT(*) as cnt FROM trade_offers WHERE from_nation=? AND status='open'", (from_nation,))
        cnt_row = await cur.fetchone()
        cnt = int(cnt_row["cnt"] or 0) if cnt_row else 0
        if cnt >= 3:
            return {"error": "You already have 3 outstanding offers. Cancel or wait for them to be resolved."}

        if offered_cash and offered_cash > 0:
            await cur.execute("SELECT COALESCE(cash,0) as cash FROM playernations WHERE nation_id=?", (from_nation,))
            r = await cur.fetchone()
            cash = float(r["cash"] or 0) if r else 0
            if cash + 1e-9 < float(offered_cash):
                return {"error": "Insufficient cash to escrow for this offer."}
            await cur.execute("UPDATE playernations SET cash = cash - ? WHERE nation_id=?", (offered_cash, from_nation))

        created_at = datetime.datetime.utcnow().isoformat()
        await cur.execute("""
            INSERT INTO trade_offers (from_nation, to_nation, offered_json, requested_json, offered_cash, requested_cash, status, transport_mode, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """, (from_nation, to_nation, json.dumps(offered_json), json.dumps(requested_json), offered_cash, requested_cash, transport_mode, created_at))
        offer_id = cur.lastrowid

        await cur.execute("SELECT owner_discord_id FROM playernations WHERE nation_id=?", (to_nation,))
        prow = await cur.fetchone()
        to_discord = None
        if prow:
            to_discord = _row_to_dict(prow).get("owner_discord_id")

        await conn.commit()
        return {"ok": True, "offer_id": offer_id, "to_discord": to_discord}
    except Exception as e:
        log.exception("create_offer failed")
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def accept_offer(offer_id: int, accepter_nation: str) -> Dict[str, Any]:
    """
    Finalize an offer that was created by create_offer (used for direct offers).
    This function is heavier: moves resources between nations and resolves money/transport.
    """
    conn = await get_conn()
    cur = await conn.cursor()
    row = None
    try:
        await cur.execute("SELECT * FROM trade_offers WHERE id=?", (offer_id,))
        row = await cur.fetchone()
        if not row:
            return {"error": "Offer not found"}
        off = _row_to_dict(row)
        if str(off.get("to_nation")) != str(accepter_nation):
            return {"error": "This offer is not addressed to your nation"}
        if (off.get("status") or "").lower() != "open":
            return {"error": f"Offer status is {off.get('status')}"}

        offered = json.loads(off.get("offered_json") or "{}")
        requested = json.loads(off.get("requested_json") or "{}")
        offered_cash = float(off.get("offered_cash") or 0)
        requested_cash = float(off.get("requested_cash") or 0)
        transport_mode = off.get("transport_mode") or "auto"

        # estimate weight & distance (best-effort)
        weight = 0.0
        all_resources = {}
        for r, q in offered.items():
            all_resources[r] = all_resources.get(r, 0) + float(q)
        for r, q in requested.items():
            all_resources[r] = all_resources.get(r, 0) + float(q)

        for rname, qty in all_resources.items():
            w = 1.0
            try:
                await cur.execute("SELECT weight_kg FROM resources WHERE resource=?", (rname,))
                rr = await cur.fetchone()
                if rr and ("weight_kg" in rr.keys() or "weight_kg" in dict(rr).keys()) and rr.get("weight_kg") is not None:
                    w = float(rr["weight_kg"])
            except sqlite3.OperationalError:
                w = 1.0
            except Exception:
                w = 1.0
            weight += w * float(qty)

        distance = 0.0
        try:
            await cur.execute("""SELECT p.x, p.y FROM provinces p WHERE p.controller_id=? ORDER BY p.node_strength DESC LIMIT 1""", (off.get("from_nation"),))
            a = await cur.fetchone()
            await cur.execute("""SELECT p.x, p.y FROM provinces p WHERE p.controller_id=? ORDER BY p.node_strength DESC LIMIT 1""", (accepter_nation,))
            b = await cur.fetchone()
            if a and b and a.get("x") is not None and a.get("y") is not None and b.get("x") is not None and b.get("y") is not None:
                dx = float(a["x"]) - float(b["x"]); dy = float(a["y"]) - float(b["y"])
                distance = math.hypot(dx, dy)
        except Exception:
            distance = 0.0

        transport_cost = compute_transport_cost(weight, distance, transport_mode)

        # Deduct requested from accepter
        for res, qty in requested.items():
            ok, deducted, short = await _deduct_from_nation(accepter_nation, res, float(qty), conn)
            if not ok:
                raise RuntimeError(f"Accepter nation lacks {res} (short {short})")

        # Deduct offered from offerer
        for res, qty in offered.items():
            ok, deducted, short = await _deduct_from_nation(off.get("from_nation"), res, float(qty), conn)
            if not ok:
                raise RuntimeError(f"Offerer nation lacks {res} (short {short})")

        # Transfer resources
        for res, qty in offered.items():
            await _add_to_nation(accepter_nation, res, float(qty), conn)
        for res, qty in requested.items():
            await _add_to_nation(off.get("from_nation"), res, float(qty), conn)

        net_to_accepter = offered_cash - transport_cost
        if net_to_accepter < 0:
            net_to_accepter = 0.0
        if net_to_accepter > 0:
            await cur.execute("UPDATE playernations SET cash = cash + ? WHERE nation_id=?", (net_to_accepter, accepter_nation))

        # record trade
        await cur.execute("SELECT value FROM config WHERE key='current_turn'")
        ct = await cur.fetchone()
        current_turn = int(ct["value"]) if ct and ct.get("value") is not None else 0
        await cur.execute("""
            INSERT INTO trades (from_nation, to_nation, resources_exchanged, cash_exchanged, transport_cost, turn, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (off.get("from_nation"), accepter_nation, json.dumps({"from_nation": off.get("offered_json"), "to_nation": off.get("requested_json")}),
              (offered_cash - requested_cash), transport_cost, current_turn, datetime.datetime.utcnow().isoformat()))

        await cur.execute("UPDATE trade_offers SET status='completed' WHERE id=?", (offer_id,))
        await conn.commit()
        return {"ok": True, "transport_cost": transport_cost}
    except Exception as e:
        log.exception("accept_offer failed")
        # On error, refund escrowed offered_cash if present
        try:
            if row:
                od = _row_to_dict(row)
                offered_cash = float(od.get("offered_cash") or 0)
                if offered_cash and offered_cash > 0:
                    await cur.execute("UPDATE playernations SET cash = cash + ? WHERE nation_id=?", (offered_cash, od.get("from_nation")))
                    await cur.execute("UPDATE trade_offers SET status='failed' WHERE id=?", (offer_id,))
                    await conn.commit()
        except Exception:
            pass
        try:
            await conn.rollback()
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass
        return {"error": f"Trade execution failed: {e}"}


async def cancel_offer(offer_id: int, nation_id: str) -> Dict[str, Any]:
    conn = await get_conn()
    try:
        cur = await conn.execute("SELECT * FROM trade_offers WHERE id=?", (offer_id,))
        row = await cur.fetchone()
        if not row:
            return {"error": "Offer not found"}
        off = _row_to_dict(row)
        if str(off.get("from_nation")) != str(nation_id):
            return {"error": "You are not the creator of this offer"}
        if (off.get("status") or "").lower() != "open":
            return {"error": f"Offer status is {off.get('status')}; only open offers can be cancelled"}
        offered_cash = float(off.get("offered_cash") or 0)
        refunded = 0.0
        if offered_cash > 0:
            await conn.execute("UPDATE playernations SET cash = cash + ? WHERE nation_id=?", (offered_cash, off.get("from_nation")))
            refunded = offered_cash
        await conn.execute("UPDATE trade_offers SET status='cancelled' WHERE id=?", (offer_id,))
        await conn.commit()
        return {"ok": True, "refunded": refunded}
    except Exception as e:
        log.exception("cancel_offer failed")
        try:
            await conn.rollback()
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        try:
            await conn.close()
        except Exception:
            pass


# ------------------------
# Helpers: deduct / add
# ------------------------
async def _deduct_from_nation(nation_id: str, resource: str, qty: float, conn) -> Tuple[bool, float, float]:
    """
    Attempts to remove qty of resource from nation's provinces (highest node_strength first).
    Returns (ok, deducted_amount, remaining_short).
    """
    cur = await conn.cursor()
    remaining = float(qty)
    await cur.execute("""
        SELECT ps.rowid as ps_rowid, ps.province_id, ps.amount
        FROM province_stockpiles ps
        JOIN provinces p ON ps.province_id = p.province_id
        WHERE p.controller_id = ? AND ps.resource = ? AND ps.amount > 0
        ORDER BY COALESCE(p.node_strength,0) DESC
    """, (nation_id, resource))
    rows = await cur.fetchall()
    if not rows:
        return False, 0.0, remaining
    deducted = 0.0
    for r in rows:
        rdict = _row_to_dict(r)
        have = float(rdict.get("amount") or 0)
        if have <= 0:
            continue
        take = min(have, remaining)
        new_amount = have - take
        await cur.execute("UPDATE province_stockpiles SET amount = ? WHERE rowid = ?", (new_amount, rdict["ps_rowid"]))
        remaining -= take
        deducted += take
        if remaining <= 1e-9:
            break
    await conn.commit()
    if remaining > 1e-9:
        return False, deducted, remaining
    return True, deducted, 0.0


async def _add_to_nation(nation_id: str, resource: str, qty: float, conn) -> None:
    """
    Adds qty to the most suitable province stockpile for nation (highest node_strength).
    Creates a new stockpile row if none exists.
    """
    cur = await conn.cursor()
    await cur.execute("""
        SELECT ps.rowid as ps_rowid, ps.province_id, ps.amount, ps.capacity
        FROM province_stockpiles ps
        JOIN provinces p ON ps.province_id = p.province_id
        WHERE p.controller_id = ? AND ps.resource = ?
        ORDER BY COALESCE(p.node_strength,0) DESC
        LIMIT 1
    """, (nation_id, resource))
    r = await cur.fetchone()
    if r:
        rdict = _row_to_dict(r)
        new_amount = float(rdict.get("amount") or 0) + float(qty)
        capacity = float(rdict.get("capacity") or 0)
        if capacity and new_amount > capacity:
            new_amount = capacity
        await cur.execute("UPDATE province_stockpiles SET amount = ? WHERE rowid = ?", (new_amount, rdict["ps_rowid"]))
        await conn.commit()
        return
    # find top province to insert into
    await cur.execute("SELECT province_id FROM provinces WHERE controller_id=? ORDER BY COALESCE(node_strength,0) DESC LIMIT 1", (nation_id,))
    prow = await cur.fetchone()
    if prow:
        province_id = _row_to_dict(prow).get("province_id")
        default_capacity = 100000.0
        insert_amount = min(qty, default_capacity)
        await cur.execute("INSERT INTO province_stockpiles (province_id, resource, amount, capacity) VALUES (?, ?, ?, ?)",
                          (province_id, resource, insert_amount, default_capacity))
        await conn.commit()
        return
    return


# -------------------------
# Estimate transport cost (READ-ONLY)
# -------------------------
async def estimate_transport_cost(from_nation: str, to_nation: str, offered: Dict[str, float], requested: Dict[str, float]) -> Dict[str, Any]:
    conn = await get_conn()
    try:
        weight = 0.0
        all_resources = {}
        for r, q in offered.items():
            all_resources[r] = all_resources.get(r, 0) + float(q)
        for r, q in requested.items():
            all_resources[r] = all_resources.get(r, 0) + float(q)

        cur = await conn.cursor()
        for rname, qty in all_resources.items():
            w = 1.0
            try:
                await cur.execute("SELECT weight_kg FROM resources WHERE resource=?", (rname,))
                rr = await cur.fetchone()
                if rr and ("weight_kg" in rr.keys() or "weight_kg" in dict(rr).keys()) and rr.get("weight_kg") is not None:
                    w = float(rr["weight_kg"])
            except sqlite3.OperationalError:
                w = 1.0
            except Exception:
                w = 1.0
            weight += w * float(qty)

        distance = 0.0
        try:
            await cur.execute("SELECT x,y FROM provinces WHERE controller_id=? ORDER BY COALESCE(node_strength,0) DESC LIMIT 1", (from_nation,))
            a = await cur.fetchone()
            await cur.execute("SELECT x,y FROM provinces WHERE controller_id=? ORDER BY COALESCE(node_strength,0) DESC LIMIT 1", (to_nation,))
            b = await cur.fetchone()
            if a and b and a.get("x") is not None and a.get("y") is not None and b.get("x") is not None and b.get("y") is not None:
                dx = float(a["x"]) - float(b["x"]); dy = float(a["y"]) - float(b["y"])
                distance = math.hypot(dx, dy)
        except Exception:
            distance = 0.0

        mode = "auto"
        cost = compute_transport_cost(weight, distance, mode)
        return {"weight_kg": weight, "distance": distance, "transport_cost": cost, "mode": mode}
    finally:
        try:
            await conn.close()
        except Exception:
            pass
