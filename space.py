# services/space.py
# Command-driven space mechanic wired to game.db.
# - Uses db.get_conn() like your existing services (army.py).
# - Gating: tech -> nation_techs/player_technologies; buildings -> province_buildings OR completed state_builds.
# - New tables: space_mission_defs, space_missions (CREATE IF NOT EXISTS; additive only).

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from db import get_conn  # same pattern as services/army.py

UTC = timezone.utc

# -----------------------
# Mission catalog (seeded once into space_mission_defs)
# -----------------------
DEFAULT_MISSIONS = [
    {
        "code": "TEST_FLIGHT",
        "name": "Test Flight",
        "requires_tech": ["ORBITAL_ROCKETS"],
        "requires_building": ["LAUNCHPAD"],
        "duration_hours": 2,
        "reward": {"prestige": 2}
    },
    {
        "code": "LEO_SAT",
        "name": "LEO Satellite",
        "requires_tech": ["SATELLITES"],
        "requires_building": ["LAUNCHPAD", "MISSION_CONTROL"],
        "duration_hours": 6,
        "reward": {"science": 25, "prestige": 5}
    },
]

# -----------------------
# Internal helpers
# -----------------------
async def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()

async def _init_schema() -> None:
    """
    Additive-only schema: two space_* tables. No changes to existing tables.
    """
    conn = await get_conn(); cur = await conn.cursor()
    await cur.executescript("""
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS space_mission_defs(
      code TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      requires_tech_json TEXT NOT NULL,
      requires_building_json TEXT NOT NULL,
      duration_hours INTEGER NOT NULL,
      reward_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS space_missions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      nation_id TEXT NOT NULL,
      discord_id TEXT NOT NULL,
      mission_code TEXT NOT NULL,
      status TEXT NOT NULL,           -- RUNNING | COMPLETE | FAILED
      eta TEXT NOT NULL,
      launched_at TEXT NOT NULL,
      completed_at TEXT,
      note TEXT,
      payload_json TEXT
    );
    """)
    # Seed mission defs
    for m in DEFAULT_MISSIONS:
        await cur.execute(
            "INSERT OR IGNORE INTO space_mission_defs(code,name,requires_tech_json,requires_building_json,duration_hours,reward_json) VALUES(?,?,?,?,?,?)",
            (m["code"], m["name"], json.dumps(m["requires_tech"]), json.dumps(m["requires_building"]),
             int(m["duration_hours"]), json.dumps(m["reward"]))
        )
    await conn.close()

async def _nation_id_for_discord(discord_id: str) -> Optional[str]:
    """
    Map discord -> nation_id using playernations.owner_discord_id first,
    then nation_players if present.
    """
    conn = await get_conn(); cur = await conn.cursor()
    # primary: playernations
    try:
        await cur.execute(
            "SELECT nation_id FROM playernations WHERE owner_discord_id=? LIMIT 1",
            (discord_id,),
        )
        r = await cur.fetchone()
        if r and r[0]:
            await conn.close()
            return str(r[0])
    except Exception:
        pass

    # fallback: nation_players (if you use a multi-member nation model)
    try:
        await cur.execute(
            "SELECT nation_id FROM nation_players WHERE discord_id=? LIMIT 1",
            (discord_id,),
        )
        r = await cur.fetchone()
        if r and r[0]:
            await conn.close()
            return str(r[0])
    except Exception:
        pass

    await conn.close()
    return None

async def _has_tech(nation_id: str, tech_code: str) -> bool:
    """
    Tech unlocked if present in nation_techs.tech_key OR player_technologies.tech_id for the nation.
    """
    conn = await get_conn(); cur = await conn.cursor()
    try:
        await cur.execute(
            "SELECT 1 FROM nation_techs WHERE nation_id=? AND tech_key=? LIMIT 1",
            (nation_id, tech_code),
        )
        if await cur.fetchone():
            await conn.close()
            return True
    except Exception:
        pass

    try:
        await cur.execute(
            "SELECT 1 FROM player_technologies WHERE nation_id=? AND tech_id=? LIMIT 1",
            (nation_id, tech_code),
        )
        if await cur.fetchone():
            await conn.close()
            return True
    except Exception:
        pass

    await conn.close()
    return False

