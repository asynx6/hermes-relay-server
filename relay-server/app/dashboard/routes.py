"""Admin dashboard endpoints: agent monitoring, messages, and file manager.

All endpoints in this router require a JWT session (cookie `hermes_dashboard`),
except `/login` and `/logout`. No CORS: the dashboard is only for browsers
that access the server directly.
"""
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..config import settings
from ..database import begin_immediate, connect
from ..ratelimit import limiter
from ..security import api_error
from ..services import audit_service, file_service, message_service
from . import auth
from .schemas import (
    ChangePasswordRequest,
    CreateGroupRequest,
    JoinGroupRequest,
    LoginRequest,
    SendMessageRequest,
)

router = APIRouter()

CHUNK = 64 * 1024
SESSION_SECONDS = settings.session_hours * 3600


def require_session(request: Request) -> None:
    """Check the JWT cookie; reject with 401 if missing or expired."""
    token = request.cookies.get(auth.COOKIE_NAME)
    if not token or auth.verify_token(token) is None:
        raise api_error(401, "UNAUTHORIZED", "Sesi tidak valid atau sudah berakhir.")


session_dep = Depends(require_session)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _audit(request: Request, action: str, *, target: str | None = None,
           detail: dict | None = None, result: str = "ok") -> None:
    """Record a dashboard action. Audit failure does not fail the main action."""
    conn = connect()
    try:
        audit_service.safe_record(
            conn,
            actor="dashboard",
            actor_type="dashboard",
            action=action,
            target=target,
            detail=detail,
            ip=_client_ip(request),
            result=result,
        )
    finally:
        conn.close()


@router.post("/login")
@limiter.limit(lambda: settings.rate_limit_login)
def login(body: LoginRequest, request: Request):
    if not auth.verify_password(body.password):
        _audit(request, audit_service.ACTION_LOGIN_FAILED, result="denied")
        raise api_error(401, "UNAUTHORIZED", "Password salah.")
    token, exp = auth.issue_token()
    response = JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "data": {"expires_at": exp, "session_seconds": SESSION_SECONDS},
        },
    )
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request):
    response = JSONResponse(status_code=200, content={"status": "ok", "data": {}})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@router.get("/me")
def me(request: Request, _=session_dep):
    token = request.cookies.get(auth.COOKIE_NAME)
    payload = auth.verify_token(token)
    return {
        "status": "ok",
        "data": {
            "expires_at": payload["exp"],
            "session_seconds": payload["exp"] - int(time.time()),
        },
    }


@router.post("/change-password")
@limiter.limit(lambda: settings.rate_limit_default)
def change_password(body: ChangePasswordRequest, request: Request, _=session_dep):
    try:
        auth.change_password(body.old_password, body.new_password)
    except auth.AuthError as exc:
        _audit(request, audit_service.ACTION_CHANGE_PASSWORD, result="denied")
        raise api_error(400, "BAD_REQUEST", str(exc))
    # Changing the password bumps the JWT epoch: all old sessions are immediately rejected.
    _audit(request, audit_service.ACTION_CHANGE_PASSWORD)
    return {"status": "ok", "data": {"message": "Password diperbarui."}}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@router.get("/agents")
