"""Long Memory: SQLite lokal (conversations, files_history, sync_state).

Satu koneksi untuk satu proses, mode WAL, busy_timeout supaya tidak
langsung gagal saat ada akses bersamaan.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS conversations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_message_id INTEGER UNIQUE,
    client_msg_id     TEXT    UNIQUE,
    direction         TEXT    NOT NULL CHECK (direction IN ('in', 'out')),
    scope             TEXT    NOT NULL CHECK (scope IN ('direct', 'group')),
    peer              TEXT    NOT NULL,
    peer_id           TEXT,
    sender            TEXT    NOT NULL,
    msg_type          TEXT    NOT NULL DEFAULT 'text',
    content           TEXT,
    file_id           TEXT,
    task_id           TEXT,
    task_status       TEXT,
    status            TEXT    NOT NULL DEFAULT 'sent'
                      CHECK (status IN ('pending', 'sent', 'failed', 'received')),
    retry_count       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_peer   ON conversations (scope, peer, created_at);
CREATE INDEX IF NOT EXISTS idx_conv_status ON conversations (status);

CREATE TABLE IF NOT EXISTS files_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       TEXT,
    direction     TEXT    NOT NULL CHECK (direction IN ('upload', 'download')),
    scope         TEXT    NOT NULL CHECK (scope IN ('direct', 'group')),
    peer          TEXT    NOT NULL,
    original_name TEXT    NOT NULL,
    local_path    TEXT,
    size_bytes    INTEGER,
    sha256        TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'done', 'failed')),
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_file_id ON files_history (file_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MAX_SEND_RETRIES = 10


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Memory:
    def __init__(self, db_path: str, agent_name: str):
        self.agent_name = agent_name
        self.server_epoch = None  # diisi dari respons fetch-messages
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- cursor polling ----

    def get_last_id(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE key = 'last_message_id'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def set_last_id(self, value: int) -> None:
        self.conn.execute(
            "INSERT INTO sync_state (key, value) VALUES ('last_message_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
        )
        self.conn.commit()

    # ---- pesan masuk ----

    def save_incoming(self, messages: list[dict], next_id: int) -> list[dict]:
        """Simpan pesan masuk lalu majukan cursor, dalam satu transaksi.

        - `remote_message_id UNIQUE` men-dedupe kalau server mengirim ulang
          pesan yang sama; hanya pesan yang BENAR-BENAR baru (rowcount > 0)
          yang dikembalikan, supaya handler tidak jalan dua kali.
        - Cursor hanya naik (MAX), tidak pernah mundur, dan hanya setelah
          simpan sukses.
        """
        new_messages: list[dict] = []
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for msg in messages:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO conversations
                         (remote_message_id, direction, scope, peer, peer_id, sender,
                          msg_type, content, file_id, task_id, task_status, status, created_at)
                       VALUES (?, 'in', ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'received', ?)""",
                    (
                        msg["id"],
                        msg["scope"],
                        msg["group"] or msg["from"],
                        msg["from"],
                        msg["msg_type"],
                        msg.get("content"),
                        msg["file"]["file_id"] if msg.get("file") else None,
                        msg.get("task_id"),
                        msg.get("task_status"),
                        msg["created_at"],
                    ),
                )
                if cur.rowcount:
                    new_messages.append(msg)
            # MAX() supaya cursor tidak pernah mundur (mis. server mengirim
            # ulang batch lama karena timeout di sisi kita).
            self.conn.execute(
                "INSERT INTO sync_state (key, value) VALUES ('last_message_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = "
                "MAX(CAST(value AS INTEGER), excluded.value)",
                (str(next_id),),
            )
            self.conn.execute(
                "INSERT INTO sync_state (key, value) VALUES ('server_epoch', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.server_epoch or "",),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return new_messages

    def server_epoch_matches(self, epoch: str | None) -> bool:
        """False kalau server_epoch berbeda dari yang terakhir tercatat
        (tanda database server di-reset — cursor lama tidak lagi valid)."""
        if not epoch:
            return True
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE key = 'server_epoch'"
        ).fetchone()
        if row is None or not row["value"]:
            return True  # belum pernah mencatat; anggap cocok
        return row["value"] == epoch

    def remember_server_epoch(self, epoch: str | None) -> None:
        self.conn.execute(
            "INSERT INTO sync_state (key, value) VALUES ('server_epoch', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (epoch or "",),
        )
        self.conn.commit()

    # ---- unduhan pending (retry tiap siklus) ----

    def pending_downloads(self) -> list[sqlite3.Row]:
        """File yang gagal/tertunda diunduh; dicoba lagi tiap siklus polling."""
        return self.conn.execute(
            "SELECT * FROM files_history WHERE direction = 'download' "
            "AND status = 'pending' ORDER BY id LIMIT 20"
        ).fetchall()

    # ---- outbox ----

    def save_outgoing(
        self, scope: str, peer: str, peer_id: str, content: str,
        client_msg_id: str, msg_type: str = "text", file_id: str | None = None,
    ) -> int:
        """Catat pesan keluar sebagai pending (siap dikirim di siklus berikutnya)."""
        cur = self.conn.execute(
            """INSERT INTO conversations
                 (client_msg_id, direction, scope, peer, peer_id, sender, msg_type,
                  content, file_id, status, created_at)
               VALUES (?, 'out', ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                client_msg_id, scope, peer, peer_id, self.agent_name, msg_type,
                content, file_id, now_iso(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def pending_outbox(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM conversations WHERE direction = 'out' AND status = 'pending' "
            "ORDER BY id LIMIT 50"
        ).fetchall()

    def mark_sent(self, row_id: int, remote_message_id: int | None) -> None:
        self.conn.execute(
            "UPDATE conversations SET status = 'sent', remote_message_id = ? WHERE id = ?",
            (remote_message_id, row_id),
        )
        self.conn.commit()

    def mark_failed(self, row_id: int) -> None:
        """Naikkan retry_count; lewat batas -> failed permanen."""
        self.conn.execute(
            "UPDATE conversations SET retry_count = retry_count + 1, "
            "status = CASE WHEN retry_count + 1 >= ? THEN 'failed' ELSE 'pending' END "
            "WHERE id = ?",
            (MAX_SEND_RETRIES, row_id),
        )
        self.conn.commit()

    # ---- riwayat file ----

    def record_file(
        self, file_id: str | None, direction: str, scope: str, peer: str,
        original_name: str, size_bytes: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO files_history
                 (file_id, direction, scope, peer, original_name, size_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (file_id, direction, scope, peer, original_name, size_bytes, now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_file_status(
        self, row_id: int, status: str, local_path: str | None = None,
        sha256: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE files_history SET status = ?, local_path = COALESCE(?, local_path), "
            "sha256 = COALESCE(?, sha256) WHERE id = ?",
            (status, local_path, sha256, row_id),
        )
        self.conn.commit()

    def recent_conversations(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
