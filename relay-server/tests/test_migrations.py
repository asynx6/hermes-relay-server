"""Tests for the versioned migration system.

The point of this feature is that a schema change no longer means deleting the
database. These tests pin that down: a fresh database reaches the latest
version, an old database is upgraded in place with its rows intact, migrations
are recorded and applied exactly once, a modified migration file is rejected,
and a failing migration rolls back cleanly.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app import migrations  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import connect, init_db, prepare_database  # noqa: E402

# Schema as it looked before the migration system existed: no
# group_members.role, no groups.join_code, no agents.token_epoch, and none of
# the tables added by later releases.
LEGACY_SCHEMA = """
CREATE TABLE agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE, secret_hash TEXT NOT NULL UNIQUE,
    description TEXT, metadata TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','suspended','revoked')),
    last_seen_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE, description TEXT,
    created_by INTEGER NOT NULL REFERENCES agents(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE group_members (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (group_id, agent_id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_msg_id TEXT NOT NULL,
    sender_id INTEGER NOT NULL REFERENCES agents(id),
    recipient_id INTEGER REFERENCES agents(id), group_id INTEGER REFERENCES groups(id),
    msg_type TEXT NOT NULL DEFAULT 'text',
    content TEXT, file_id TEXT, task_id TEXT, task_status TEXT, reply_to INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK ((recipient_id IS NULL) <> (group_id IS NULL)),
    UNIQUE (sender_id, client_msg_id)
);
"""


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the whole app at a throwaway database file.

    `settings.data_dir` is a property derived from `db_path`, so patching
    `db_path` moves backups and the dashboard files along with it.
    """
    db = tmp_path / "relay.db"
    monkeypatch.setattr(settings, "db_path", db)
    return db


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def _columns(db: Path, table: str) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _make_legacy_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO agents (agent_id, name, secret_hash, description) "
            "VALUES ('ag_aaa','hermes-alpha','h1','existing agent')"
        )
        conn.execute(
            "INSERT INTO agents (agent_id, name, secret_hash) VALUES ('ag_bbb','hermes-beta','h2')"
        )
        conn.execute(
            "INSERT INTO groups (group_id, name, created_by) VALUES ('gr_xxx','old-team',1)"
        )
        conn.execute("INSERT INTO group_members (group_id, agent_id) VALUES (1,1)")
        conn.execute(
            "INSERT INTO messages (client_msg_id, sender_id, recipient_id, content) "
            "VALUES ('c1',1,2,'legacy message')"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fresh database
# ---------------------------------------------------------------------------


def test_fresh_database_reaches_latest_version(temp_db):
    applied = init_db()

    assert [m.version for m in applied] == [1, 2]
    conn = connect()
    try:
        info = migrations.status(conn)
    finally:
        conn.close()
    assert info["current"] == info["target"] == 2
    assert info["pending"] == []
    assert info["drifted"] == []

    tables = _tables(temp_db)
    assert {
        "agents",
        "groups",
        "group_members",
        "messages",
        "message_receipts",
        "registration_replays",
        "file_logs",
        "agent_tokens",
        "token_rotation_grace",
        "audit_log",
        "message_mentions",
        "upload_sessions",
        "schema_migrations",
    } <= tables

    # Columns that older releases added through ad-hoc ALTERs are declared
    # inline by the baseline migration.
    assert "role" in _columns(temp_db, "group_members")
    assert "join_code" in _columns(temp_db, "groups")
    assert "token_epoch" in _columns(temp_db, "agents")


def test_running_twice_is_a_noop(temp_db):
    init_db()
    second = init_db()
    assert second == [], "already-applied migrations must not run again"


# ---------------------------------------------------------------------------
# Upgrading an existing database
# ---------------------------------------------------------------------------


def test_legacy_database_upgraded_without_data_loss(temp_db):
    _make_legacy_db(temp_db)

    applied = init_db()
    assert [m.version for m in applied] == [1, 2]

    conn = sqlite3.connect(temp_db)
    try:
        # Existing rows survive, values included.
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT description FROM agents WHERE name = 'hermes-alpha'"
            ).fetchone()[0]
            == "existing agent"
        )
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert (
            conn.execute("SELECT content FROM messages").fetchone()[0]
            == "legacy message"
        )
        # The new column exists and is backfilled for existing rows.
        assert (
            conn.execute("SELECT role FROM group_members").fetchone()[0] == "member"
        )
    finally:
        conn.close()

    # Columns and tables that the legacy database lacked are now present.
    assert "role" in _columns(temp_db, "group_members")
    assert "join_code" in _columns(temp_db, "groups")
    assert "token_epoch" in _columns(temp_db, "agents")
    assert "upload_sessions" in _tables(temp_db)
    assert "agent_tokens" in _tables(temp_db)


