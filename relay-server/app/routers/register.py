"""POST /register dan GET /health."""
import json

from fastapi import APIRouter, Header, Request, status

from ..config import settings
from ..database import connect
from ..ratelimit import limiter
from ..schemas import RegisterRequest
from ..security import api_error, check_registration_key, generate_secret, hash_secret
from ..services import message_service

router = APIRouter()

SERVER_VERSION = "1.0.0"


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit(lambda: settings.rate_limit_register)
def register(body: RegisterRequest, request: Request, x_registration_key: str | None = Header(None)):
    check_registration_key(x_registration_key)

    conn = connect()
    try:
        clash = conn.execute(
            "SELECT 1 FROM agents WHERE name = ? COLLATE NOCASE", (body.name,)
        ).fetchone()
        if clash:
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
        conn.commit()
        created = conn.execute(
            "SELECT created_at FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
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


@router.get("/health")
def health():
    return {"status": "ok", "version": SERVER_VERSION}
