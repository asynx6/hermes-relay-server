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
