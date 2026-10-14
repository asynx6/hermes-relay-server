"""Akses SQLite: satu koneksi per operasi, WAL, foreign keys aktif."""
import sqlite3
import time
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


def begin_immediate(conn: sqlite3.Connection, max_attempts: int = 8) -> None:
    """BEGIN IMMEDIATE dengan retry untuk konflik lock sesaat.

    Di mode WAL, beberapa penulis bersamaan kadang mendapat
    "database is locked" atau (pada beberapa platform/filesystem)
    "attempt to write a readonly database" yang sebenarnya transien.
    busy_timeout menangani kasus umum; retri ini menangani sisanya supaya
    request balapan berakhir 409/200, bukan 500.
    """
    for attempt in range(max_attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError:
            if attempt == max_attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def init_db() -> None:
    """Buat semua tabel dari schema.sql (aman dijalankan berulang)."""
    conn = connect()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
