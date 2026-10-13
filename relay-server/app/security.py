"""Autentikasi: pembuatan secret, hashing, dependency agent, validasi registration key."""
import hashlib
import hmac
import secrets
import sqlite3

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .database import connect
from .config import settings

bearer_scheme = HTTPBearer(auto_error=False)


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


def get_current_agent(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> sqlite3.Row:
    if credentials is None or not credentials.credentials:
        raise api_error(401, "UNAUTHORIZED", "Header Authorization: Bearer <secret> wajib.")

    secret = credentials.credentials
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM agents WHERE secret_hash = ?", (hash_secret(secret),)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise api_error(401, "UNAUTHORIZED", "Token tidak valid atau agent sudah dicabut.")
    if row["status"] != "active":
        raise api_error(401, "UNAUTHORIZED", "Agent disuspend atau dicabut.")
    return row
