"""Konfigurasi server dari environment / file .env."""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    registration_key: str = "change-me-in-env"
    db_path: Path = Path("data/relay.db")
    filemanager_dir: Path = Path("filemanager")
    max_upload_mb: int = Field(default=25, ge=1, le=1024)
    blocked_ext: str = ".exe,.bat,.cmd,.msi,.dll,.scr,.ps1"
    retention_days: int = 30
    rate_limit_register: str = "5/minute"
    rate_limit_default: str = "120/minute"
    rate_limit_login: str = "10/minute"

    # Dashboard admin
    dashboard_password: str = "123456"
    session_hours: int = Field(default=24, ge=1, le=8760)
    cookie_secure: bool = False  # True kalau dashboard dilayani lewat HTTPS

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def blocked_ext_set(self) -> set[str]:
        return {e.strip().lower() for e in self.blocked_ext.split(",") if e.strip()}


settings = Settings()
