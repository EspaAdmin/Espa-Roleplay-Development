# db_migrate_space.py
# Purpose: create the two additive space tables and seed default mission defs.
# - No renames, no drops, no schema changes to existing game.db tables.

from __future__ import annotations
import json
import asyncio
from db import get_conn  # same as other services

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

SQL = """
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
  status TEXT NOT NULL,
  eta TEXT NOT NULL,
  launched_at TEXT NOT NULL,
  completed_at TEXT,
  note TEXT,
  payload_json TEXT
);
"""

async def main():
    conn = await get_conn(); cur = await conn.cursor()
    await cur.executescript(SQL)
    # Seed defs
    for m in DEFAULT_MISSIONS:
        await cur.execute(
            "INSERT OR IGNORE INTO space_mission_defs(code,name,requires_tech_json,requires_building_json,duration_hours,reward_json) VALUES(?,?,?,?,?,?)",
            (m["code"], m["name"], json.dumps(m["requires_tech"]), json.dumps(m["requires_building"]), int(m["duration_hours"]), json.dumps(m["reward"]))
        )
    await conn.close()
    print("✅ space tables ensured & missions seeded")

if __name__ == "__main__":
    asyncio.run(main())
