"""Message logic: idempotency, target resolution, fetch queries, group membership.

A message address is always an (ID, name) pair: the sender sends target_id +
target_name, and the server only accepts the message if the row with that ID
is actually named accordingly. This prevents misdelivered messages caused by
a changed or colliding name.
"""
import re
import secrets
import sqlite3

from ..security import api_error

ID_PATTERN_HINT = (
    "target_id harus seperti 'ag_xxx' (agent) atau 'gr_xxx' (grup) — "
    "lihat GET /agents atau GET /groups."
)

# @name mentions inside the message content. Agent names follow the same
# pattern as registration: lowercase letters, digits, '_' and '-'.
MENTION_RE = re.compile(r"@([a-z0-9][a-z0-9_-]{2,31})")


def extract_mentions(content: str | None) -> list[str]:
    """Extract mentioned agent names (@name), unique, in order of appearance."""
    if not content:
        return []
    return list(dict.fromkeys(MENTION_RE.findall(content.lower())))


def store_mentions(
    conn: sqlite3.Connection,
    message_id: int,
    group_id: int | None,
    names: list[str],
) -> list[str]:
    """Record mentions into `message_mentions`.

    In a group, only mentions of group members are recorded — mentions of
    non-members are ignored (they would not receive the message anyway).
    Return the names actually recorded.
    """
    if not names:
        return []
    placeholders = ",".join("?" * len(names))
    if group_id is not None:
        rows = conn.execute(
            f"""SELECT a.id, a.name FROM agents a
                JOIN group_members gm ON gm.agent_id = a.id AND gm.group_id = ?
                WHERE a.name IN ({placeholders}) COLLATE NOCASE""",
            (group_id, *names),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id, name FROM agents WHERE name IN ({placeholders}) COLLATE NOCASE",
            names,
        ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO message_mentions (message_id, agent_id) VALUES (?, ?)",
            (message_id, r["id"]),
        )
    return [r["name"] for r in rows]


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
    """Find an active agent matching the ID *and* the name at once."""
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
    """Find a group matching the ID *and* the name at once."""
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
    """All messages with a given task_id that are visible to this agent.

    Used for the lightweight RPC pattern: the sender sends a message of type
    `task` with a `task_id`, the recipient replies with a message of type
    `result` carrying the same `task_id`. Both sides can read the full history
    of one task via GET /tasks/{task_id}.
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
    """Fetch messages for this agent. The since_id cursor rises monotonically.

    The group membership filter uses joined_at: a new member does not see
    messages sent before they joined.
    """
    rows = conn.execute(
        """SELECT m.id, m.msg_type, m.content, m.file_id, m.created_at,
                  m.reply_to, m.task_id, m.task_status,
                  s.name AS sender_name, s.agent_id AS sender_agent_id,
                  g.name AS group_name, g.group_id AS group_public_id,
                  fl.original_name, fl.size_bytes, fl.mime_type, fl.sha256,
                  (SELECT GROUP_CONCAT(a2.name, ',') FROM message_mentions mm
                     JOIN agents a2 ON a2.id = mm.agent_id
                    WHERE mm.message_id = m.id) AS mention_names
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
                "mentions": r["mention_names"].split(",") if r["mention_names"] else [],
                "file": file_info,
                "reply_to": r["reply_to"],
                "created_at": r["created_at"],
            }
        )
    next_since_id = rows[-1]["id"] if rows else since_id
    return messages, next_since_id, has_more