@limiter.limit(lambda: settings.rate_limit_default)
def list_agents(request: Request, _=session_dep):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT agent_id, name, description, status, last_seen_at, created_at "
            "FROM agents ORDER BY created_at"
        ).fetchall()
        counts = conn.execute(
            "SELECT sender_id, COUNT(*) AS n FROM messages GROUP BY sender_id"
        ).fetchall()
    finally:
        conn.close()
    sent = {r["sender_id"]: r["n"] for r in counts}
    agents = [
        {
            "agent_id": r["agent_id"],
            "name": r["name"],
            "description": r["description"],
            "status": r["status"],
            "last_seen_at": r["last_seen_at"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"status": "ok", "data": {"agents": agents}}


# ---------------------------------------------------------------------------
# Private chat
# ---------------------------------------------------------------------------

@router.get("/conversations")
@limiter.limit(lambda: settings.rate_limit_default)
def list_conversations(request: Request, _=session_dep):
    """List of agent pairs that have ever chatted directly."""
    conn = connect()
    try:
        pairs = conn.execute(
            """SELECT CASE WHEN m.sender_id < m.recipient_id THEN m.sender_id
                             ELSE m.recipient_id END AS low_id,
                      CASE WHEN m.sender_id < m.recipient_id THEN m.recipient_id
                             ELSE m.sender_id END AS high_id,
                      COUNT(*) AS message_count,
                      MAX(m.id) AS last_id
               FROM messages m
               WHERE m.recipient_id IS NOT NULL
               GROUP BY low_id, high_id
               ORDER BY last_id DESC"""
        ).fetchall()
        conversations = []
        for p in pairs:
            names = conn.execute(
                "SELECT name FROM agents WHERE id IN (?, ?)",
                (p["low_id"], p["high_id"]),
            ).fetchall()
            name_map = {n["name"] for n in names}
            if len(name_map) < 2:
                continue  # one of the agents was deleted
            last = conn.execute(
                "SELECT content, msg_type, created_at FROM messages WHERE id = ?",
                (p["last_id"],),
            ).fetchone()
            conversations.append(
                {
                    "agents": sorted(name_map),
                    "message_count": p["message_count"],
                    "last_at": last["created_at"],
                    "last_preview": last["content"] or f"[{last['msg_type']}]",
                }
            )
    finally:
        conn.close()
    return {"status": "ok", "data": {"conversations": conversations}}


@router.get("/conversations/{agent_a}/{agent_b}")
@limiter.limit(lambda: settings.rate_limit_default)
def conversation_thread(
    agent_a: str,
    agent_b: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _=session_dep,
):
    conn = connect()
    try:
        ids = conn.execute(
            "SELECT id FROM agents WHERE name IN (?, ?) COLLATE NOCASE",
            (agent_a, agent_b),
        ).fetchall()
        if len(ids) != 2:
            raise api_error(404, "NOT_FOUND", "Salah satu agent tidak ditemukan.")
        a, b = sorted(r["id"] for r in ids)
        total = conn.execute(
            """SELECT COUNT(*) AS n FROM messages m
               WHERE (m.sender_id = ? AND m.recipient_id = ?)
                  OR (m.sender_id = ? AND m.recipient_id = ?)""",
            (a, b, b, a),
        ).fetchone()["n"]
        # Fetch the most recent window first (DESC + OFFSET), then reverse to
        # chronological order so the UI still reads old -> new.
        rows = conn.execute(
            """SELECT m.id, m.msg_type, m.content, m.created_at, m.reply_to,
                      s.name AS sender, r.name AS recipient,
                      fl.file_id, fl.original_name, fl.size_bytes
               FROM messages m
               JOIN agents s ON s.id = m.sender_id
               JOIN agents r ON r.id = m.recipient_id
               LEFT JOIN file_logs fl ON fl.file_id = m.file_id
               WHERE (m.sender_id = ? AND m.recipient_id = ?)
                  OR (m.sender_id = ? AND m.recipient_id = ?)
               ORDER BY m.id DESC
               LIMIT ? OFFSET ?""",
            (a, b, b, a, limit, offset),
        ).fetchall()
        rows = list(reversed(rows))
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "agents": [agent_a, agent_b],
            "messages": [
                {
                    "id": r["id"],
                    "from": r["sender"],
                    "to": r["recipient"],
                    "msg_type": r["msg_type"],
                    "content": r["content"],
                    "file": _file_info(r),
                    "reply_to": r["reply_to"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        },
    }


# ---------------------------------------------------------------------------
# Group chat
# ---------------------------------------------------------------------------

def _resolve_agent_row(conn, name: str):
    row = conn.execute(
        "SELECT * FROM agents WHERE name = ? COLLATE NOCASE AND status = 'active'",
        (name,),
    ).fetchone()
    if row is None:
        raise api_error(404, "NOT_FOUND", f"Agent '{name}' tidak ditemukan.")
    return row


@router.post("/send-message")
@limiter.limit(lambda: settings.rate_limit_default)
def send_message(body: SendMessageRequest, request: Request, _=session_dep):
    """Send a message as an admin-chosen agent (see the note in schemas)."""
    conn = connect()
    try:
        begin_immediate(conn)
        sender = _resolve_agent_row(conn, body.as_agent)

        if body.target_type == "direct":
            if body.target_name.strip().lower() == sender["name"].lower():
                raise api_error(400, "BAD_REQUEST", "Tidak bisa mengirim ke diri sendiri.")
            target = message_service.resolve_direct_target(
                conn, body.target_id, body.target_name
            )
            recipient_id, group_id = target["id"], None
        else:
            group = message_service.resolve_group_target(
                conn, body.target_id, body.target_name
            )
            message_service.ensure_group_member(conn, group["id"], sender["id"])
            recipient_id, group_id = None, group["id"]

        row = message_service.store_message(
            conn,
            sender_row_id=sender["id"],
            client_msg_id=str(uuid.uuid4()),
            recipient_row_id=recipient_id,
            group_id=group_id,
            content=body.content,
            reply_to=None,
        )
        # The most sensitive action in the dashboard: send on behalf of another agent.
        audit_service.record(
            conn,
            actor="dashboard",
            actor_type="dashboard",
            action=audit_service.ACTION_SEND_AS_AGENT,
            target=f"{body.as_agent} -> {body.target_name}",
            detail={"target_type": body.target_type, "message_id": row["id"]},
            ip=_client_ip(request),
        )
        conn.commit()
        message_id, created_at = row["id"], row["created_at"]
    finally:
        conn.close()

    return JSONResponse(
        status_code=201,
        content={
            "status": "ok",
            "data": {
                "message_id": message_id,
                "from": body.as_agent,
                "target_type": body.target_type,
                "target": body.target_name,
                "target_id": body.target_id,
                "created_at": created_at,
            },
        },
    )


@router.post("/groups")
@limiter.limit(lambda: settings.rate_limit_default)
def create_group(body: CreateGroupRequest, request: Request, _=session_dep):
    conn = connect()
    try:
        clash = conn.execute(
            "SELECT 1 FROM groups WHERE name = ? COLLATE NOCASE", (body.name,)
        ).fetchone()
        if clash:
            raise api_error(409, "CONFLICT", f"Nama grup '{body.name}' sudah dipakai.")
        creator = _resolve_agent_row(conn, body.as_agent)
        public_id = message_service.new_group_id(conn)
        cur = conn.execute(
            "INSERT INTO groups (group_id, name, description, created_by) VALUES (?, ?, ?, ?)",
            (public_id, body.name, body.description, creator["id"]),
        )
        row_id = cur.lastrowid
        conn.execute(
            "INSERT INTO group_members (group_id, agent_id) VALUES (?, ?)",
            (row_id, creator["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse(
        status_code=201,
        content={
            "status": "ok",
            "data": {
                "group_id": public_id,
                "name": body.name,
                "creator": body.as_agent,
            },
        },
    )


@router.post("/groups/{name}/join")
@limiter.limit(lambda: settings.rate_limit_default)
def join_group(name: str, body: JoinGroupRequest, request: Request, _=session_dep):
    conn = connect()
    try:
        agent = _resolve_agent_row(conn, body.as_agent)
        group = conn.execute(
            "SELECT id, name FROM groups WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if group is None:
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
        existing = conn.execute(
            "SELECT joined_at FROM group_members WHERE group_id = ? AND agent_id = ?",
            (group["id"], agent["id"]),
        ).fetchone()
        if existing:
            return {"status": "ok", "data": {"group": name, "joined_at": existing["joined_at"], "already_member": True}}
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
    return {"status": "ok", "data": {"group": name, "joined_at": joined_at, "already_member": False}}


@router.get("/groups")
@limiter.limit(lambda: settings.rate_limit_default)
def list_groups(request: Request, _=session_dep):
    conn = connect()
    try:
        groups = conn.execute(
            """SELECT g.group_id, g.name, g.description, g.created_at,
                      (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS members
               FROM groups g ORDER BY g.created_at"""
        ).fetchall()
        members = conn.execute(
            """SELECT g.name AS group_name, a.name AS agent
               FROM group_members gm
               JOIN groups g ON g.id = gm.group_id
               JOIN agents a ON a.id = gm.agent_id
               ORDER BY g.name, a.name"""
        ).fetchall()
    finally:
        conn.close()
    by_group: dict[str, list[str]] = {}
    for m in members:
        by_group.setdefault(m["group_name"], []).append(m["agent"])
    return {
        "status": "ok",
        "data": {
            "groups": [
                {
                    "group_id": g["group_id"],
                    "name": g["name"],
                    "description": g["description"],
                    "created_at": g["created_at"],
                    "members": by_group.get(g["name"], []),
                }
                for g in groups
            ]
        },
    }


@router.get("/groups/{name}/messages")
@limiter.limit(lambda: settings.rate_limit_default)
def group_messages(
    name: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _=session_dep,
):
    conn = connect()
    try:
        group = conn.execute(
            "SELECT id, name FROM groups WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if group is None:
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE group_id = ?", (group["id"],)
        ).fetchone()["n"]
        rows = conn.execute(
            """SELECT m.id, m.msg_type, m.content, m.created_at, m.reply_to,
                      s.name AS sender, fl.file_id, fl.original_name, fl.size_bytes
               FROM messages m
               JOIN agents s ON s.id = m.sender_id
               LEFT JOIN file_logs fl ON fl.file_id = m.file_id
               WHERE m.group_id = ?
               ORDER BY m.id DESC
               LIMIT ? OFFSET ?""",
            (group["id"], limit, offset),
        ).fetchall()
        rows = list(reversed(rows))
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "group": group["name"],
            "messages": [
                {
                    "id": r["id"],
                    "from": r["sender"],
                    "msg_type": r["msg_type"],
                    "content": r["content"],
                    "file": _file_info(r),
                    "reply_to": r["reply_to"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        },
    }


# ---------------------------------------------------------------------------
# File manager
# ---------------------------------------------------------------------------

def _file_info(row) -> dict | None:
    """File metadata for file-type messages (None if it is not a file message)."""
    if not row["file_id"]:
        return None
    return {
        "file_id": row["file_id"],
        "original_name": row["original_name"],
        "size_bytes": row["size_bytes"],
    }


def _file_rows(conn, limit: int | None = None, offset: int = 0):
    sql = (
        """SELECT fl.file_id, fl.original_name, fl.stored_name, fl.mime_type,
                  fl.size_bytes, fl.sha256, fl.uploaded_at, fl.deleted_at,
                  fl.download_count,
                  u.name AS uploader,
                  r.name AS recipient,
                  g.name AS group_name
           FROM file_logs fl
           JOIN agents u ON u.id = fl.uploader_id
           LEFT JOIN agents r ON r.id = fl.recipient_id
           LEFT JOIN groups g ON g.id = fl.group_id
           ORDER BY fl.uploaded_at DESC, fl.id DESC"""
    )
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = (limit, offset)
    rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        exists = (settings.filemanager_dir / r["stored_name"]).is_file()
        result.append(
            {
                "file_id": r["file_id"],
                "original_name": r["original_name"],
                "stored_name": r["stored_name"],
                "mime_type": r["mime_type"],
                "size_bytes": r["size_bytes"],
                "sha256": r["sha256"],
                "uploader": r["uploader"],
                "target": r["recipient"] or f"grup {r['group_name']}",
                "uploaded_at": r["uploaded_at"],
                "deleted_at": r["deleted_at"],
                "download_count": r["download_count"],
                "exists_on_disk": exists,
            }
        )
    return result


@router.get("/files")
@limiter.limit(lambda: settings.rate_limit_default)
def list_files(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _=session_dep,
):
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM file_logs").fetchone()["n"]
        files = _file_rows(conn, limit=limit, offset=offset)
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "files": files,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(files) < total,
        },
    }


@router.get("/files/{stored_name}/download")
@limiter.limit(lambda: settings.rate_limit_default)
def download_file(stored_name: str, request: Request, _=session_dep):
    # The name is sanitized so it cannot be used for path traversal.
    safe = file_service.sanitize_filename(stored_name)
    stored = (settings.filemanager_dir / safe).resolve()
    if settings.filemanager_dir.resolve() not in stored.parents:
        raise api_error(400, "BAD_REQUEST", "Path tidak valid.")

    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM file_logs WHERE stored_name = ?", (safe,)
        ).fetchone()
        if row is None:
            raise api_error(404, "NOT_FOUND", "File tidak ditemukan.")
        if row["deleted_at"]:
            raise api_error(410, "GONE", "File sudah dihapus.")
        if not stored.is_file():
            raise api_error(404, "NOT_FOUND", "File fisik tidak ada di server.")
        conn.execute(
            "UPDATE file_logs SET download_count = download_count + 1 WHERE file_id = ?",
            (row["file_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    return FileResponse(
        stored,
        media_type=row["mime_type"] or "application/octet-stream",
        filename=row["original_name"],
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/files/{stored_name}")
@limiter.limit(lambda: settings.rate_limit_default)
def delete_file(stored_name: str, request: Request, _=session_dep):
    """Delete the file from disk and mark deleted_at (the message remains,
    downloads via the agent API will return 410)."""
    safe = file_service.sanitize_filename(stored_name)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM file_logs WHERE stored_name = ?", (safe,)
        ).fetchone()
        if row is None:
            raise api_error(404, "NOT_FOUND", "File tidak ditemukan.")
        if row["deleted_at"]:
            raise api_error(409, "CONFLICT", "File sudah dihapus sebelumnya.")

        stored = settings.filemanager_dir / safe
        stored.unlink(missing_ok=True)
        conn.execute(
            "UPDATE file_logs SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE file_id = ?",
            (row["file_id"],),
        )
        # Hardlink dedupe: deleting one name does not delete the content as
        # long as another row still points to the same inode.
        audit_service.record(
            conn,
            actor="dashboard",
            actor_type="dashboard",
            action=audit_service.ACTION_DELETE_FILE,
            target=row["file_id"],
            detail={"stored_name": safe, "original_name": row["original_name"]},
            ip=_client_ip(request),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "data": {"deleted": safe}}


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

@router.get("/audit")
@limiter.limit(lambda: settings.rate_limit_default)
def list_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, max_length=64),
    actor_type: str | None = Query(default=None, max_length=16),
    _=session_dep,
):
    """Trail of administrative & security actions (newest first)."""
    conn = connect()
    try:
        rows, total = audit_service.recent(
            conn, limit=limit, offset=offset, action=action, actor_type=actor_type
        )
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "entries": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        },
    }
