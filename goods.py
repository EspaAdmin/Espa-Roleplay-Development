# services/goods.py
import json
import discord
from typing import Dict, List, Any
from db import get_conn

# Category resources
CATEGORIES = {
    "Raw": ["Raw Ore", "Coal", "Oil", "Food", "Raw Uranium"],
    "Refined": ["Iron", "Fuel", "Military Goods"],
    "Advanced": ["Steel", "Refined Uranium"]
}
EMOJI = {"Raw": "⛏️", "Refined": "⚙️", "Advanced": "🔬"}


async def _aggregate_stock_and_capacity(nation_id: str) -> Dict[str, Dict[str, float]]:
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("""
        SELECT ps.resource, SUM(ps.amount) as amount, SUM(ps.capacity) as capacity
        FROM province_stockpiles ps
        JOIN provinces p ON ps.province_id = p.province_id
        WHERE p.controller_id = ?
        GROUP BY ps.resource
    """, (nation_id,))
    rows = await cur.fetchall(); await conn.close()
    out = {}
    for r in rows:
        out[r["resource"]] = {"amount": float(r["amount"] or 0), "capacity": float(r["capacity"] or 0)}
    return out


async def _compute_production_and_consumption(nation_id: str):
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("""
        SELECT pb.province_id, pb.building_id, pb.tier, pb.count, bt.inputs, bt.outputs
        FROM province_buildings pb
        JOIN provinces p ON pb.province_id = p.province_id
        JOIN building_templates bt ON bt.id = pb.building_id
        WHERE p.controller_id=?
    """, (nation_id,))
    rows = await cur.fetchall(); await conn.close()
    produced = {}
    consumed = {}
    for r in rows:
        try:
            inputs = json.loads(r["inputs"] or "{}")
        except Exception:
            inputs = {}
        try:
            outputs = json.loads(r["outputs"] or "{}")
        except Exception:
            outputs = {}
        tier = int(r["tier"]) if r["tier"] is not None else 1
        count = int(r["count"]) if r["count"] is not None else 1
        mult = tier * count
        for res, amt in outputs.items():
            produced[res] = produced.get(res, 0.0) + float(amt) * mult
        for res, amt in inputs.items():
            consumed[res] = consumed.get(res, 0.0) + float(amt) * mult
    return produced, consumed


async def get_goods_by_category(nation_id: str) -> Dict[str, List[Dict[str, Any]]]:
    stock = await _aggregate_stock_and_capacity(nation_id)
    produced, consumed = await _compute_production_and_consumption(nation_id)

    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("SELECT resource FROM resources ORDER BY resource")
    res_rows = await cur.fetchall(); await conn.close()
    canonical = [r["resource"] for r in res_rows]

    # prepare data map
    data = {}
    for res in canonical:
        amt = stock.get(res, {}).get("amount", 0.0)
        cap = stock.get(res, {}).get("capacity", 0.0)
        prod = produced.get(res, 0.0)
        cons = consumed.get(res, 0.0)
        net = prod - cons
        data[res] = {"resource": res, "amount": amt, "capacity": cap, "produced": prod, "consumed": cons, "net": net}

    out = {}
    for cat, resources in CATEGORIES.items():
        lst = []
        for r in resources:
            if r in data:
                lst.append(data[r])
            else:
                lst.append({"resource": r, "amount": 0.0, "capacity": 0.0, "produced": 0.0, "consumed": 0.0, "net": 0.0})
        out[cat] = lst
    return out


# ---------- UI: View + embed builder ----------
class GoodsView(discord.ui.View):
    def __init__(self, author_id: int, data_by_cat: Dict[str, List[Dict[str, Any]]]):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.data = data_by_cat
        self.current = "Raw"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command user can use these buttons.", ephemeral=True)
            return False
        return True

    async def _update(self, interaction: discord.Interaction, category: str):
        self.current = category
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = (child.custom_id == f"goods:{category}")
        emb = build_goods_embed_for_category(self.data, category)
        await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Raw", style=discord.ButtonStyle.primary, custom_id="goods:Raw")
    async def raw_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update(interaction, "Raw")

    @discord.ui.button(label="Refined", style=discord.ButtonStyle.secondary, custom_id="goods:Refined")
    async def refined_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update(interaction, "Refined")

    @discord.ui.button(label="Advanced", style=discord.ButtonStyle.secondary, custom_id="goods:Advanced")
    async def adv_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update(interaction, "Advanced")


def build_goods_embed_for_category(data_by_cat: Dict[str, List[Dict[str, Any]]], category: str) -> discord.Embed:
    rows = []
    for entry in data_by_cat.get(category, []):
        rname = entry["resource"]
        amt = int(entry["amount"])
        cap = int(entry["capacity"])
        prod = int(entry["produced"])
        cons = int(entry["consumed"])
        net = int(entry["net"])
        emoji = EMOJI.get(category, "📦")
        rows.append(f"{emoji} **{rname}**\n• Stock: **{amt:,}** / {cap:,}\n• Net: **{net:,}** (P: {prod:,}/ C: {cons:,})")
    emb = discord.Embed(title=f"📦 Stockpiles — {category}", description="\n\n".join(rows) if rows else "_No resources found_", color=0x2ECC71)
    emb.set_footer(text="Stock / Capacity | Net (Produced / Consumed)")
    return emb


async def get_goods_embed_and_view(nation_id: str, author_id: int):
    data = await get_goods_by_category(nation_id)
    emb = build_goods_embed_for_category(data, "Raw")
    view = GoodsView(author_id=author_id, data_by_cat=data)
    # disable Raw button initially
    for child in view.children:
        if getattr(child, "custom_id", "") == "goods:Raw":
            child.disabled = True
    return emb, view
