"""Konfigurasi client dari config.json.

Setelah registrasi berhasil, registration_key dihapus dari file dan
secret_key terisi. File ini memuat secret, jadi permission diatur ke 0600.
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    "server_url": "https://relay.example.com/api/v1",
    "registration_key": "",
    "agent_name": "",
    "secret_key": "",
    "poll_min_seconds": 3,
    "poll_max_seconds": 30,
    "download_dir": "downloads",
    "max_upload_mb": 25,
}


class ClientConfig:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            raw = {}
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in raw.items() if v not in (None, "")})
        self.server_url: str = merged["server_url"].rstrip("/")
        self.registration_key: str = merged["registration_key"]
        self.agent_name: str = merged["agent_name"]
        self.secret_key: str = merged["secret_key"]
        self.poll_min_seconds: float = float(merged["poll_min_seconds"])
        self.poll_max_seconds: float = float(merged["poll_max_seconds"])
        self.download_dir: str = merged["download_dir"]
        self.max_upload_mb: int = int(merged["max_upload_mb"])

    @property
    def registered(self) -> bool:
        return bool(self.secret_key)

    def save_registration(self, secret_key: str) -> None:
        """Simpan secret dan buang registration_key (dipakai sekali)."""
        data = {
            "server_url": self.server_url,
            "agent_name": self.agent_name,
            "secret_key": secret_key,
            "poll_min_seconds": self.poll_min_seconds,
            "poll_max_seconds": self.poll_max_seconds,
            "download_dir": self.download_dir,
            "max_upload_mb": self.max_upload_mb,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self._restrict_permissions()
        self.secret_key = secret_key
        self.registration_key = ""

    def save(self) -> None:
        data = {
            "server_url": self.server_url,
            "registration_key": self.registration_key,
            "agent_name": self.agent_name,
            "secret_key": self.secret_key,
            "poll_min_seconds": self.poll_min_seconds,
            "poll_max_seconds": self.poll_max_seconds,
            "download_dir": self.download_dir,
            "max_upload_mb": self.max_upload_mb,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # Windows: chmod hanya mendukung flag read-only. Secret tetap
            # tersimpan, batas proteksinya maksimal sebisanya.
            pass
