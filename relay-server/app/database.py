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


# Kolom yang ditambahkan setelah rilis pertama. `CREATE TABLE IF NOT EXISTS`
# tidak bisa mengubah tabel yang sudah ada, jadi kolom baru ditambahkan di
# sini lewat ALTER TABLE (SQLite tidak mendukung IF NOT EXISTS untuk kolom).
# Untuk perubahan yang lebih berat (ganti tipe, hapus kolom), pakai Alembic.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("group_members", "role", "role TEXT NOT NULL DEFAULT 'member'"),
    ("groups", "join_code", "join_code TEXT"),
    ("agents", "token_epoch", "token_epoch INTEGER NOT NULL DEFAULT 0"),
]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if not existing:
        return  # tabel belum ada; schema.sql yang akan membuatnya
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def migrate(conn: sqlite3.Connection) -> None:
    """Tambahkan kolom yang hilang pada database lama (idempotent)."""
    for table, column, ddl in _ADDED_COLUMNS:
        _ensure_column(conn, table, column, ddl)


def init_db() -> None:
    """Buat semua tabel dari schema.sql, lalu terapkan migrasi kolom."""
    conn = connect()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        migrate(conn)
        conn.commit()
    finally:
        conn.close()