async def _has_building(nation_id: str, building_code: str) -> bool:
    """
    Building available if:
      - Any installed instance exists in province_buildings.building_id across any province (count>0), OR
      - A state_builds row for the nation with building_id and status='complete'.
    This matches how your DB stores placed vs pipeline builds.
    """
    conn = await get_conn(); cur = await conn.cursor()
    # Installed in any province?
    try:
        await cur.execute(
            "SELECT 1 FROM province_buildings WHERE building_id=? LIMIT 1",
            (building_code,),
        )
        if await cur.fetchone():
            await conn.close()
            return True
    except Exception:
        pass

    # Completed state build?
    try:
        await cur.execute(
            "SELECT 1 FROM state_builds WHERE nation_id=? AND building_id=? AND status='complete' LIMIT 1",
            (nation_id, building_code),
        )
        if await cur.fetchone():
            await conn.close()
            return True
    except Exception:
        pass

    await conn.close()
    return False

async def _mission_def(code: str) -> Optional[Dict[str, Any]]:
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute(
        "SELECT code,name,requires_tech_json,requires_building_json,duration_hours,reward_json FROM space_mission_defs WHERE code=? LIMIT 1",
        (code,),
    )
    r = await cur.fetchone(); await conn.close()
    if not r:
        return None
    return {
        "code": r[0],
        "name": r[1],
        "req_tech": json.loads(r[2] or "[]"),
        "req_build": json.loads(r[3] or "[]"),
        "duration_hours": int(r[4]),
        "reward": json.loads(r[5] or "{}"),
    }

async def _list_mission_defs() -> List[Tuple[str, str]]:
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("SELECT code, name FROM space_mission_defs ORDER BY code")
    rows = await cur.fetchall(); await conn.close()
    return [(r[0], r[1]) for r in rows]

async def _resolve_due_for(nation_id: str, discord_id: str) -> int:
    conn = await get_conn(); cur = await conn.cursor()
    now = await _now_iso()
    await cur.execute(
        "SELECT id, mission_code FROM space_missions WHERE nation_id=? AND discord_id=? AND status='RUNNING' AND eta <= ?",
        (nation_id, discord_id, now),
    )
    due = await cur.fetchall()
    count = 0
    for mid, mcode in due:
        await cur.execute("SELECT reward_json FROM space_mission_defs WHERE code=? LIMIT 1", (mcode,))
        rw = await cur.fetchone()
        note = f"Rewards: {rw[0] if rw else '{}'}"
        await cur.execute(
            "UPDATE space_missions SET status='COMPLETE', completed_at=?, note=? WHERE id=?",
            (now, note, mid),
        )
        count += 1
    await conn.close()
    return count

