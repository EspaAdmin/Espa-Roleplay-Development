# services/invite.py
import random
import string
import logging
from typing import Dict, Any, List, Optional

from db import get_conn  # your DB helper returning an aiosqlite.Connection

log = logging.getLogger(__name__)

ALLOWED_ROLES = {"Secondary", "General", "Diplomat", "Citizen", "Staff-Visit"}


# -------------------------
# Utility
# -------------------------
def generate_invite_code(length: int = 10) -> str:
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=length))


# -------------------------
# Ensure required tables/columns exist (safe to call)
# -------------------------
async def ensure_tables_and_columns() -> None:
    """
    Create nation_invites and nation_players tables if missing.
    Also ensure both tables have a 'role' column (adds it if missing).
    """
    conn = await get_conn()
    try:
        # Create tables if not exist
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nation_invites (
                nation_id TEXT NOT NULL,
                invited_id TEXT NOT NULL,
                invite_code TEXT NOT NULL,
                invite_count INTEGER DEFAULT 0,
                role TEXT DEFAULT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (nation_id, invited_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nation_players (
                nation_id TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                role TEXT DEFAULT NULL,
                PRIMARY KEY (nation_id, discord_id)
            )
        """)
        await conn.commit()

        # Ensure 'role' exists in nation_invites (older DB might not)
        cur = await conn.execute("PRAGMA table_info(nation_invites)")
        cols = await cur.fetchall()
        col_names = {c["name"] for c in cols}
        if "role" not in col_names:
            try:
                await conn.execute("ALTER TABLE nation_invites ADD COLUMN role TEXT DEFAULT NULL")
                await conn.commit()
            except Exception:
                log.exception("Failed to add 'role' column to nation_invites")

        # Ensure 'role' exists in nation_players
        cur = await conn.execute("PRAGMA table_info(nation_players)")
        cols = await cur.fetchall()
        col_names = {c["name"] for c in cols}
        if "role" not in col_names:
            try:
                await conn.execute("ALTER TABLE nation_players ADD COLUMN role TEXT DEFAULT NULL")
                await conn.commit()
            except Exception:
                log.exception("Failed to add 'role' column to nation_players")

    finally:
        await conn.close()


# -------------------------
# Core service functions
# -------------------------
async def create_invite(inviter_discord_id: str, invited_discord_id: str, nation_id: str, role: Optional[str] = None) -> Dict[str, Any]:
    """
    Create or update an invite for invited_discord_id to join nation_id.
    Only allowed if inviter_discord_id is the primary owner of nation_id.
    role: one of ALLOWED_ROLES (if provided). If None, defaults to 'Secondary'.
    Returns {"ok":bool, "code":str, "error":str}
    """
    inviter_discord_id = str(inviter_discord_id)
    invited_discord_id = str(invited_discord_id)
    nation_id = str(nation_id)
    role = (role or "Secondary").strip()
    if role not in ALLOWED_ROLES:
        return {"ok": False, "error": f"Invalid role '{role}'. Allowed: {', '.join(sorted(ALLOWED_ROLES))}"}

    # Make sure tables/columns exist
    try:
        await ensure_tables_and_columns()
    except Exception:
        log.exception("ensure_tables_and_columns failed in create_invite")

    conn = await get_conn()
    try:
        # verify inviter is primary owner of the nation
        cur = await conn.execute("SELECT * FROM playernations WHERE nation_id = ? LIMIT 1", (nation_id,))
        pn = await cur.fetchone()
        if not pn:
            return {"ok": False, "error": "Nation not found."}

        owner_id = pn["owner_discord_id"] if "owner_discord_id" in pn.keys() else pn.get("owner_discord_id") if isinstance(pn, dict) else None
        # owner_id may be None in some DBs; disallow if mismatch
        if str(owner_id) != inviter_discord_id:
            return {"ok": False, "error": "Only the nation's primary owner may invite players."}

        # prevent inviting someone who is primary owner of ANY nation
        cur = await conn.execute("SELECT nation_id FROM playernations WHERE owner_discord_id = ? LIMIT 1", (invited_discord_id,))
        existing_owner = await cur.fetchone()
        if existing_owner:
            return {"ok": False, "error": "The invited user is already a primary owner of a nation and cannot be invited."}

        # prevent duplicate invite if user is already member of same nation
        cur = await conn.execute("SELECT 1 FROM nation_players WHERE nation_id = ? AND discord_id = ? LIMIT 1", (nation_id, invited_discord_id))
        already_member = await cur.fetchone()
        if already_member:
            return {"ok": False, "error": "The user is already a member of this nation."}

        # generate code and insert/upsert invite (store role)
        code = generate_invite_code(10)
        await conn.execute("""
            INSERT INTO nation_invites (nation_id, invited_id, invite_code, status, invite_count, role, created_at)
            VALUES (?, ?, ?, 'pending', 1, ?, datetime('now'))
            ON CONFLICT(nation_id, invited_id) DO UPDATE SET
                invite_code = excluded.invite_code,
                status = 'pending',
                invite_count = nation_invites.invite_count + 1,
                role = excluded.role,
                created_at = datetime('now')
        """, (nation_id, invited_discord_id, code, role))
        await conn.commit()
        return {"ok": True, "code": code, "role": role}
    except Exception as e:
        log.exception("create_invite failed")
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()


async def accept_invite(code: str, accepting_discord_id: str) -> Dict[str, Any]:
    """
    Accept an invite identified by 'code'. If valid and intended for this user,
    remove any previous membership in nation_players, insert new membership,
    and mark invite as accepted. When inserting into nation_players, copy the role
    from the invite (if present).
    Returns {"ok":bool, "message":str, "nation_id":str}
    """
    accepting_discord_id = str(accepting_discord_id)
    code = str(code).strip()

    # Ensure schema present
    try:
        await ensure_tables_and_columns()
    except Exception:
        log.exception("ensure_tables_and_columns failed in accept_invite")

    conn = await get_conn()
    try:
        # check invite exists
        cur = await conn.execute("SELECT nation_id, invited_id, status, role FROM nation_invites WHERE invite_code = ? LIMIT 1", (code,))
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "message": "Invalid or expired invite code."}

        # sqlite3.Row -> index access
        status = row["status"]
        invited_id = str(row["invited_id"])
        invite_nation_id = row["nation_id"]
        invite_role = row["role"] if "role" in row.keys() else None

        if status != "pending":
            return {"ok": False, "message": "This invite is no longer pending."}

        if invited_id != accepting_discord_id:
            return {"ok": False, "message": "This invite is not for your account."}

        nation_id = invite_nation_id

        # If the user is a primary owner of any nation, they cannot accept an invite
        cur = await conn.execute("SELECT nation_id FROM playernations WHERE owner_discord_id = ? LIMIT 1", (accepting_discord_id,))
        owner_row = await cur.fetchone()
        if owner_row:
            return {"ok": False, "message": "You are a primary owner of a nation and cannot join another nation."}

        # Remove previous membership(s) in nation_players for this user (so they move to this new nation)
        await conn.execute("DELETE FROM nation_players WHERE discord_id = ?", (accepting_discord_id,))

        # Insert membership in new nation, setting role from invite (or default to 'Secondary')
        role_to_set = invite_role if invite_role and invite_role in ALLOWED_ROLES else "Secondary"
        await conn.execute("INSERT OR REPLACE INTO nation_players (nation_id, discord_id, role) VALUES (?, ?, ?)", (nation_id, accepting_discord_id, role_to_set))

        # Mark invite accepted
        await conn.execute("UPDATE nation_invites SET status = 'accepted' WHERE invite_code = ?", (code,))
        await conn.commit()

        return {"ok": True, "message": f"You have joined the nation as {role_to_set}.", "nation_id": nation_id}
    except Exception as e:
        log.exception("accept_invite failed")
        return {"ok": False, "message": f"Internal error: {e}"}
    finally:
        await conn.close()


async def list_pending_invites_for_nation(requester_discord_id: str, nation_id: str) -> Dict[str, Any]:
    """
    Return pending invites for the nation, only if requester is primary owner of that nation.
    Returns {"ok":bool, "invites":[{invited_id, invite_code, invite_count, role, status}], "error":str}
    """
    requester_discord_id = str(requester_discord_id)
    nation_id = str(nation_id)

    conn = await get_conn()
    try:
        # confirm requester is the primary owner
        cur = await conn.execute("SELECT owner_discord_id FROM playernations WHERE nation_id = ? LIMIT 1", (nation_id,))
        pn = await cur.fetchone()
        if not pn:
            return {"ok": False, "error": "Nation not found."}
        if str(pn["owner_discord_id"]) != requester_discord_id:
            return {"ok": False, "error": "Only the primary owner may view pending invites."}

        cur = await conn.execute("""
            SELECT invited_id, invite_code, invite_count, role, status, created_at
            FROM nation_invites
            WHERE nation_id = ? AND status = 'pending'
            ORDER BY created_at DESC
        """, (nation_id,))
        rows = await cur.fetchall()
        invites = [dict(r) for r in rows]
        return {"ok": True, "invites": invites}
    except Exception as e:
        log.exception("list_pending_invites_for_nation failed")
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()


async def add_member_by_staff(nation_id: str, discord_id: str, role: Optional[str] = "Staff-Visit") -> Dict[str, Any]:
    """
    Admin/staff function to add a user to a nation. Sets role to 'Staff-Visit' by default.
    Returns {"ok":bool, "message":str}.
    """
    nation_id = str(nation_id)
    discord_id = str(discord_id)
    role = (role or "Staff-Visit").strip()
    if role not in ALLOWED_ROLES:
        return {"ok": False, "error": f"Invalid role '{role}'. Allowed: {', '.join(sorted(ALLOWED_ROLES))}"}

    try:
        await ensure_tables_and_columns()
    except Exception:
        log.exception("ensure_tables_and_columns failed in add_member_by_staff")

    conn = await get_conn()
    try:
        # ensure nation exists
        cur = await conn.execute("SELECT 1 FROM playernations WHERE nation_id = ? LIMIT 1", (nation_id,))
        if not await cur.fetchone():
            return {"ok": False, "error": "Nation not found."}

        # Check if the user is primary owner of another nation; staff should not overwrite owners
        cur = await conn.execute("SELECT nation_id FROM playernations WHERE owner_discord_id = ? LIMIT 1", (discord_id,))
        if await cur.fetchone():
            return {"ok": False, "error": "That user is a primary owner of a nation; cannot add as member."}

        # Insert or replace membership
        await conn.execute("INSERT OR REPLACE INTO nation_players (nation_id, discord_id, role) VALUES (?, ?, ?)", (nation_id, discord_id, role))
        await conn.commit()
        return {"ok": True, "message": f"User {discord_id} added to nation {nation_id} as {role}."}
    except Exception as e:
        log.exception("add_member_by_staff failed")
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()
async def promote_member(nation_id: str, discord_id: str, new_role: str) -> Dict[str, Any]:
    """
    Change `discord_id`'s role in `nation_id` to new_role.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    nation_id = str(nation_id)
    discord_id = str(discord_id)
    new_role = str(new_role).strip()
    if new_role not in ALLOWED_ROLES:
        return {"ok": False, "error": f"Invalid role '{new_role}'. Allowed: {', '.join(sorted(ALLOWED_ROLES))}"}

    try:
        await ensure_tables_and_columns()
    except Exception:
        log.exception("ensure_tables_and_columns failed in promote_member")

    conn = await get_conn()
    try:
        # ensure nation exists
        cur = await conn.execute("SELECT owner_discord_id FROM playernations WHERE nation_id = ? LIMIT 1", (nation_id,))
        pn = await cur.fetchone()
        if not pn:
            return {"ok": False, "error": "Nation not found."}
        owner_id = pn["owner_discord_id"] if "owner_discord_id" in pn.keys() else None

        # prevent promoting/removing the primary owner via this function
        if owner_id and str(owner_id) == discord_id:
            return {"ok": False, "error": "Cannot change the role of the primary owner via promote. Transfer of ownership must be done manually."}

        # ensure the user is a member of the nation
        cur = await conn.execute("SELECT role FROM nation_players WHERE nation_id = ? AND discord_id = ? LIMIT 1", (nation_id, discord_id))
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "error": "User is not a member of that nation."}

        prev_role = row["role"] if "role" in row.keys() else row.get("role") if isinstance(row, dict) else None
        await conn.execute("UPDATE nation_players SET role = ? WHERE nation_id = ? AND discord_id = ?", (new_role, nation_id, discord_id))
        await conn.commit()
        return {"ok": True, "previous_role": prev_role, "new_role": new_role}
    except Exception as e:
        log.exception("promote_member failed")
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()


async def remove_member_by_staff(nation_id: str, discord_id: str) -> Dict[str, Any]:
    """
    Remove a member (discord_id) from the nation (nation_id).
    Prevents removing the primary owner.
    Returns {"ok": True, "removed_role": ...} or {"ok": False, "error": ...}
    """
    nation_id = str(nation_id)
    discord_id = str(discord_id)

    try:
        await ensure_tables_and_columns()
    except Exception:
        log.exception("ensure_tables_and_columns failed in remove_member_by_staff")

    conn = await get_conn()
    try:
        # ensure nation exists and find owner
        cur = await conn.execute("SELECT owner_discord_id FROM playernations WHERE nation_id = ? LIMIT 1", (nation_id,))
        pn = await cur.fetchone()
        if not pn:
            return {"ok": False, "error": "Nation not found."}
        owner_id = pn["owner_discord_id"] if "owner_discord_id" in pn.keys() else None
        if owner_id and str(owner_id) == discord_id:
            return {"ok": False, "error": "Cannot remove the primary owner via this command."}

        # check membership and capture previous role to return
        cur = await conn.execute("SELECT role FROM nation_players WHERE nation_id = ? AND discord_id = ? LIMIT 1", (nation_id, discord_id))
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "error": "User is not a member of that nation."}
        prev_role = row["role"] if "role" in row.keys() else None

        # delete membership
        await conn.execute("DELETE FROM nation_players WHERE nation_id = ? AND discord_id = ?", (nation_id, discord_id))
        await conn.commit()
        return {"ok": True, "removed_role": prev_role}
    except Exception as e:
        log.exception("remove_member_by_staff failed")
        return {"ok": False, "error": str(e)}
    finally:
        await conn.close()