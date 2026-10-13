"""Logika pesan: idempotency, resolusi target, query fetch, keanggotaan grup."""
import secrets
import sqlite3

from ..config import settings
from ..security import api_error


def new_agent_id(conn: sqlite3.Connection) -> str:
    while True:
        candidate = "ag_" + secrets.token_hex(5)
        exists = conn.execute(
            "SELECT 1 FROM agents WHERE agent_id = ?", (candidate,)
        ).fetchone()
        if not exists:
            return candidate


def resolve_direct_target(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM agents WHERE name = ? COLLATE NOCASE AND status = 'active'",
        (name,),
    ).fetchone()
    if row is None:
        raise api_error(404, "NOT_FOUND", f"Agent '{name}' tidak ditemukan.")
    return row


def resolve_group_target(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM groups WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row is None:
        raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
    return row


def ensure_group_member(conn: sqlite3.Connection, group_id: int, agent_row_id: int) -> None:
    row = conn.execute(
        "SELECT joined_at FROM group_members WHERE group_id = ? AND agent_id = ?",
        (group_id, agent_row_id),
    ).fetchone()
    if row is None:
        raise api_error(403, "FORBIDDEN", "Pengirim bukan anggota grup ini.")


def find_by_client_msg_id(
    conn: sqlite3.Connection, sender_row_id: int, client_msg_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT m.*, s.name AS sender_name,
                  r.name AS recipient_name, g.name AS group_name
           FROM messages m
           JOIN agents s ON s.id = m.sender_id
           LEFT JOIN agents r ON r.id = m.recipient_id
           LEFT JOIN groups g ON g.id = m.group_id
           WHERE m.sender_id = ? AND m.client_msg_id = ?""",
        (sender_row_id, client_msg_id),
    ).fetchone()


def validate_reply_to(
    conn: sqlite3.Connection, reply_to: int, agent_row_id: int
) -> None:
    row = conn.execute(
        """SELECT m.sender_id, m.recipient_id, m.group_id,
                  EXISTS (SELECT 1 FROM group_members gm
                          WHERE gm.group_id = m.group_id AND gm.agent_id = ?) AS in_group
           FROM messages m WHERE m.id = ?""",
        (agent_row_id, reply_to),
    ).fetchone()
    if row is None:
        raise api_error(404, "NOT_FOUND", f"Pesan #{reply_to} tidak ditemukan.")
    visible = (
        row["sender_id"] == agent_row_id
        or row["recipient_id"] == agent_row_id
        or (row["group_id"] is not None and row["in_group"])
    )
    if not visible:
        raise api_error(404, "NOT_FOUND", f"Pesan #{reply_to} tidak ditemukan.")


def store_message(
    conn: sqlite3.Connection,
    *,
    sender_row_id: int,
    client_msg_id: str,
    recipient_row_id: int | None,
    group_id: int | None,
    content: str,
    reply_to: int | None,
) -> sqlite3.Row:
    cur = conn.execute(
        """INSERT INTO messages
             (client_msg_id, sender_id, recipient_id, group_id, msg_type, content, reply_to)
           VALUES (?, ?, ?, ?, 'text', ?, ?)""",
        (client_msg_id, sender_row_id, recipient_row_id, group_id, content, reply_to),
    )
    return conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()


def fetch_messages(
    conn: sqlite3.Connection, agent_row_id: int, since_id: int, limit: int
) -> tuple[list[dict], int, bool]:
    """Ambil pesan untuk agent ini. Cursor since_id naik monotonik.

    Filter keanggotaan grup memakai joined_at: anggota baru tidak melihat
    pesan yang terkirim sebelum ia join.
    """
    rows = conn.execute(
        """SELECT m.id, m.msg_type, m.content, m.file_id, m.created_at,
                  m.reply_to, s.name AS sender_name, g.name AS group_name,
                  fl.original_name, fl.size_bytes, fl.mime_type, fl.sha256
           FROM messages m
           JOIN agents s ON s.id = m.sender_id
           LEFT JOIN groups g ON g.id = m.group_id
           LEFT JOIN file_logs fl ON fl.file_id = m.file_id
           WHERE m.id > ? AND m.sender_id <> ?
             AND (
                   m.recipient_id = ?
                   OR EXISTS (
                       SELECT 1 FROM group_members gm
                       WHERE gm.group_id = m.group_id
                         AND gm.agent_id = ?
                         AND m.created_at >= gm.joined_at
                   )
                 )
           ORDER BY m.id ASC
           LIMIT ?""",
        (since_id, agent_row_id, agent_row_id, agent_row_id, limit + 1),
    ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]

    messages = []
    for r in rows:
        file_info = None
        if r["file_id"]:
            file_info = {
                "file_id": r["file_id"],
                "original_name": r["original_name"],
                "size_bytes": r["size_bytes"],
                "mime_type": r["mime_type"],
                "sha256": r["sha256"],
            }
        messages.append(
            {
                "id": r["id"],
                "scope": "group" if r["group_name"] else "direct",
                "from": r["sender_name"],
                "group": r["group_name"],
                "msg_type": r["msg_type"],
                "content": r["content"],
                "file": file_info,
                "reply_to": r["reply_to"],
                "created_at": r["created_at"],
            }
        )
    next_since_id = rows[-1]["id"] if rows else since_id
    return messages, next_since_id, has_more
