"""Autentikasi dashboard admin: password + JWT.

Password:
- Kalau `DASHBOARD_PASSWORD` di-set di environment, nilai itu dipakai
  (pilihan operator, bahkan kalau lemah).
- Kalau tidak di-set dan belum ada file auth, password **acak** dibuat,
  dicetak sekali ke stdout, lalu hanya hash PBKDF2-nya yang disimpan di
  `data/dashboard_auth.json` (permission 0600). Tidak ada default lemah.

Sesi: JWT HS256 di cookie HttpOnly. Token membawa klaim `epoch`; setiap
ganti password menaikkan epoch di `data/dashboard_jwt_epoch`, sehingga
semua sesi lama langsung ditolak (kontrol pencabutan tanpa denylist).
Secret JWT persisten di `data/dashboard_jwt_secret` (restart tidak
membatalkan sesi yang belum kedaluwarsa).
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
    """Password lama salah atau kredensial tidak valid."""


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows: chmod terbatas; file tetap di direktori data lokal.


def _load_record() -> dict:
    """Ambil record password; buat dari env/password acak kalau belum ada."""
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
        # Satu-satunya kali password asli terlihat.
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
    """Ganti password. Gagal dengan AuthError kalau password lama salah."""
    if not verify_password(old_password):
        raise AuthError("Password lama tidak sesuai.")
    if len(new_password) < 6:
        raise AuthError("Password baru minimal 6 karakter.")
    if len(new_password) > 128:
        raise AuthError("Password baru maksimal 128 karakter.")
    _write_private(
        settings.data_dir / AUTH_FILENAME, json.dumps(_new_record(new_password))
    )
    _bump_epoch()  # semua sesi lama langsung tidak valid


# --- epoch sesi (pencabutan token) ---

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
    """Buat JWT sesi dashboard. Kembalikan (token, exp_unix)."""
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
        return None  # dibatalkan (mis. password diganti)
    return payload
