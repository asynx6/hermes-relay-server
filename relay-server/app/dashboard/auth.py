"""Admin dashboard authentication: password + JWT.

Password:
- If `DASHBOARD_PASSWORD` is set in the environment, that value is used
  (operator's choice, even if weak).
- If it is not set and no auth file exists, a **random** password is created,
  printed once to stdout, and only its PBKDF2 hash is stored in
  `data/dashboard_auth.json` (permission 0600). No weak default.

Session: HS256 JWT in an HttpOnly cookie. The token carries an `epoch` claim;
each password change bumps the epoch in `data/dashboard_jwt_epoch`, so all
old sessions are immediately rejected (revocation control without a denylist).
The JWT secret is persistent in `data/dashboard_jwt_secret` (a restart does
not invalidate sessions that have not yet expired).
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import jwt

from ..config import settings

AUTH_FILENAME = "dashboard_auth.json"
JWT_SECRET_FILENAME = "dashboard_jwt_secret"
JWT_EPOCH_FILENAME = "dashboard_jwt_epoch"
PBKDF2_ITERATIONS = 200_000
COOKIE_NAME = "hermes_dashboard"


class AuthError(Exception):
    """Old password is wrong or the credentials are invalid."""


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows: chmod is limited; the file still stays in the local data directory.


def _load_record() -> dict:
    """Get the password record; create it from env/random password if absent."""
    path = settings.data_dir / AUTH_FILENAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    env_password = os.environ.get("DASHBOARD_PASSWORD")
    if env_password:
        password, generated = env_password, False
    else:
        password, generated = secrets.token_urlsafe(12), True

    record = _new_record(password)
    _write_private(path, json.dumps(record))
    if generated:
        # The only time the plaintext password is ever visible.
        print(
            "\n" + "=" * 64 + "\n"
            "PASSWORD DASHBOARD DIBUAT OTOMATIS (tampil sekali saja):\n"
            f"  {password}\n"
            "Simpan sekarang. Setelah ini hanya hash yang disimpan.\n"
            + "=" * 64 + "\n",
            flush=True,
        )
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
    """Change the password. Fails with AuthError if the old password is wrong."""
    if not verify_password(old_password):
        raise AuthError("Password lama tidak sesuai.")
    if len(new_password) < 6:
        raise AuthError("Password baru minimal 6 karakter.")
    if len(new_password) > 128:
        raise AuthError("Password baru maksimal 128 karakter.")
    _write_private(
        settings.data_dir / AUTH_FILENAME, json.dumps(_new_record(new_password))
    )
    _bump_epoch()  # all old sessions become invalid immediately


# --- session epoch (token revocation) ---

def _epoch() -> int:
    path = settings.data_dir / JWT_EPOCH_FILENAME
    if path.exists():
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            return 0
    return 0


def _bump_epoch() -> None:
    _write_private(settings.data_dir / JWT_EPOCH_FILENAME, str(_epoch() + 1))


def _jwt_secret() -> str:
    path = settings.data_dir / JWT_SECRET_FILENAME
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    _write_private(path, secret)
    return secret


def issue_token() -> tuple[str, int]:
    """Create a dashboard session JWT. Return (token, exp_unix)."""
    now = int(time.time())
    exp = now + settings.session_hours * 3600
    token = jwt.encode(
        {"sub": "dashboard", "iat": now, "exp": exp, "epoch": _epoch()},
        _jwt_secret(),
        algorithm="HS256",
    )
    return token, exp


def verify_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("epoch") != _epoch():
        return None  # revoked (e.g. password changed)
    return payload
