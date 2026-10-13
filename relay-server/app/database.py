"""Akses SQLite: satu koneksi per operasi, WAL, foreign keys aktif."""
import sqlite3
from pathlib import Path

from .config import settings

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def connect() -> sqlite3.Connection:
    """Buka koneksi baru. Aman untuk thread: tiap request memakai koneksinya sendiri."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    """Buat semua tabel dari schema.sql (aman dijalankan berulang)."""
    conn = connect()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
