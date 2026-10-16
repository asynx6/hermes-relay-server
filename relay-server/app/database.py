"""SQLite access: one connection per operation, WAL, foreign keys enabled.

Schema creation and upgrades are handled by `app/migrations.py`; this module
only owns connections and the startup check.
"""
import sqlite3
import time
from pathlib import Path

from . import migrations
from .config import settings


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


def init_db(log=None) -> list[migrations.Migration]:
    """Create or upgrade the schema. Returns the migrations that were applied.

    Safe to call on every start: already-applied migrations are skipped, and a
    database created by an older release is upgraded in place without losing
    data.
    """
    conn = connect()
    try:
        return migrations.run(conn, log=log)
    finally:
        conn.close()


def pending_migrations() -> list[migrations.Migration]:
    """Migrations that would be applied on the next `init_db()`."""
    conn = connect()
    try:
        return migrations.pending(conn)
    finally:
        conn.close()


def prepare_database(log=print) -> None:
    """Startup guard: migrate automatically, or refuse to start if disabled.

    With `AUTO_MIGRATE=false` an operator gets an explicit failure listing the
    pending migrations instead of the server silently running against a schema
    it does not expect.
    """
    todo = pending_migrations()
    if not todo:
        init_db(log=log)
        return

    if not settings.auto_migrate:
        listing = "\n".join(f"  - {m.label}" for m in todo)
        raise SystemExit(
            "REFUSING TO START: the database schema is out of date and "
            "AUTO_MIGRATE is disabled.\n"
            f"Pending migrations:\n{listing}\n"
            "Apply them with:\n"
            "  python scripts/migrate.py up        # takes a backup first"
        )

    log(f"Applying {len(todo)} pending database migration(s)...")
    init_db(log=log)


def backup_db(dest: Path | None = None) -> Path:
    """Convenience re-export of the migration backup helper."""
    return migrations.backup(dest)
