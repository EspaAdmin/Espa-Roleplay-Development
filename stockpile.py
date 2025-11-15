"""
services/stockpile.py

Provides atomic helpers for province stockpiles and reservations.
All functions use the get_conn() helper from db.py which returns an aiosqlite connection.
"""
import aiosqlite
from db import get_conn
from typing import List, Dict

async def get_province_stockpile(province_id: str) -> List[Dict]:
    """
    Return list of {resource, amount, capacity} rows for a province.
    """
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT resource, amount, capacity FROM province_stockpiles WHERE province_id=?", (province_id,)
    )
    rows = await cur.fetchall()
    await conn.close()
    return [dict(r) for r in rows]

async def ensure_stockpile_row(province_id: str, resource: str):
    """
    Create a row with zero amounts if not exists.
    """
    conn = await get_conn()
    cur = await conn.cursor()
    await cur.execute(
        "INSERT OR IGNORE INTO province_stockpiles (province_id, resource, amount, capacity) VALUES (?, ?, 0, 0)",
        (province_id, resource)
    )
    await conn.commit()
    await conn.close()

async def add_to_stockpile(province_id: str, resource: str, amount: float) -> bool:
    """
    Add amount to province stockpile. Ensures the row exists.
    Returns True on success.
    """
    await ensure_stockpile_row(province_id, resource)
    conn = await get_conn()
    cur = await conn.cursor()
    await cur.execute("UPDATE province_stockpiles SET amount = amount + ? WHERE province_id=? AND resource=?", (amount, province_id, resource))
    await conn.commit()
    await conn.close()
    return True

async def remove_from_stockpile(province_id: str, resource: str, amount: float) -> bool:
    """
    Remove up to `amount` from stockpile. If insufficient, returns False and does not modify.
    Use transactions for safety.
    """
    conn = await get_conn()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.cursor()
        await cur.execute("SELECT amount FROM province_stockpiles WHERE province_id=? AND resource=?", (province_id, resource))
        row = await cur.fetchone()
        total = float(row["amount"]) if row else 0.0
        if total + 1e-9 < amount:
            await conn.rollback(); await conn.close()
            return False
        await cur.execute("UPDATE province_stockpiles SET amount = amount - ? WHERE province_id=? AND resource=?", (amount, province_id, resource))
        await conn.commit(); await conn.close()
        return True
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        await conn.close()
        return False

async def get_available_amount(province_id: str, resource: str) -> float:
    """
    Return available amount in a province after subtracting reservations.
    """
    conn = await get_conn()
    cur = await conn.cursor()
    await cur.execute("SELECT amount FROM province_stockpiles WHERE province_id=? AND resource=?", (province_id, resource))
    row = await cur.fetchone()
    total = float(row["amount"]) if row else 0.0
    await cur.execute("SELECT COALESCE(SUM(amount),0) as reserved FROM province_reservations WHERE province_id=? AND resource=?", (province_id, resource))
    rrow = await cur.fetchone()
    reserved = float(rrow["reserved"] or 0)
    await conn.close()
    return max(0.0, total - reserved)

async def reserve_resources(build_id: int, province_id: str, resource: str, amount: float) -> bool:
    """
    Atomically reserve `amount` of `resource` in `province_id` for a build.
    Returns True if reservation succeeded, False otherwise.
    """
    conn = await get_conn()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.cursor()
        await cur.execute("SELECT amount FROM province_stockpiles WHERE province_id=? AND resource=?", (province_id, resource))
        row = await cur.fetchone()
        total = float(row["amount"]) if row else 0.0
        await cur.execute("SELECT COALESCE(SUM(amount),0) as reserved FROM province_reservations WHERE province_id=? AND resource=?", (province_id, resource))
        rrow = await cur.fetchone()
        reserved = float(rrow["reserved"] or 0)
        available = total - reserved
        if available + 1e-9 < amount:
            await conn.rollback(); await conn.close()
            return False
        await cur.execute(
            "INSERT INTO province_reservations (build_id, province_id, resource, amount) VALUES (?, ?, ?, ?)",
            (build_id, province_id, resource, amount)
        )
        await conn.commit(); await conn.close()
        return True
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        await conn.close()
        return False

async def consume_reservation(build_id: int) -> List[Dict]:
    """
    Consume reservations for a build. Deduct reserved amounts from stockpiles
    and delete reservations entries. Returns list of consumed rows.
    """
    conn = await get_conn()
    cur = await conn.cursor()
    await cur.execute("SELECT id, province_id, resource, amount FROM province_reservations WHERE build_id=?", (build_id,))
    rows = await cur.fetchall()
    consumed = []
    for r in rows:
        rr = dict(r)
        consumed.append(rr)
        await cur.execute("UPDATE province_stockpiles SET amount = amount - ? WHERE province_id=? AND resource=?", (rr["amount"], rr["province_id"], rr["resource"]))
    await cur.execute("DELETE FROM province_reservations WHERE build_id=?", (build_id,))
    await conn.commit(); await conn.close()
    return consumed
