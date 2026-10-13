"""POST /groups dan POST /groups/{name}/join."""
from fastapi import APIRouter, Depends, Request, status

from ..config import settings
from ..database import connect
from ..ratelimit import limiter
from ..schemas import CreateGroupRequest
from ..security import api_error, get_current_agent
from ..services import message_service

router = APIRouter()


@router.post("/groups", status_code=status.HTTP_201_CREATED)
@limiter.limit(lambda: settings.rate_limit_default)
def create_group(body: CreateGroupRequest, request: Request, agent=Depends(get_current_agent)):
    conn = connect()
    try:
        clash = conn.execute(
            "SELECT 1 FROM groups WHERE name = ? COLLATE NOCASE", (body.name,)
        ).fetchone()
        if clash:
            raise api_error(409, "CONFLICT", f"Nama grup '{body.name}' sudah dipakai.")

        cur = conn.execute(
            "INSERT INTO groups (name, description, created_by) VALUES (?, ?, ?)",
            (body.name, body.description, agent["id"]),
        )
        group_id = cur.lastrowid
        # Pembuat otomatis menjadi anggota.
        conn.execute(
            "INSERT INTO group_members (group_id, agent_id) VALUES (?, ?)",
            (group_id, agent["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    return {"status": "ok", "data": {"group_id": group_id, "name": body.name}}


@router.get("/groups")
@limiter.limit(lambda: settings.rate_limit_default)
def list_groups(request: Request, agent=Depends(get_current_agent)):
    """Direktori grup: semua grup yang ada + status keanggotaan pemanggil."""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT g.name, g.description, g.created_at,
                      (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS member_count,
                      (SELECT gm2.joined_at FROM group_members gm2
                        WHERE gm2.group_id = g.id AND gm2.agent_id = ?) AS my_joined_at
               FROM groups g ORDER BY g.created_at""",
            (agent["id"],),
        ).fetchall()
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "groups": [
                {
                    "name": r["name"],
                    "description": r["description"],
                    "created_at": r["created_at"],
                    "member_count": r["member_count"],
                    "is_member": r["my_joined_at"] is not None,
                    "joined_at": r["my_joined_at"],
                }
                for r in rows
            ]
        },
    }


@router.post("/groups/{name}/join")
@limiter.limit(lambda: settings.rate_limit_default)
def join_group(name: str, request: Request, agent=Depends(get_current_agent)):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT gm.joined_at FROM group_members gm JOIN groups g ON g.id = gm.group_id "
            "WHERE g.name = ? COLLATE NOCASE AND gm.agent_id = ?",
            (name, agent["id"]),
        ).fetchone()
        if row:
            # Sudah anggota: operasi idempotent.
            return {"status": "ok", "data": {"group": name, "joined_at": row["joined_at"]}}

        group = message_service.resolve_group_target(conn, name)
        conn.execute(
            "INSERT INTO group_members (group_id, agent_id) VALUES (?, ?)",
            (group["id"], agent["id"]),
        )
        joined_at = conn.execute(
            "SELECT joined_at FROM group_members WHERE group_id = ? AND agent_id = ?",
            (group["id"], agent["id"]),
        ).fetchone()["joined_at"]
        conn.commit()
    finally:
        conn.close()

    return {"status": "ok", "data": {"group": name, "joined_at": joined_at}}
