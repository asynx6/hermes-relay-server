"""Logika pesan: idempotency, resolusi target, query fetch, keanggotaan grup.

Alamat pesan selalu pasangan (ID, nama): pengirim mengirim target_id +
target_name, dan server hanya menerima pesan kalau baris dengan ID tersebut
benar-benar bernama sesuai. Ini mencegah pesan salah kirim akibat nama yang
berubah atau bentrok.
"""
import secrets
import sqlite3

from ..security import api_error

ID_PATTERN_HINT = (
    "target_id harus seperti 'ag_xxx' (agent) atau 'gr_xxx' (grup) — "
    "lihat GET /agents atau GET /groups."
)


def new_agent_id(conn: sqlite3.Connection) -> str:
    while True:
        candidate = "ag_" + secrets.token_hex(5)
        exists = conn.execute(
            "SELECT 1 FROM agents WHERE agent_id = ?", (candidate,)
        ).fetchone()
        if not exists:
            return candidate


def new_group_id(conn: sqlite3.Connection) -> str:
    while True:
        candidate = "gr_" + secrets.token_hex(5)
        exists = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (candidate,)
        ).fetchone()
        if not exists:
            return candidate


def resolve_direct_target(
    conn: sqlite3.Connection, target_id: str, target_name: str
) -> sqlite3.Row:
    """Cari agent aktif yang cocok dengan ID *dan* nama sekaligus."""
    row = conn.execute(
        "SELECT * FROM agents WHERE agent_id = ? AND name = ? COLLATE NOCASE "
        "AND status = 'active'",
        (target_id, target_name),
    ).fetchone()
    if row is None:
        raise api_error(
            404, "NOT_FOUND", "Agent tidak ditemukan, atau ID dan nama tidak cocok."
        )
    return row


def resolve_group_target(
    conn: sqlite3.Connection, target_id: str, target_name: str
) -> sqlite3.Row:
    """Cari grup yang cocok dengan ID *dan* nama sekaligus."""
    row = conn.execute(
        "SELECT * FROM groups WHERE group_id = ? AND name = ? COLLATE NOCASE",
        (target_id, target_name),
    ).fetchone()
    if row is None:
        raise api_error(
            404, "NOT_FOUND", "Grup tidak ditemukan, atau ID dan nama tidak cocok."
        )
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
    msg_type: str = "text",
    task_id: str | None = None,
    task_status: str | None = None,
) -> sqlite3.Row:
    cur = conn.execute(
        """INSERT INTO messages
             (client_msg_id, sender_id, recipient_id, group_id, msg_type, content,
              reply_to, task_id, task_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            client_msg_id,
            sender_row_id,
            recipient_row_id,
            group_id,
            msg_type,
            content,
            reply_to,
            task_id,
            task_status,
        ),
    )
    return conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()


def task_thread(
    conn: sqlite3.Connection, task_id: str, agent_row_id: int
) -> list[dict]:
    """Semua pesan dengan task_id tertentu yang terlihat oleh agent ini.

    Dipakai pola RPC ringan: pengirim mengirim pesan bertipe `task`
    dengan `task_id`, penerima membalas dengan pesan bertipe `result`
    yang membawa `task_id` yang sama. Kedua sisi bisa membaca seluruh
    riwayat satu tugas lewat GET /tasks/{task_id}.
    """
    rows = conn.execute(
        """SELECT m.id, m.msg_type, m.content, m.created_at, m.task_status,
                  s.name AS sender_name, s.agent_id AS sender_agent_id,
                  r.name AS recipient_name, g.name AS group_name
           FROM messages m
           JOIN agents s ON s.id = m.sender_id
           LEFT JOIN agents r ON r.id = m.recipient_id
           LEFT JOIN groups g ON g.id = m.group_id
           WHERE m.task_id = ?
             AND (
                   m.sender_id = ?
                   OR m.recipient_id = ?
                   OR EXISTS (
                       SELECT 1 FROM group_members gm
                       WHERE gm.group_id = m.group_id AND gm.agent_id = ?
                         AND m.created_at >= gm.joined_at
                   )
                 )
           ORDER BY m.id""",
        (task_id, agent_row_id, agent_row_id, agent_row_id),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "msg_type": r["msg_type"],
            "task_status": r["task_status"],
            "content": r["content"],
            "from": r["sender_name"],
            "from_id": r["sender_agent_id"],
            "to": r["recipient_name"],
            "group": r["group_name"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def fetch_messages(
    conn: sqlite3.Connection, agent_row_id: int, since_id: int, limit: int
) -> tuple[list[dict], int, bool]:
    """Ambil pesan untuk agent ini. Cursor since_id naik monotonik.

    Filter keanggotaan grup memakai joined_at: anggota baru tidak melihat
    pesan yang terkirim sebelum ia join.
    """
    rows = conn.execute(
        """SELECT m.id, m.msg_type, m.content, m.file_id, m.created_at,
                  m.reply_to, m.task_id, m.task_status,
                  s.name AS sender_name, s.agent_id AS sender_agent_id,
                  g.name AS group_name, g.group_id AS group_public_id,
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
                "from_id": r["sender_agent_id"],
                "group": r["group_name"],
                "group_id": r["group_public_id"],
                "msg_type": r["msg_type"],
                "content": r["content"],
                "task_id": r["task_id"],
                "task_status": r["task_status"],
                "file": file_info,
                "reply_to": r["reply_to"],
                "created_at": r["created_at"],
            }
        )
    next_since_id = rows[-1]["id"] if rows else since_id
    return messages, next_since_id, has_more
