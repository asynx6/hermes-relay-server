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
    msg_type      TEXT    NOT NULL DEFAULT 'text'
                  CHECK (msg_type IN ('text', 'file', 'task', 'result', 'event')),
    content       TEXT,
    file_id       TEXT    REFERENCES file_logs(file_id),
    task_id       TEXT,
    task_status   TEXT    CHECK (task_status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    reply_to      INTEGER REFERENCES messages(id),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK ((recipient_id IS NULL) <> (group_id IS NULL)),
    UNIQUE (sender_id, client_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages (recipient_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_group     ON messages (group_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_task      ON messages (task_id, id);

-- Bukti penerimaan (ack): agent memberi tahu server bahwa pesan sudah
-- diterima/dibaca. Menjadi dasar retry di sisi server dan status pengiriman.
CREATE TABLE IF NOT EXISTS message_receipts (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    status     TEXT    NOT NULL CHECK (status IN ('delivered', 'read')),
    at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (message_id, agent_id, status)
);

-- Replay registrasi: kalau respons 201 hilang (timeout jaringan), retry
-- dengan nonce yang sama mengembalikan secret yang sama, jadi nama agent
-- tidak "terbakar". Baris kedaluwarsa dibuang otomatis.
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
CREATE INDEX IF NOT EXISTS idx_file_logs_sha256 ON file_logs (sha256);

-- ---------------------------------------------------------------------------
-- Keamanan token: token sekunder ber-scope, grace rotasi, dan audit log
-- ---------------------------------------------------------------------------

-- Token utama agent (agents.secret_hash) selalu ber-akses penuh (scope "*").
-- Tabel ini menampung token tambahan yang izinnya dibatasi — mis. token
-- baca-saja untuk dipasang di tempat yang kurang aman, atau token yang hanya
-- boleh menyentuh satu jenis operasi.
CREATE TABLE IF NOT EXISTS agent_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id     TEXT    NOT NULL UNIQUE,
    agent_id     INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    label        TEXT,
    secret_hash  TEXT    NOT NULL UNIQUE,
    scopes       TEXT    NOT NULL,          -- JSON array, mis. ["messages:read"]
    status       TEXT    NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'revoked')),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at TEXT,
    expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_agent ON agent_tokens (agent_id, status);

-- Rotasi token: hash kunci lama tetap diterima sampai jendela grace habis,
-- supaya client yang sedang berjalan bisa menukar kunci tanpa putus.
CREATE TABLE IF NOT EXISTS token_rotation_grace (
    old_hash   TEXT PRIMARY KEY,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Audit log append-only. Dashboard bisa mengirim sebagai agent mana pun dan
-- mengunduh semua file; tanpa jejak, penyalahgunaan tidak bisa ditelusuri.
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
-- Kelola grup lanjutan: role anggota dan mention
-- ---------------------------------------------------------------------------

-- Mention @agent pada isi pesan; memudahkan penerima menyaring pesan yang
-- benar-benar menyebut dirinya.
CREATE TABLE IF NOT EXISTS message_mentions (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, agent_id)
);

-- ---------------------------------------------------------------------------
-- Unggahan berpotong (chunked) dan resumable
-- ---------------------------------------------------------------------------

-- Satu sesi unggah. Chunk ditulis ke .staging/<upload_id>/chunk_<n>, lalu
-- dirakit dan diverifikasi SHA-256 total sebelum commit atomik.
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
    received      TEXT    NOT NULL DEFAULT '',   -- index chunk diterima, dipisah koma
    status        TEXT    NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'completed', 'aborted')),
    file_id       TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_agent
    ON upload_sessions (agent_id, status);
