"""Versioned, transactional database migrations.

Why this exists
---------------
`CREATE TABLE IF NOT EXISTS` can never change a table that already exists, so a
schema change used to mean deleting `data/relay.db` and losing every message,
file and agent registration. This module replaces that with an ordered chain of
migration files that are applied once and recorded, so an existing database is
upgraded in place.

Layout
------
`relay-server/migrations/` holds the chain, ordered by numeric prefix:

    0001_initial.sql          baseline schema
    0002_legacy_columns.py    heals databases from before the migration system
    0003_add_something.sql    future change

A migration is either:

* `.sql`  — plain statements, executed one by one inside a single transaction.
            Statements must not contain a `;` inside a trigger body; use a
            Python migration if you need a trigger (the splitter understands
            string literals and comments, but not BEGIN...END blocks).
* `.py`   — must expose `up(conn)`. Gives full control for conditional DDL or
            data backfills. By default it runs inside the migration
            transaction; set `MANAGES_TRANSACTION = True` at module level if it
            needs to manage its own (for example a table rebuild that requires
            `PRAGMA foreign_keys = OFF`, which SQLite refuses to change inside a
            transaction).

Guarantees
----------
* Each migration is applied at most once, recorded in `schema_migrations` with
  its file checksum.
* Each migration is atomic: a failure rolls the whole migration back, so the
  database is never left half-migrated.
* A migration file that was edited after being applied is reported as *drifted*
  and blocks further migration until the operator resolves it, because the
  recorded state no longer matches the file.
* `backup()` takes a consistent snapshot with the SQLite online backup API,
  which is safe while the server is running.

Downgrades are intentionally not supported: SQLite cannot drop or retype columns
portably, so the rollback path is the pre-migration backup.
"""
import hashlib
import importlib.util
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    duration_ms INTEGER
)
"""

_NAME_RE = re.compile(r"^(\d{4,})_([A-Za-z0-9_]+)\.(sql|py)$")


class MigrationError(Exception):
    """Raised when the chain cannot be applied safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    kind: str
    path: Path
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover(directory: Path | None = None) -> list[Migration]:
    """Return the migration chain, ordered by version.

    Raises MigrationError on duplicate versions, which would make the order
    ambiguous.
    """
    root = Path(directory or MIGRATIONS_DIR)
    if not root.is_dir():
        raise MigrationError(f"Migration directory not found: {root}")

    found: dict[int, Migration] = {}
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        match = _NAME_RE.match(path.name)
        if not match:
            continue  # README, .gitkeep, editor leftovers
        version, name, kind = int(match.group(1)), match.group(2), match.group(3)
        if version in found:
            other = found[version].path.name
            raise MigrationError(
                f"Duplicate migration version {version:04d}: {other} and {path.name}"
            )
        found[version] = Migration(
            version=version,
            name=name,
            kind=kind,
            path=path,
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return [found[v] for v in sorted(found)]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        is not None
    )


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create the ledger. Only called when something is about to be written, so
    that read-only commands (status, dry-run) never modify the database."""
    conn.execute(_TABLE_DDL)
    conn.commit()


def applied(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """Applied migrations keyed by version.

    Read-only: returns an empty mapping when the ledger does not exist yet, so
    inspecting a database never creates anything.
    """
    if not _table_exists(conn):
        return {}
    rows = conn.execute(
        "SELECT version, name, kind, checksum, applied_at, duration_ms "
        "FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {row["version"]: row for row in rows}


def pending(conn: sqlite3.Connection, directory: Path | None = None) -> list[Migration]:
    """Migrations present on disk but not yet recorded as applied."""
    done = applied(conn)
    return [m for m in discover(directory) if m.version not in done]


def drifted(conn: sqlite3.Connection, directory: Path | None = None) -> list[tuple[Migration, str]]:
    """Applied migrations whose file checksum no longer matches.

    Returns (migration, recorded_checksum) pairs. This means the file was edited
    after it was applied, so the database no longer provably matches the chain.
    """
    done = applied(conn)
    out: list[tuple[Migration, str]] = []
    for migration in discover(directory):
        row = done.get(migration.version)
        if row is not None and row["checksum"] != migration.checksum:
            out.append((migration, row["checksum"]))
    return out


def orphaned(conn: sqlite3.Connection, directory: Path | None = None) -> list[sqlite3.Row]:
    """Recorded versions with no matching file (a migration was deleted)."""
    on_disk = {m.version for m in discover(directory)}
    return [row for version, row in applied(conn).items() if version not in on_disk]


# ---------------------------------------------------------------------------
# Statement splitting
# ---------------------------------------------------------------------------


def split_statements(sql: str) -> list[str]:
    """Split a SQL script on `;`, ignoring separators inside strings/comments.

    Handles single-quoted literals (including '' escapes), double-quoted
    identifiers, `--` line comments and `/* */` block comments. Statements that
    contain nothing but comments and whitespace are dropped.

    Not handled: `;` inside a trigger BEGIN...END body. Write those as a Python
    migration.
    """
    statements: list[str] = []
    buf: list[str] = []
    has_code = False
    i, n = 0, len(sql)
    state = "code"  # code | single | double | line | block

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if state == "line":
            buf.append(ch)
            if ch == "\n":
                state = "code"
            i += 1
            continue
        if state == "block":
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                state = "code"
                continue
            i += 1
            continue
        if state == "single":
            buf.append(ch)
            if ch == "'":
                if nxt == "'":  # escaped quote inside a literal
                    buf.append(nxt)
                    i += 2
                    continue
                state = "code"
            i += 1
            continue
        if state == "double":
            buf.append(ch)
            if ch == '"':
                state = "code"
            i += 1
            continue

        # state == "code"
        if ch == "-" and nxt == "-":
            state = "line"
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            state = "block"
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            state = "single"
            has_code = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            state = "double"
            has_code = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            if has_code:
                statements.append("".join(buf).strip())
            buf = []
            has_code = False
            i += 1
            continue
        if not ch.isspace():
            has_code = True
        buf.append(ch)
        i += 1

    if has_code:
        statements.append("".join(buf).strip())
    return statements


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _load_python_migration(path: Path):
    spec = importlib.util.spec_from_file_location(f"hermes_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"Cannot load migration module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "up"):
        raise MigrationError(f"Python migration {path.name} has no up(conn) function")
    return module


def _apply_body(conn: sqlite3.Connection, migration: Migration) -> None:
    if migration.kind == "sql":
        for statement in split_statements(migration.path.read_text(encoding="utf-8")):
            conn.execute(statement)
        return
    module = _load_python_migration(migration.path)
    module.up(conn)


def apply_one(conn: sqlite3.Connection, migration: Migration) -> int:
    """Apply a single migration atomically. Returns the duration in milliseconds."""
    started = datetime.now(timezone.utc)
    manages = False
    if migration.kind == "py":
        manages = bool(getattr(_load_python_migration(migration.path), "MANAGES_TRANSACTION", False))

    if not manages:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _apply_body(conn, migration)
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations "
            "(version, name, kind, checksum, duration_ms) VALUES (?, ?, ?, ?, ?)",
            (migration.version, migration.name, migration.kind, migration.checksum, duration_ms),
        )
        if not manages:
            conn.commit()
        else:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return duration_ms


def apply(
    conn: sqlite3.Connection,
    migrations: list[Migration],
    *,
    log=None,
) -> list[Migration]:
    """Apply migrations in order. Stops at the first failure, leaving earlier
    migrations applied and the failing one rolled back."""
    if migrations:
        ensure_table(conn)
    done: list[Migration] = []
    for migration in migrations:
        if log:
            log(f"applying {migration.label} ...")
        duration_ms = apply_one(conn, migration)
        if log:
            log(f"  ok ({duration_ms} ms)")
        done.append(migration)
    return done


def run(conn: sqlite3.Connection, *, log=None) -> list[Migration]:
    """Bring the database up to date. Returns the migrations that were applied.

    Raises MigrationError when a recorded migration has drifted. Read-only when
    there is nothing to do.
    """
    problems = drifted(conn)
    if problems:
        detail = ", ".join(f"{m.label} (recorded {c[:8]})" for m, c in problems)
        raise MigrationError(
            "These migrations were modified after being applied, so the database "
            f"can no longer be trusted to match the chain: {detail}. Restore the "
            "original file, or roll back to a pre-migration backup."
        )
    return apply(conn, pending(conn), log=log)


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def status(conn: sqlite3.Connection) -> dict:
    """Snapshot of the chain: applied, pending, drifted and orphaned entries."""
    done = applied(conn)
    chain = discover()
    todo = [m for m in chain if m.version not in done]
    return {
        "applied": [done[m.version] for m in chain if m.version in done],
        "pending": todo,
        "drifted": drifted(conn),
        "orphaned": orphaned(conn),
        "current": max(done) if done else 0,
        "target": chain[-1].version if chain else 0,
    }


def dump_schema() -> str:
    """Render the schema produced by the whole chain.

    Builds an in-memory database, applies every migration to it and prints the
    resulting DDL. This is generated on demand, so there is no committed schema
    file that can drift away from the migrations.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        for migration in discover():
            _apply_body(conn, migration)
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "AND name <> 'schema_migrations' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, rowid"
        ).fetchall()
    finally:
        conn.close()
    parts = ["-- Generated by `python scripts/migrate.py schema` — do not edit."]
    for row in rows:
        parts.append(f"{row['sql'].strip()};")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def default_backup_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return settings.data_dir / "backups" / f"relay-{stamp}.db"


def backup(dest: Path | None = None) -> Path:
    """Take a consistent snapshot of the database using the SQLite backup API.

    Safe while the server is running (unlike copying the file, which can miss
    data sitting in the WAL). Returns the path written.
    """
    target = Path(dest) if dest else default_backup_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(settings.db_path)
    try:
        dest_conn = sqlite3.connect(target)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()
    return target
