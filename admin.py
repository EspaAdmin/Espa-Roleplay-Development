# services/admin.py
from db import get_conn

async def add_admin(issuer: str, target: str):
    conn = await get_conn(); cur = await conn.cursor()
    now = __import__("datetime").datetime.utcnow().isoformat()
    await cur.execute("INSERT OR REPLACE INTO admins (discord_id, added_by, added_at) VALUES (?, ?, ?)", (str(target), str(issuer), now))
    await conn.commit(); await conn.close()
    return True

async def remove_admin(target: str):
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("DELETE FROM admins WHERE discord_id=?", (str(target),))
    await conn.commit(); await conn.close()
    return True

async def link_nation(nation_id: str, discord_id: str):
    conn = await get_conn(); cur = await conn.cursor()
    await cur.execute("UPDATE playernations SET owner_discord_id=? WHERE nation_id=?", (str(discord_id), nation_id))
    await conn.commit(); await conn.close()
    return True
