"""Server configuration from the environment / .env file."""
import secrets
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

    # Upload validation
    max_storage_mb_per_agent: int = Field(default=0, ge=0)  # 0 = no quota
    strict_mime_match: bool = False  # True = reject if extension & content do not match

    # Chunked & resumable uploads
    chunk_mb: int = Field(default=5, ge=1, le=64)  # maximum size of a single chunk
    chunk_session_hours: int = Field(default=24, ge=1)  # TTL for stray upload sessions
    max_chunked_mb: int = Field(default=512, ge=1)  # total limit for chunked uploads

    # Token security
    token_grace_minutes: int = Field(default=5, ge=0, le=1440)
    audit_log_retention_days: int = Field(default=90, ge=1)

    # Latency: long polling & SSE (optional; regular polling remains the fallback)
    long_poll_enabled: bool = True
    long_poll_max_seconds: int = Field(default=25, ge=0, le=60)
    sse_enabled: bool = True
    sse_heartbeat_seconds: int = Field(default=15, ge=5, le=60)

    # Admin dashboard
    dashboard_password: str = "123456"
    session_hours: int = Field(default=24, ge=1, le=8760)
    cookie_secure: bool = False  # True if the dashboard is served over HTTPS

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    @property
    def server_epoch(self) -> str:
        """Database instance identity. Clients store it; if it changes
        (DB reset), old since_id cursors can no longer be trusted."""
        path = self.data_dir / "server_epoch"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        epoch = secrets.token_hex(8)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(epoch, encoding="utf-8")
        return epoch

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_chunk_bytes(self) -> int:
        return self.chunk_mb * 1024 * 1024

    @property
    def max_chunked_bytes(self) -> int:
        return self.max_chunked_mb * 1024 * 1024

    @property
    def blocked_ext_set(self) -> set[str]:
        return {e.strip().lower() for e in self.blocked_ext.split(",") if e.strip()}


settings = Settings()
