-- Skema database relay server (SQLite, mode WAL).
-- Kompatibel dengan MySQL: cukup ganti tipe kolom dan driver saat migrasi.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    secret_hash   TEXT    NOT NULL UNIQUE,
    description   TEXT,
    metadata      TEXT,
    status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended', 'revoked')),
    last_seen_at  TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    created_by  INTEGER NOT NULL REFERENCES agents(id),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    agent_id  INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    joined_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (group_id, agent_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_msg_id TEXT    NOT NULL,
    sender_id     INTEGER NOT NULL REFERENCES agents(id),
    recipient_id  INTEGER REFERENCES agents(id),
    group_id      INTEGER REFERENCES groups(id),
    msg_type      TEXT    NOT NULL DEFAULT 'text' CHECK (msg_type IN ('text', 'file')),
    content       TEXT,
    file_id       TEXT    REFERENCES file_logs(file_id),
    reply_to      INTEGER REFERENCES messages(id),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK ((recipient_id IS NULL) <> (group_id IS NULL)),
    UNIQUE (sender_id, client_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages (recipient_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_group     ON messages (group_id, id);

CREATE TABLE IF NOT EXISTS file_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id        TEXT    NOT NULL UNIQUE,
    original_name  TEXT    NOT NULL,
    stored_name    TEXT    NOT NULL UNIQUE,
    mime_type      TEXT,
    size_bytes     INTEGER NOT NULL,
    sha256         TEXT    NOT NULL,
    uploader_id    INTEGER NOT NULL REFERENCES agents(id),
    recipient_id   INTEGER REFERENCES agents(id),
    group_id       INTEGER REFERENCES groups(id),
    download_count INTEGER NOT NULL DEFAULT 0,
    uploaded_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at     TEXT,
    CHECK ((recipient_id IS NULL) <> (group_id IS NULL))
);
