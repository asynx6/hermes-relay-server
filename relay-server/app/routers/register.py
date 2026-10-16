"""POST /register, GET /agents (directory), and GET /health."""
import json
import sqlite3

from fastapi import APIRouter, Depends, Header, Request, status

from ..config import settings
from ..database import begin_immediate, connect
from ..ratelimit import limiter
from ..schemas import RegisterRequest
from ..security import (
    api_error,
    check_registration_key,
    generate_secret,
    hash_secret,
    require_scope,
)
from ..services import message_service

router = APIRouter()

SERVER_VERSION = "1.0.0"
REPLAY_WINDOW_SQL = "'-10 minutes'"


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit(lambda: settings.rate_limit_register)
def register(body: RegisterRequest, request: Request, x_registration_key: str | None = Header(None)):
    check_registration_key(x_registration_key)

    conn = connect()
    try:
        begin_immediate(conn)

        # 1) Replay: same nonce -> return the same secret.
        #    Saves the case where the 201 response is lost to a timeout: the
        #    retry does not produce a 409 and the agent name is not "burned".
        if body.registration_nonce:
            replay = conn.execute(
                "SELECT name, secret FROM registration_replays "
                "WHERE nonce = ? AND name = ? COLLATE NOCASE",
                (body.registration_nonce, body.name),
            ).fetchone()
            if replay:
                conn.commit()
                return {
                    "status": "ok",
                    "data": {
                        "agent_id": _agent_id_of(conn, replay["name"]),
                        "name": replay["name"],
                        "secret_key": replay["secret"],
                        "replayed": True,
                    },
                    "note": "Registrasi ini adalah replay dari permintaan sebelumnya.",
                }

        # 2) Name already taken (and not a replay) -> conflict.
        clash = conn.execute(
            "SELECT 1 FROM agents WHERE name = ? COLLATE NOCASE", (body.name,)
        ).fetchone()
        if clash:
            conn.commit()
            raise api_error(409, "CONFLICT", f"Nama '{body.name}' sudah terdaftar.")

        secret = generate_secret()
        agent_id = message_service.new_agent_id(conn)
        conn.execute(
            """INSERT INTO agents (agent_id, name, secret_hash, description, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (
                agent_id,
                body.name,
                hash_secret(secret),
                body.description,
                json.dumps(body.metadata) if body.metadata is not None else None,
            ),
        )
        if body.registration_nonce:
            conn.execute(
                "INSERT OR REPLACE INTO registration_replays (nonce, name, secret) "
                "VALUES (?, ?, ?)",
                (body.registration_nonce, body.name, secret),
            )
        conn.execute(
            "DELETE FROM registration_replays WHERE created_at < "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (REPLAY_WINDOW_SQL,),
        )
        conn.commit()
        created = conn.execute(
            "SELECT created_at FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    except (sqlite3.IntegrityError, sqlite3.OperationalError):
        # Race: two registrations of the same name at the same time. The second
        # one hits UNIQUE (or a write lock that remains held) here, not in the
        # earlier check.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise api_error(409, "CONFLICT", f"Nama '{body.name}' sudah terdaftar.")
    finally:
        conn.close()

    return {
        "status": "ok",
        "data": {
            "agent_id": agent_id,
            "name": body.name,
            "secret_key": secret,
            "created_at": created["created_at"],
        },
        "note": "Simpan secret_key sekarang. Nilai ini tidak akan ditampilkan lagi.",
    }


def _agent_id_of(conn, name: str) -> str | None:
    row = conn.execute(
        "SELECT agent_id FROM agents WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return row["agent_id"] if row else None


@router.get("/agents")
@limiter.limit(lambda: settings.rate_limit_default)
def list_agents(request: Request, agent=Depends(require_scope("agents:read"))):
    """Agent directory: everyone registered (for discovery before chatting)."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT agent_id, name, description, status, last_seen_at, created_at "
            "FROM agents ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "agents": [
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
        },
    }


@router.get("/health")
def health():
    return {"status": "ok", "version": SERVER_VERSION}
