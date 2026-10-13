"""Autentikasi dashboard admin: password + JWT.

Password default '123456', disimpan sebagai PBKDF2-SHA256 (200k iterasi)
beserta salt acak di `data/dashboard_auth.json`. Bisa diganti lewat UI
dashboard atau environment `DASHBOARD_PASSWORD` pada instalasi pertama.

Sesi memakai JWT di cookie HttpOnly dengan masa berlaku 24 jam (atur lewat
`SESSION_HOURS` di .env). Token yang kedaluwarsa ditolak server, dan
frontend melakukan auto-logout saat masa berlaku habis.
"""
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

import jwt

from ..config import settings

AUTH_FILENAME = "dashboard_auth.json"
JWT_SECRET_FILENAME = "dashboard_jwt_secret"
PBKDF2_ITERATIONS = 200_000
COOKIE_NAME = "hermes_dashboard"


class AuthError(Exception):
    """Password lama salah atau kredensial tidak valid."""


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows: chmod terbatas; file tetap di direktori data lokal.


def _load_record() -> dict:
    """Ambil record password; buat dari default/env kalau belum ada."""
    path = settings.data_dir / AUTH_FILENAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    password = settings.dashboard_password or "123456"
    record = _new_record(password)
    _write_private(path, json.dumps(record))
    return record


def _new_record(password: str) -> dict:
    salt = secrets.token_bytes(16)
    return {
        "salt": salt.hex(),
        "hash": _pbkdf2(password, salt),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _pbkdf2(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()


def verify_password(password: str) -> bool:
    record = _load_record()
    salt = bytes.fromhex(record["salt"])
    return hmac.compare_digest(_pbkdf2(password, salt), record["hash"])


def change_password(old_password: str, new_password: str) -> None:
    """Ganti password. Gagal dengan AuthError kalau password lama salah."""
    if not verify_password(old_password):
        raise AuthError("Password lama tidak sesuai.")
    if len(new_password) < 6:
        raise AuthError("Password baru minimal 6 karakter.")
    if len(new_password) > 128:
        raise AuthError("Password baru maksimal 128 karakter.")
    record = _new_record(new_password)
    _write_private(settings.data_dir / AUTH_FILENAME, json.dumps(record))


def _jwt_secret() -> str:
    path = settings.data_dir / JWT_SECRET_FILENAME
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    _write_private(path, secret)
    return secret


def issue_token() -> tuple[str, int]:
    """Buat JWT sesi dashboard. Kembalikan (token, exp_unix)."""
    now = int(time.time())
    exp = now + settings.session_hours * 3600
    token = jwt.encode(
        {"sub": "dashboard", "iat": now, "exp": exp},
        _jwt_secret(),
        algorithm="HS256",
    )
    return token, exp


def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
