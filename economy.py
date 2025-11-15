# services/economy.py
import json
from db import get_conn
from typing import List, Dict, Any

async def run_end_turn() -> int:
    """
    Advance the game by one turn, process completed builds, apply production/maintenance.
    This implementation is intentionally conservative: it completes builds and applies production into stockpiles.
    """
    conn = await get_conn(); cur = await conn.cursor()
    # get current turn
    await cur.execute("SELECT value FROM config WHERE key='current_turn'")
    r = await cur.fetchone(); current_turn = int(r["value"] or 0) if r else 0
    next_turn = current_turn + 1

    # process completed builds whose complete_turn <= next_turn and status pending
    await cur.execute("SELECT * FROM state_builds WHERE status='pending' AND complete_turn<=?", (next_turn,))
    pending = await cur.fetchall()
    for b in pending:
        build_id = b["id"]
        state_id = b["state_id"]
        nation_id = b["nation_id"]
        building_id = b["building_id"]
        # parse reserved_json
        try:
            reserved = json.loads(b.get("reserved_json") or "[]")
        except Exception:
            reserved = []
        # consume reservations
        for rsv in reserved:
            pid = rsv["province_id"]; resource = rsv["resource"]; amt = float(rsv["amount"] or 0)
            # subtract amt from province_stockpiles.amount
            await cur.execute("SELECT amount FROM province_stockpiles WHERE province_id=? AND resource=?", (pid, resource))
            prow = await cur.fetchone()
            if prow:
                new_amt = max(0.0, float(prow["amount"] or 0) - amt)
                await cur.execute("UPDATE province_stockpiles SET amount=? WHERE province_id=? AND resource=?", (new_amt, pid, resource))
        # install building: choose province in state with largest node_strength that belongs to nation
        await cur.execute("SELECT province_id FROM provinces WHERE state_id=? AND controller_id=? ORDER BY node_strength DESC LIMIT 1", (state_id, nation_id))
        prov = await cur.fetchone()
        if prov:
            target_pid = prov["province_id"]
            # insert or update province_buildings (add count 1)
            await cur.execute("SELECT rowid, count FROM province_buildings WHERE province_id=? AND building_id=? AND tier=?", (target_pid, building_id, b["tier"]))
            rbb = await cur.fetchone()
            if rbb:
                new_count = int(rbb["count"] or 0) + 1
                await cur.execute("UPDATE province_buildings SET count=? WHERE rowid=?", (new_count, rbb["rowid"]))
            else:
                await cur.execute("INSERT INTO province_buildings (province_id, building_id, tier, count) VALUES (?, ?, ?, ?)", (target_pid, building_id, b["tier"], 1))
            # mark build completed
            await cur.execute("UPDATE state_builds SET status='completed' WHERE id=?", (build_id,))
        else:
            # no province to install into: mark failed
            await cur.execute("UPDATE state_builds SET status='failed' WHERE id=?", (build_id,))

    # Apply production for every province building (simple model)
    await cur.execute("""
        SELECT pb.province_id, pb.count, pb.tier, bt.outputs, bt.inputs
        FROM province_buildings pb
        JOIN building_templates bt ON bt.id = pb.building_id
    """)
    bld_rows = await cur.fetchall()
    for br in bld_rows:
        pid = br["province_id"]
        count = int(br["count"] or 1)
        tier = int(br["tier"] or 1)
        mult = count * tier
        try:
            outputs = json.loads(br["outputs"] or "{}")
        except Exception:
            outputs = {}
        try:
            inputs = json.loads(br["inputs"] or "{}")
        except Exception:
            inputs = {}
        # consume inputs greedily (reduce stockpile amounts)
        for res, amt in inputs.items():
            need = float(amt) * mult
            await cur.execute("SELECT amount FROM province_stockpiles WHERE province_id=? AND resource=?", (pid, res))
            r = await cur.fetchone()
            have = float(r["amount"] or 0) if r else 0.0
            use = min(have, need)
            if r:
                await cur.execute("UPDATE province_stockpiles SET amount=? WHERE province_id=? AND resource=?", (max(0.0, have - use), pid, res))
        # produce outputs: add to stockpiles up to capacity
        for res, amt in outputs.items():
            produce = float(amt) * mult
            await cur.execute("SELECT amount, capacity FROM province_stockpiles WHERE province_id=? AND resource=?", (pid, res))
            pr = await cur.fetchone()
            if pr:
                cur_amt = float(pr["amount"] or 0); cap = float(pr["capacity"] or 0)
                space = max(0.0, cap - cur_amt)
                add = min(space, produce)
                await cur.execute("UPDATE province_stockpiles SET amount=? WHERE province_id=? AND resource=?", (cur_amt + add, pid, res))
            else:
                # if no row present, insert with capacity default 1000 (you can change)
                cap = 1000
                add = min(cap, produce)
                await cur.execute("INSERT INTO province_stockpiles (province_id, resource, amount, capacity) VALUES (?, ?, ?, ?)", (pid, res, add, cap))

    # maintenance: apply basic maintenance penalty if cash insufficient (very simple model)
    # For each nation sum maintenance_cash and reduce cash; if short, increase nation's debt
    await cur.execute("""
        SELECT pn.nation_id, COALESCE(SUM(bt.maintenance_cash * pb.count),0) as maintenance_total
        FROM playernations pn
        LEFT JOIN provinces p ON p.controller_id = pn.nation_id
        LEFT JOIN province_buildings pb ON pb.province_id = p.province_id
        LEFT JOIN building_templates bt ON bt.id = pb.building_id
        GROUP BY pn.nation_id
    """)
    maint_rows = await cur.fetchall()
    for m in maint_rows:
        nid = m["nation_id"]; maintenance_due = float(m["maintenance_total"] or 0)
        await cur.execute("SELECT cash, debt FROM playernations WHERE nation_id=?", (nid,))
        nrow = await cur.fetchone()
        if not nrow:
            continue
        cash = float(nrow["cash"] or 0); debt = float(nrow["debt"] or 0)
        if cash >= maintenance_due:
            await cur.execute("UPDATE playernations SET cash = cash - ? WHERE nation_id=?", (maintenance_due, nid))
        else:
            short = maintenance_due - cash
            await cur.execute("UPDATE playernations SET cash = 0, debt = debt + ? WHERE nation_id=?", (short, nid))

    # update turn in config
    await cur.execute("UPDATE config SET value=? WHERE key='current_turn'", (str(next_turn),))
    await conn.commit(); await conn.close()
    return next_turn
