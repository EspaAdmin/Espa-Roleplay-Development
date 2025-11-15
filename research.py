# services/research.py
import json
from db import get_conn

async def start_research(nation_id: str, tech_id: str):
    """
    Start a research project if nation can afford it.
    Does NOT instantly grant tech. Adds a row to research_projects table.
    """
    conn = await get_conn()
    cur = await conn.cursor()
    try:
        # load tech definition from catalog.db (assumes same get_conn uses DB routing; if not, adjust)
        await cur.execute("SELECT * FROM technologies WHERE id=?", (tech_id,))
        tech = await cur.fetchone()
        if not tech:
            await conn.close()
            return {"ok": False, "error": "Technology not found"}

        cost_cash = float(tech["cost_cash"] or 0)
        cost_resources = {}
        try:
            cost_resources = json.loads(tech.get("cost_resources") or "{}")
        except Exception:
            cost_resources = {}

        research_time = int(tech.get("research_time_turns") or 1)

        # check cash
        await cur.execute("SELECT COALESCE(cash,0) AS cash FROM playernations WHERE nation_id=? LIMIT 1", (nation_id,))
        prow = await cur.fetchone()
        cash_have = float(prow["cash"] or 0) if prow else 0.0
        if cash_have + 1e-9 < cost_cash:
            await conn.close()
            return {"ok": False, "error": f"Insufficient cash for research (need {cost_cash}, have {cash_have})"}

        # attempt to reserve/deduct resources (greedy)
        for resname, qty in cost_resources.items():
            # sum available
            await cur.execute("""
                SELECT SUM(ps.amount) AS total
                FROM province_stockpiles ps
                JOIN provinces p ON ps.province_id = p.province_id
                WHERE p.controller_id=? AND ps.resource=?
            """, (nation_id, resname))
            r = await cur.fetchone()
            have = float(r["total"] or 0) if r else 0
            if have + 1e-9 < float(qty):
                await conn.close()
                return {"ok": False, "error": f"Insufficient resource {resname} for research (need {qty}, have {have})"}

        # deduct cash now
        await cur.execute("UPDATE playernations SET cash = cash - ? WHERE nation_id=?", (cost_cash, nation_id))

        # greedily deduct resource amounts across provinces
        for resname, qty in cost_resources.items():
            remaining = float(qty)
            # pick provinces in order of node_strength
            await cur.execute("SELECT ps.rowid as ps_rowid, ps.amount, ps.province_id FROM province_stockpiles ps JOIN provinces p ON ps.province_id = p.province_id WHERE p.controller_id=? AND ps.resource=? ORDER BY p.node_strength DESC", (nation_id, resname))
            rows = await cur.fetchall()
            for rr in rows:
                avail = float(rr["amount"] or 0)
                take = min(avail, remaining)
                if take > 0:
                    new_amount = avail - take
                    await cur.execute("UPDATE province_stockpiles SET amount=? WHERE rowid=?", (new_amount, rr["ps_rowid"]))
                    remaining -= take
                if remaining <= 1e-9:
                    break
            if remaining > 1e-9:
                # should not happen because we checked above; rollback and return error
                await conn.rollback()
                await conn.close()
                return {"ok": False, "error": f"Failed to deduct resources {resname}"}

        # insert research project
        await cur.execute("SELECT value FROM config WHERE key='current_turn'")
        crow = await cur.fetchone()
        current_turn = int(crow["value"]) if crow else 1
        complete_turn = current_turn + research_time
        await cur.execute("""INSERT INTO research_projects (nation_id, tech_id, started_turn, complete_turn, status) VALUES (?,?,?,?,?)""",
                          (nation_id, tech_id, current_turn, complete_turn, 'pending'))
        await conn.commit()
        await conn.close()
        return {"ok": True, "complete_turn": complete_turn}
    except Exception as e:
        try:
            await conn.rollback()
        except Exception:
            pass
        await conn.close()
        return {"ok": False, "error": str(e)}


async def complete_research_projects(current_turn: int):
    """
    Call from tick/end-turn processing. For all research_projects with complete_turn <= current_turn and status='pending',
    mark them complete and add to player_technologies (if not already present).
    """
    conn = await get_conn()
    cur = await conn.cursor()
    try:
        await cur.execute("SELECT id, nation_id, tech_id FROM research_projects WHERE status='pending' AND complete_turn <= ?", (current_turn,))
        rows = await cur.fetchall()
        for r in rows:
            pid = r["id"]
            nation_id = r["nation_id"]; tech_id = r["tech_id"]
            # add to player_technologies if missing
            await cur.execute("INSERT OR IGNORE INTO player_technologies (nation_id, tech_id, acquired_turn) VALUES (?, ?, ?)",
                              (nation_id, tech_id, current_turn))
            await cur.execute("UPDATE research_projects SET status='completed' WHERE id=?", (pid,))
        await conn.commit()
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
    finally:
        await conn.close()
