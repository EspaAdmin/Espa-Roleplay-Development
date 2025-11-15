# services/audit.py
# Path: services/audit.py
import aiosqlite
import json
from datetime import datetime
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / "db_files"
ECON_DB = DB_DIR / "economy.db"
PLAYERS_DB = DB_DIR / "playernations.db"

async def _get_conn():
    return await aiosqlite.connect(ECON_DB)

async def init_audit_tables():
    conn = await _get_conn()
    await conn.executescript("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        turn INTEGER,
        timestamp TEXT,
        actor_nation TEXT,
        action_type TEXT,
        details TEXT
    );
    CREATE TABLE IF NOT EXISTS audit_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        archived_at TEXT,
        archived_turn INTEGER,
        payload TEXT
    );
    CREATE TABLE IF NOT EXISTS last_turn_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        turn INTEGER UNIQUE,
        snapshot JSON,
        created_at TEXT
    );
    """)
    await conn.commit()
    await conn.close()

async def log_action(actor_nation: str, action_type: str, details: dict, turn: int = None):
    """
    Persist an audit entry. 'details' will be JSON-serialized.
    """
    await init_audit_tables()
    conn = await _get_conn()
    ts = datetime.utcnow().isoformat()
    await conn.execute(
        "INSERT INTO audit_log (turn, timestamp, actor_nation, action_type, details) VALUES (?,?,?,?,?)",
        (turn, ts, actor_nation, action_type, json.dumps(details))
    )
    await conn.commit()
    await conn.close()

async def fetch_audit_for_turn(turn: int):
    conn = await _get_conn()
    cur = await conn.execute("SELECT * FROM audit_log WHERE turn=?", (turn,))
    rows = await cur.fetchall()
    await conn.close()
    return [dict(r) for r in rows]

async def archive_and_clear(turn: int):
    """
    Get all audit_log for turn, store as one archive payload, then delete them from audit_log.
    """
    await init_audit_tables()
    conn = await _get_conn()
    cur = await conn.execute("SELECT * FROM audit_log WHERE turn=?", (turn,))
    rows = await cur.fetchall()
    payload = json.dumps([dict(r) for r in rows])
    await conn.execute("INSERT INTO audit_archive (archived_at, archived_turn, payload) VALUES (?,?,?)", (datetime.utcnow().isoformat(), turn, payload))
    await conn.execute("DELETE FROM audit_log WHERE turn=?", (turn,))
    await conn.commit()
    await conn.close()

# Snapshot & rollback helpers
# We'll snapshot critical tables' content into a JSON blob.
async def create_snapshot(turn: int, table_list=None):
    """
    Read current state of important tables and save into last_turn_snapshot (turn).
    table_list: optional list of (db_path, table_name) tuples, otherwise use defaults.
    """
    # default tables across DBs
    default = [
        (str(PLAYERS_DB), "playernations"),
        (str(DB_DIR / "provinces.db"), "provinces"),
        (str(DB_DIR / "provinces.db"), "province_stockpiles"),
        (str(DB_DIR / "states.db"), "state_buildings"),
        (str(DB_DIR / "provinces.db"), "province_buildings"),
        (str(DB_DIR / "army.db"), "playerarmy"),
        (str(DB_DIR / "economy.db"), "trade_orders")
    ]
    tables = table_list or default
    snapshot = {}
    # For safety, we open each DB path separately (they are SQLite files)
    for dbpath, table in tables:
        try:
            conn = await aiosqlite.connect(dbpath)
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(f"SELECT * FROM {table}")
            rows = await cur.fetchall()
            await conn.close()
            snapshot[f"{Path(dbpath).name}:{table}"] = [dict(r) for r in rows]
        except Exception as e:
            snapshot[f"{Path(dbpath).name}:{table}"] = {"error": str(e)}
    # store snapshot in economy DB
    econ = await _get_conn()
    await econ.execute("INSERT OR REPLACE INTO last_turn_snapshot (turn, snapshot, created_at) VALUES (?,?,?)",
                       (turn, json.dumps(snapshot), datetime.utcnow().isoformat()))
    await econ.commit()
    await econ.close()
    return snapshot

async def get_last_snapshot(turn: int):
    await init_audit_tables()
    econ = await _get_conn()
    cur = await econ.execute("SELECT snapshot FROM last_turn_snapshot WHERE turn=?", (turn,))
    row = await cur.fetchone()
    await econ.close()
    if not row:
        return None
    return json.loads(row["snapshot"])

async def rollback_to_turn(turn: int):
    """
    Restore from last_turn_snapshot for `turn`. This is a dangerous operation
    and will wholesale delete and re-insert data for the snapshot tables.
    Use with caution (admin-only).
    """
    snap = await get_last_snapshot(turn)
    if not snap:
        raise RuntimeError("No snapshot for that turn")
    # restore each snapshot block - WARNING: destructive
    # The snapshot keys are like 'playernations.db:playernations' or 'provinces.db:provinces'
    for key, rows in snap.items():
        try:
            dbfile, tab = key.split(":", 1)
            dbpath = DB_DIR / dbfile
            conn = await aiosqlite.connect(dbpath)
            # delete all rows, then re-insert snapshot rows
            await conn.execute(f"DELETE FROM {tab}")
            if rows and isinstance(rows, list):
                # build an insert using column names from first row
                cols = rows[0].keys() if rows else []
                if cols:
                    col_list = ",".join(cols)
                    placeholders = ",".join(["?"] * len(cols))
                    insert_sql = f"INSERT INTO {tab} ({col_list}) VALUES ({placeholders})"
                    for r in rows:
                        vals = [r[c] for c in cols]
                        await conn.execute(insert_sql, vals)
            await conn.commit()
            await conn.close()
        except Exception as e:
            # if anything fails, abort and raise
            raise RuntimeError(f"Failed restoring {key}: {e}")
    return True
