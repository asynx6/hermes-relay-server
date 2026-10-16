"""SQLite access: one connection per operation, WAL, foreign keys enabled."""
import sqlite3
import time
from pathlib import Path

from .config import settings

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def connect() -> sqlite3.Connection:
    """Open a new connection. Thread-safe: each request uses its own connection."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def begin_immediate(conn: sqlite3.Connection, max_attempts: int = 8) -> None:
    """BEGIN IMMEDIATE with retry for transient lock conflicts.

    In WAL mode, several concurrent writers sometimes get
    "database is locked" or (on some platforms/filesystems)
    "attempt to write a readonly database" that is actually transient.
    busy_timeout handles the common case; this retry handles the rest so
    racing requests end as 409/200, not 500.
    """
    for attempt in range(max_attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError:
            if attempt == max_attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS`
# cannot alter an existing table, so new columns are added here
# via ALTER TABLE (SQLite does not support IF NOT EXISTS for columns).
# For heavier changes (type changes, dropping columns), use Alembic.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("group_members", "role", "role TEXT NOT NULL DEFAULT 'member'"),
    ("groups", "join_code", "join_code TEXT"),
    ("agents", "token_epoch", "token_epoch INTEGER NOT NULL DEFAULT 0"),
]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if not existing:
        return  # table does not exist yet; schema.sql will create it
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def migrate(conn: sqlite3.Connection) -> None:
    """Add missing columns on an older database (idempotent)."""
    for table, column, ddl in _ADDED_COLUMNS:
        _ensure_column(conn, table, column, ddl)


def init_db() -> None:
    """Create all tables from schema.sql, then apply column migrations."""
    conn = connect()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        migrate(conn)
        conn.commit()
    finally:
        conn.close()
