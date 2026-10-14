"""Kelola token agent: token ber-scope, pencabutan, dan rotasi kunci utama."""
import json
import secrets

from fastapi import APIRouter, Depends, Request, status

from ..config import settings
from ..database import begin_immediate, connect
from ..ratelimit import limiter
from ..schemas import CreateTokenRequest
from ..security import ALL_SCOPES, api_error, generate_secret, get_current_agent, hash_secret
from ..services import audit_service

router = APIRouter()


def _new_token_id(conn) -> str:
    while True:
        candidate = "tk_" + secrets.token_hex(5)
        if not conn.execute(
            "SELECT 1 FROM agent_tokens WHERE token_id = ?", (candidate,)
        ).fetchone():
            return candidate


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
@limiter.limit(lambda: settings.rate_limit_default)
def create_token(
    body: CreateTokenRequest, request: Request, agent=Depends(get_current_agent)
):
    """Buat token sekunder ber-scope untuk agent pemanggil.

    Berguna untuk menaruh kredensial di tempat yang kurang aman (mis. token
    baca-saja), atau membatasi token ke satu jenis operasi. Secret hanya
    ditampilkan sekali di respons ini.
    """
    secret = generate_secret()
    conn = connect()
    try:
        begin_immediate(conn)
        token_id = _new_token_id(conn)
        expires_at = None
        if body.expires_in_days:
            expires_at = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) AS t",
                (f"+{body.expires_in_days} days",),
            ).fetchone()["t"]
        conn.execute(
            """INSERT INTO agent_tokens
                 (token_id, agent_id, label, secret_hash, scopes, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                token_id,
                agent["id"],
                body.label,
                hash_secret(secret),
                json.dumps(body.scopes),
                expires_at,
            ),
        )
        audit_service.record(
            conn,
            actor=agent["name"],
            actor_type="agent",
            action=audit_service.ACTION_CREATE_TOKEN,
            target=token_id,
            detail={"scopes": body.scopes, "label": body.label},
            ip=_client_ip(request),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "data": {
            "token_id": token_id,
            "scopes": body.scopes,
            "label": body.label,
            "expires_at": expires_at,
            "secret_key": secret,
        },
        "note": "Simpan secret_key sekarang. Nilai ini tidak akan ditampilkan lagi.",
    }


@router.get("/tokens")
@limiter.limit(lambda: settings.rate_limit_default)
def list_tokens(request: Request, agent=Depends(get_current_agent)):
    """Daftar token sekunder milik agent ini (tanpa secret)."""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT token_id, label, scopes, status, created_at, last_used_at, expires_at
               FROM agent_tokens WHERE agent_id = ? ORDER BY created_at DESC""",
            (agent["id"],),
        ).fetchall()
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "tokens": [
                {
                    "token_id": r["token_id"],
                    "label": r["label"],
                    "scopes": json.loads(r["scopes"]),
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "last_used_at": r["last_used_at"],
                    "expires_at": r["expires_at"],
                }
                for r in rows
            ]
        },
    }


@router.delete("/tokens/{token_id}")
@limiter.limit(lambda: settings.rate_limit_default)
def revoke_token(token_id: str, request: Request, agent=Depends(get_current_agent)):
    """Cabut satu token sekunder. Token utama tidak bisa dicabut di sini."""
    conn = connect()
    try:
        begin_immediate(conn)
        row = conn.execute(
            "SELECT id, status FROM agent_tokens WHERE token_id = ? AND agent_id = ?",
            (token_id, agent["id"]),
        ).fetchone()
        if row is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Token '{token_id}' tidak ditemukan.")
        conn.execute(
            "UPDATE agent_tokens SET status = 'revoked' WHERE id = ?", (row["id"],)
        )
        audit_service.record(
            conn,
            actor=agent["name"],
            actor_type="agent",
            action=audit_service.ACTION_REVOKE_TOKEN,
            target=token_id,
            ip=_client_ip(request),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "data": {"token_id": token_id, "status": "revoked"}}


@router.post("/rotate-token")
@limiter.limit(lambda: settings.rate_limit_default)
def rotate_token(request: Request, agent=Depends(get_current_agent)):
    """Putar kunci utama agent.

    Kunci lama tetap diterima selama `TOKEN_GRACE_MINUTES` (default 5 menit),
    jadi client yang sedang berjalan punya waktu menukar kunci tanpa putus.
    Setelah grace habis, kunci lama otomatis ditolak (401).
    """
    new_secret = generate_secret()
    conn = connect()
    try:
        begin_immediate(conn)
        old_hash = conn.execute(
            "SELECT secret_hash FROM agents WHERE id = ?", (agent["id"],)
        ).fetchone()["secret_hash"]

        if settings.token_grace_minutes > 0:
            conn.execute(
                """INSERT OR REPLACE INTO token_rotation_grace
                     (old_hash, agent_id, expires_at)
                   VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?))""",
                (old_hash, agent["id"], f"+{settings.token_grace_minutes} minutes"),
            )
        else:
            conn.execute(
                "DELETE FROM token_rotation_grace WHERE agent_id = ?", (agent["id"],)
            )

        # Baris grace kedaluwarsa dibersihkan sekalian.
        conn.execute(
            "DELETE FROM token_rotation_grace WHERE expires_at < "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        )

        conn.execute(
            "UPDATE agents SET secret_hash = ?, token_epoch = token_epoch + 1 WHERE id = ?",
            (hash_secret(new_secret), agent["id"]),
        )
        audit_service.record(
            conn,
            actor=agent["name"],
            actor_type="agent",
            action=audit_service.ACTION_ROTATE_TOKEN,
            target=agent["agent_id"],
            detail={"grace_minutes": settings.token_grace_minutes},
            ip=_client_ip(request),
        )
        conn.commit()
        epoch = conn.execute(
            "SELECT token_epoch FROM agents WHERE id = ?", (agent["id"],)
        ).fetchone()["token_epoch"]
    finally:
        conn.close()

    return {
        "status": "ok",
        "data": {
            "agent_id": agent["agent_id"],
            "secret_key": new_secret,
            "token_epoch": epoch,
            "grace_minutes": settings.token_grace_minutes,
            "scopes": [ALL_SCOPES],
        },
        "note": "Simpan secret_key sekarang. Kunci lama hanya berlaku sampai grace habis.",
    }
