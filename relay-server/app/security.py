"""Autentikasi & otorisasi: secret, token ber-scope, dependency agent.

Tiga jenis kredensial diterima:

1. **Token utama** agent (`agents.secret_hash`) — akses penuh (scope `*`).
   Dibuat saat registrasi, diputar lewat `POST /rotate-token`.
2. **Token sekunder** (`agent_tokens`) — dibuat eksplisit dengan daftar scope
   terbatas, bisa dicabut per token.
3. **Kunci lama dalam jendela grace** (`token_rotation_grace`) — hash kunci
   yang baru saja dirotasi tetap diterima sampai grace habis, supaya client
   yang sedang berjalan bisa menukar kunci tanpa putus.

`get_current_agent` mengembalikan **dict** (bukan sqlite3.Row) supaya bisa
membawa `scopes`; seluruh pemanggil memakai akses `agent["..."]` sehingga
perubahan ini transparan.
"""
import hashlib
import hmac
import json
import secrets
import sqlite3

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .database import connect
from .config import settings

bearer_scheme = HTTPBearer(auto_error=False)

# Scope yang dikenali. Token utama memakai "*" (akses penuh).
ALL_SCOPES = "*"
KNOWN_SCOPES = (
    "messages:read",
    "messages:write",
    "files:read",
    "files:write",
    "groups:read",
    "groups:write",
    "groups:admin",
    "agents:read",
)


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    """HTTPException dengan detail terstruktur; di-format envelope oleh handler di main.py."""
    return HTTPException(
        status_code=status_code,
        detail={"status": "error", "code": code, "message": message},
    )


def generate_secret() -> str:
    """Secret per-agent. Entropi 256 bit, SHA-256 cukup untuk disimpan sebagai hash."""
    return "hsk_" + secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def check_registration_key(key: str | None) -> None:
    if not key or not hmac.compare_digest(key, settings.registration_key):
        raise api_error(401, "UNAUTHORIZED", "Registration key tidak valid.")


def _agent_dict(row: sqlite3.Row, *, scopes: list[str], token_id: str | None,
                via_rotation: bool = False) -> dict:
    data = dict(row)
    data["scopes"] = scopes
    data["token_id"] = token_id
    data["via_rotation"] = via_rotation
    return data


def get_current_agent(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    if credentials is None or not credentials.credentials:
        raise api_error(401, "UNAUTHORIZED", "Header Authorization: Bearer <secret> wajib.")

    hashed = hash_secret(credentials.credentials)
    conn = connect()
    try:
        # 1) Token utama (akses penuh).
        row = conn.execute(
            "SELECT * FROM agents WHERE secret_hash = ?", (hashed,)
        ).fetchone()
        if row is not None:
            if row["status"] != "active":
                raise api_error(401, "UNAUTHORIZED", "Agent disuspend atau dicabut.")
            return _agent_dict(row, scopes=[ALL_SCOPES], token_id=None)

        # 2) Token sekunder ber-scope.
        token = conn.execute(
            "SELECT * FROM agent_tokens WHERE secret_hash = ?", (hashed,)
        ).fetchone()
        if token is not None:
            if token["status"] != "active":
                raise api_error(401, "UNAUTHORIZED", "Token sudah dicabut.")
            if token["expires_at"] is not None:
                expired = conn.execute(
                    "SELECT ? < strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS past",
                    (token["expires_at"],),
                ).fetchone()["past"]
                if expired:
                    raise api_error(401, "UNAUTHORIZED", "Token sudah kedaluwarsa.")
            owner = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (token["agent_id"],)
            ).fetchone()
            if owner is None or owner["status"] != "active":
                raise api_error(401, "UNAUTHORIZED", "Agent disuspend atau dicabut.")
            conn.execute(
                "UPDATE agent_tokens SET last_used_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (token["id"],),
            )
            conn.commit()
            return _agent_dict(
                owner, scopes=json.loads(token["scopes"]), token_id=token["token_id"]
            )

        # 3) Kunci lama yang masih dalam jendela grace setelah rotasi.
        grace = conn.execute(
            "SELECT * FROM token_rotation_grace WHERE old_hash = ? "
            "AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (hashed,),
        ).fetchone()
        if grace is not None:
            owner = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (grace["agent_id"],)
            ).fetchone()
            if owner is not None and owner["status"] == "active":
                return _agent_dict(
                    owner, scopes=[ALL_SCOPES], token_id=None, via_rotation=True
                )
    finally:
        conn.close()

    raise api_error(401, "UNAUTHORIZED", "Token tidak valid atau agent sudah dicabut.")


def require_scope(*needed: str):
    """Dependency factory: token harus punya SEMUA scope yang diminta.

    Token utama (scope `*`) selalu lolos. Penolakan memakai 403 — tokennya
    valid, hanya izinnya kurang — bukan 401.
    """

    def dependency(agent: dict = Depends(get_current_agent)) -> dict:
        scopes = agent.get("scopes") or []
        if ALL_SCOPES in scopes:
            return agent
        missing = [s for s in needed if s not in scopes]
        if missing:
            raise api_error(
                403,
                "FORBIDDEN",
                f"Token tidak punya scope: {', '.join(missing)}.",
            )
        return agent

    return dependency
