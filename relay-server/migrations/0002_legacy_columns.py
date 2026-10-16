"""0002_legacy_columns — heal databases created before the migration system.

Background: the first releases built the schema from `schema.sql` and then added
a few columns on every startup with `ALTER TABLE`. Those columns are now declared
inline in migration 0001, so a fresh database already has them.

This migration exists only to bring an older database to the same shape. It is
written in Python rather than SQL because SQLite has no
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so the check has to be conditional.

Applying it to a database that already has the columns is a no-op, and it never
touches existing rows — only the table definitions.
"""

# (table, column, column definition)
COLUMNS = [
    ("group_members", "role", "role TEXT NOT NULL DEFAULT 'member'"),
    ("groups", "join_code", "join_code TEXT"),
    ("agents", "token_epoch", "token_epoch INTEGER NOT NULL DEFAULT 0"),
]


def up(conn) -> None:
    """Add any of the legacy columns that are still missing."""
    for table, column, ddl in COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            # Table does not exist at all; migration 0001 creates it with the
            # column already in place.
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
