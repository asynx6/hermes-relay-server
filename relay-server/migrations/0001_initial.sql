-- 0001_initial — baseline schema for the Hermes Relay Server.
--
-- This is the first migration: it describes the schema as of the first release
-- that shipped a migration system. Every statement is idempotent
-- (IF NOT EXISTS), so applying it to a database that was created by an earlier
-- release is a no-op that leaves existing data untouched.
--
-- Connection-level pragmas (journal_mode, foreign_keys, busy_timeout) live in
-- app/database.py::connect() — they are per-connection settings, not schema.
--
-- Compatibility note: the types and relations map directly onto MySQL, so a
-- future port only needs different column types and a different driver.

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
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Bumped on key rotation; lets a client detect that its session material
    -- changed without comparing secrets.
    token_epoch   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    created_by  INTEGER NOT NULL REFERENCES agents(id),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- NULL means the group is open; otherwise joining requires this code.
    join_code   TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    agent_id  INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    joined_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- 'admin' members may manage the group; 'member' may only participate.
    role      TEXT    NOT NULL DEFAULT 'member'
              CHECK (role IN ('admin', 'member')),
    PRIMARY KEY (group_id, agent_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_msg_id TEXT    NOT NULL,
    sender_id     INTEGER NOT NULL REFERENCES agents(id),
    recipient_id  INTEGER REFERENCES agents(id),
    group_id      INTEGER REFERENCES groups(id),
    msg_type      TEXT    NOT NULL DEFAULT 'text'
                  CHECK (msg_type IN ('text', 'file', 'task', 'result', 'event')),
    content       TEXT,
    file_id       TEXT    REFERENCES file_logs(file_id),
    task_id       TEXT,
    task_status   TEXT    CHECK (task_status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    reply_to      INTEGER REFERENCES messages(id),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Exactly one of recipient_id / group_id must be set.
    CHECK ((recipient_id IS NULL) <> (group_id IS NULL)),
    UNIQUE (sender_id, client_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages (recipient_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_group     ON messages (group_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_task      ON messages (task_id, id);

-- Delivery acknowledgement: a receiver tells the server it got/read a message.
-- This backs retry logic on the server side and the sender-visible status.
CREATE TABLE IF NOT EXISTS message_receipts (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    status     TEXT    NOT NULL CHECK (status IN ('delivered', 'read')),
    at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (message_id, agent_id, status)
);

-- Registration replay: when the 201 response is lost (network timeout), a retry
-- with the same nonce returns the same secret, so the agent name is not
-- "burned". Expired rows are pruned automatically.
CREATE TABLE IF NOT EXISTS registration_replays (
    nonce      TEXT PRIMARY KEY,
    name       TEXT    NOT NULL,
    secret     TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

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
-- Deduplication looks files up by content hash.
CREATE INDEX IF NOT EXISTS idx_file_logs_sha256 ON file_logs (sha256);

-- ---------------------------------------------------------------------------
-- Token security: scoped secondary tokens, rotation grace, and the audit log
-- ---------------------------------------------------------------------------

-- The primary token (agents.secret_hash) always has full access (scope "*").
-- This table holds additional tokens with restricted permissions — for example
-- a read-only token placed somewhere less secure, or a token limited to a
-- single kind of operation.
CREATE TABLE IF NOT EXISTS agent_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id     TEXT    NOT NULL UNIQUE,
    agent_id     INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    label        TEXT,
    secret_hash  TEXT    NOT NULL UNIQUE,
    scopes       TEXT    NOT NULL,          -- JSON array, e.g. ["messages:read"]
    status       TEXT    NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'revoked')),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at TEXT,
    expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_agent ON agent_tokens (agent_id, status);

-- Key rotation: the previous key's hash stays valid until the grace window
-- expires, so a running client can swap keys without being disconnected.
CREATE TABLE IF NOT EXISTS token_rotation_grace (
    old_hash   TEXT PRIMARY KEY,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Append-only audit log. The dashboard can send as any agent and download every
-- file; without a trail, misuse cannot be traced.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT    NOT NULL,
    actor_type TEXT    NOT NULL CHECK (actor_type IN ('dashboard', 'agent', 'system')),
    action     TEXT    NOT NULL,
    target     TEXT,
    detail     TEXT,
    ip         TEXT,
    result     TEXT    NOT NULL DEFAULT 'ok'
               CHECK (result IN ('ok', 'denied', 'error')),
    at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at DESC);

-- ---------------------------------------------------------------------------
-- Group management: member roles (above) and mentions
-- ---------------------------------------------------------------------------

-- @agent mentions inside a message body; lets a receiver filter for messages
-- that actually address it.
CREATE TABLE IF NOT EXISTS message_mentions (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, agent_id)
);

-- ---------------------------------------------------------------------------
-- Chunked and resumable uploads
-- ---------------------------------------------------------------------------

-- One upload session. Chunks are written to .staging/<upload_id>/chunk_<n>,
-- then assembled and verified against the total SHA-256 before an atomic commit.
CREATE TABLE IF NOT EXISTS upload_sessions (
    upload_id     TEXT    PRIMARY KEY,
    agent_id      INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    original_name TEXT    NOT NULL,
    mime_type     TEXT,
    total_size    INTEGER NOT NULL,
    chunk_size    INTEGER NOT NULL,
    total_chunks  INTEGER NOT NULL,
    sha256        TEXT    NOT NULL,
    target_type   TEXT    NOT NULL,
    target_id     TEXT    NOT NULL,
    target_name   TEXT    NOT NULL,
    client_msg_id TEXT    NOT NULL,
    caption       TEXT,
    received      TEXT    NOT NULL DEFAULT '',   -- received chunk indexes, comma separated
    status        TEXT    NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'completed', 'aborted')),
    file_id       TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_agent
    ON upload_sessions (agent_id, status);