# -----------------------
# Discord Cog
# -----------------------
class Space(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await _init_schema()

    space = app_commands.Group(name="space", description="Space program (tech + building gated)")

    @space.command(name="status", description="Show readiness & active missions")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        did = str(interaction.user.id)
        nation_id = await _nation_id_for_discord(did)
        if not nation_id:
            await interaction.followup.send("No nation linked to your Discord account.", ephemeral=True)
            return

        defs = await _list_mission_defs()
        readiness_lines: List[str] = []
        for code, name in defs:
            d = await _mission_def(code)
            missing: List[str] = []
            for t in d["req_tech"]:
                if not await _has_tech(nation_id, t):
                    missing.append(f"tech:{t}")
            for b in d["req_build"]:
                if not await _has_building(nation_id, b):
                    missing.append(f"building:{b}")
            readiness_lines.append(f"**{name}** ({code}) — " + ("✅ ready" if not missing else f"missing {', '.join(missing)}"))

        conn = await get_conn(); cur = await conn.cursor()
        await cur.execute(
            "SELECT id, mission_code, status, eta FROM space_missions WHERE nation_id=? AND discord_id=? AND status='RUNNING' ORDER BY eta ASC",
            (nation_id, did),
        )
        running = await cur.fetchall(); await conn.close()

        emb = discord.Embed(title="🚀 Space Program", timestamp=datetime.now(tz=UTC))
        emb.add_field(name="Nation", value=nation_id, inline=False)
        emb.add_field(name="Missions readiness", value="\n".join(readiness_lines) if readiness_lines else "(no missions)", inline=False)
        if running:
            rlines = [f"#{r[0]} — {r[1]} — ETA {r[3]}" for r in running]
            emb.add_field(name="Active missions", value="\n".join(rlines), inline=False)
        else:
            emb.add_field(name="Active missions", value="None", inline=False)

        await interaction.followup.send(embed=emb, ephemeral=True)

    @space.command(name="launch", description="Launch a mission")
    @app_commands.describe(mission_code="e.g. TEST_FLIGHT, LEO_SAT", payload_json="Optional JSON payload")
    async def launch(self, interaction: discord.Interaction, mission_code: str, payload_json: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        did = str(interaction.user.id)
        nation_id = await _nation_id_for_discord(did)
        if not nation_id:
            await interaction.followup.send("No nation linked to your Discord account.", ephemeral=True)
            return

        d = await _mission_def(mission_code)
        if not d:
            await interaction.followup.send("Unknown mission code.", ephemeral=True)
            return

        missing: List[str] = []
        for t in d["req_tech"]:
            if not await _has_tech(nation_id, t):
                missing.append(f"tech:{t}")
        for b in d["req_build"]:
            if not await _has_building(nation_id, b):
                missing.append(f"building:{b}")
        if missing:
            await interaction.followup.send("Requirements not met — " + ", ".join(missing), ephemeral=True)
            return

        # schedule
        try:
            payload = json.dumps(json.loads(payload_json)) if payload_json else None
        except Exception:
            await interaction.followup.send("Payload must be valid JSON.", ephemeral=True)
            return

        conn = await get_conn(); cur = await conn.cursor()
        now = await _now_iso()
        eta = (datetime.now(tz=UTC) + timedelta(hours=int(d["duration_hours"]))).isoformat()
        await cur.execute(
            "INSERT INTO space_missions(nation_id, discord_id, mission_code, status, eta, launched_at, payload_json, note) VALUES(?,?,?,?,?,?,?,?)",
            (nation_id, did, mission_code, "RUNNING", eta, now, payload, None),
        )
        # aiosqlite lastrowid
        mid = cur.lastrowid
        await conn.close()
        await interaction.followup.send(f"Mission **{mission_code}** launched. ID **#{mid}** ⏳ ETA: {eta}", ephemeral=True)

    @space.command(name="missions", description="List your missions")
    async def missions(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        did = str(interaction.user.id)
        nation_id = await _nation_id_for_discord(did)
        if not nation_id:
            await interaction.followup.send("No nation linked to your Discord account.", ephemeral=True)
            return

        conn = await get_conn(); cur = await conn.cursor()
        await cur.execute(
            "SELECT id, mission_code, status, launched_at, eta, completed_at, note FROM space_missions WHERE nation_id=? AND discord_id=? ORDER BY id DESC LIMIT 25",
            (nation_id, did),
        )
        rows = await cur.fetchall(); await conn.close()
        if not rows:
            await interaction.followup.send("No missions yet.", ephemeral=True)
            return

        emb = discord.Embed(title="🛰️ Missions", timestamp=datetime.now(tz=UTC))
        for r in rows:
            mid, mcode, st, launched, eta, done, note = r
            val = f"{st} • launched {launched}"
            if st == "RUNNING":
                val += f" • ETA {eta}"
            if done:
                val += f" • completed {done}"
            if note:
                val += f"\n{note}"
            emb.add_field(name=f"#{mid} — {mcode}", value=val, inline=False)
        await interaction.followup.send(embed=emb, ephemeral=True)

    @space.command(name="resolve", description="Resolve any completed missions")
    async def resolve(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        did = str(interaction.user.id)
        nation_id = await _nation_id_for_discord(did)
        if not nation_id:
            await interaction.followup.send("No nation linked to your Discord account.", ephemeral=True)
            return
        count = await _resolve_due_for(nation_id, did)
        await interaction.followup.send(f"Resolved **{count}** mission(s).", ephemeral=True)

    @space.command(name="admin_seed", description="(Admin) Ensure mission defs exist")
    async def admin_seed(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True); return
        if not (interaction.user == interaction.guild.owner or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Admin only.", ephemeral=True); return
        await _init_schema()
        await interaction.response.send_message("Space mission defs ensured.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Space(bot))
