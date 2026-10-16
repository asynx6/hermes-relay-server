"""Authentication & authorization: secrets, scoped tokens, agent dependency.

Three kinds of credentials are accepted:

1. **Primary agent token** (`agents.secret_hash`) — full access (scope `*`).
   Created at registration, rotated via `POST /rotate-token`.
2. **Secondary token** (`agent_tokens`) — explicitly created with a limited
   scope list, revocable per token.
3. **Old key within the grace window** (`token_rotation_grace`) — the hash of
   a key that was just rotated is still accepted until the grace period ends,
   so a running client can swap the key without interruption.

`get_current_agent` returns a **dict** (not sqlite3.Row) so it can carry
`scopes`; every caller uses `agent["..."]` access, so this change is
transparent.
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

# Recognized scopes. The primary token uses "*" (full access).
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
    """HTTPException with structured detail; the envelope is formatted by the handler in main.py."""
    return HTTPException(
        status_code=status_code,
        detail={"status": "error", "code": code, "message": message},
    )


def generate_secret() -> str:
    """Per-agent secret. 256-bit entropy; SHA-256 is enough to store as a hash."""
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
        # 1) Primary token (full access).
        row = conn.execute(
            "SELECT * FROM agents WHERE secret_hash = ?", (hashed,)
        ).fetchone()
        if row is not None:
            if row["status"] != "active":
                raise api_error(401, "UNAUTHORIZED", "Agent disuspend atau dicabut.")
            return _agent_dict(row, scopes=[ALL_SCOPES], token_id=None)

        # 2) Secondary scoped token.
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

        # 3) Old key still within the grace window after rotation.
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
    """Dependency factory: the token must have ALL requested scopes.

    The primary token (scope `*`) always passes. Rejection uses 403 — the
    token is valid, it just lacks permission — not 401.
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