def test_partial_upgrade_reports_pending(temp_db):
    """Stopping at version 1 leaves version 2 pending and reported as such."""
    conn = connect()
    try:
        migrations.apply(conn, [m for m in migrations.discover() if m.version == 1])
        todo = migrations.pending(conn)
        assert migrations.applied(conn).keys() == {1}
    finally:
        conn.close()

    assert [m.version for m in todo] == [2]
    # 0001 already declares the modern schema, so the columns exist even at
    # version 1; 0002 only exists to heal tables created before the ledger.
    assert "role" in _columns(temp_db, "group_members")

    init_db()
    conn = connect()
    try:
        assert migrations.pending(conn) == []
    finally:
        conn.close()


def test_legacy_columns_migration_is_a_noop_on_a_fresh_database(temp_db):
    """Applying 0002 twice must not fail (no ALTER on an existing column)."""
    init_db()
    conn = connect()
    try:
        # Re-running the body directly is the same code path a re-apply would take.
        module = migrations._load_python_migration(
            Path(migrations.MIGRATIONS_DIR) / "0002_legacy_columns.py"
        )
        module.up(conn)
        conn.commit()
    finally:
        conn.close()
    assert "role" in _columns(temp_db, "group_members")


# ---------------------------------------------------------------------------
# Safety rails
# ---------------------------------------------------------------------------


def test_modified_migration_is_rejected(temp_db):
    init_db()

    conn = connect()
    try:
        conn.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1"
        )
        conn.commit()
        with pytest.raises(migrations.MigrationError, match="modified after being applied"):
            migrations.run(conn)
        assert [m.version for m, _ in migrations.drifted(conn)] == [1]
    finally:
        conn.close()


def test_failing_migration_rolls_back(temp_db, tmp_path, monkeypatch):
    """A migration that fails halfway must leave no partial changes behind."""
    broken = tmp_path / "migrations"
    broken.mkdir()
    (broken / "0001_ok.sql").write_text("CREATE TABLE alpha (id INTEGER);\n", encoding="utf-8")
    (broken / "0002_broken.sql").write_text(
        "CREATE TABLE beta (id INTEGER);\n"
        "INSERT INTO does_not_exist VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", broken)

    with pytest.raises(sqlite3.OperationalError):
        init_db()

    tables = _tables(temp_db)
    assert "alpha" in tables, "the migration before the failure stays applied"
    assert "beta" not in tables, "the failing migration must be rolled back"

    conn = connect()
    try:
        assert sorted(migrations.applied(conn)) == [1]
        assert [m.version for m in migrations.pending(conn)] == [2]
    finally:
        conn.close()


def test_duplicate_versions_are_rejected(tmp_path, monkeypatch):
    dupes = tmp_path / "migrations"
    dupes.mkdir()
    (dupes / "0001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (dupes / "0001_second.sql").write_text("SELECT 2;\n", encoding="utf-8")
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", dupes)

    with pytest.raises(migrations.MigrationError, match="Duplicate migration version"):
        migrations.discover()


def test_auto_migrate_disabled_refuses_to_start(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "auto_migrate", False)
    with pytest.raises(SystemExit, match="REFUSING TO START"):
        prepare_database(log=lambda _m: None)

    # The check is read-only: nothing was created, not even the ledger.
    assert _tables(temp_db) == set()

    monkeypatch.setattr(settings, "auto_migrate", True)
    prepare_database(log=lambda _m: None)
    assert "agents" in _tables(temp_db)


def test_inspection_commands_are_read_only(temp_db):
    """status / pending / drifted must not modify the database."""
    conn = connect()
    try:
        assert migrations.status(conn)["pending"]
        assert migrations.pending(conn)
        assert migrations.applied(conn) == {}
        assert migrations.drifted(conn) == []
    finally:
        conn.close()

    assert _tables(temp_db) == set(), "inspection must not create the ledger table"


def test_backup_is_a_usable_snapshot(temp_db, tmp_path):
    _make_legacy_db(temp_db)
    init_db()

    dest = migrations.backup(tmp_path / "snapshot.db")
    assert dest.is_file()

    conn = sqlite3.connect(dest)
    try:
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def test_dump_schema_covers_the_whole_chain():
    ddl = migrations.dump_schema()
    for table in ("agents", "group_members", "upload_sessions", "audit_log"):
        assert f"CREATE TABLE {table}" in ddl or f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert "role" in ddl and "join_code" in ddl and "token_epoch" in ddl
    assert "schema_migrations" not in ddl, "the ledger itself is not part of the schema"


@pytest.mark.parametrize(
    "script, expected",
    [
        ("CREATE TABLE a (id INT); CREATE TABLE b (id INT);", 2),
        ("-- only a comment\n", 0),
        ("CREATE TABLE a (id INT) -- trailing comment\n;", 1),
        ("INSERT INTO a VALUES (';not a separator');", 1),
        ("INSERT INTO a VALUES ('it''s; fine');", 1),
        ('CREATE TABLE "we;ird" (id INT);', 1),
        ("/* block ; comment */ CREATE TABLE a (id INT);", 1),
    ],
)
def test_split_statements(script, expected):
    assert len(migrations.split_statements(script)) == expected


def test_split_statements_ignores_semicolons_in_strings():
    parts = migrations.split_statements("INSERT INTO a VALUES ('x;y');")
    assert parts == ["INSERT INTO a VALUES ('x;y')"]
