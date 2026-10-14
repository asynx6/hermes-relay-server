"""Audit log append-only untuk aksi administratif dan keamanan.

Dashboard adalah permukaan admin: pemegang sesi bisa mengirim pesan sebagai
agent mana pun, mengunduh semua file, dan mengubah keanggotaan grup. Tanpa
jejak, penyalahgunaan tidak bisa ditelusuri. Setiap aksi penting dicatat di
sini: aktor, aksi, target, detail, IP, dan hasilnya.

Log bersifat append-only — tidak ada UPDATE/DELETE kecuali pemangkasan
retention (`prune`) yang membuang baris lebih tua dari N hari.
"""
import json
import sqlite3

# Aksi yang dicatat. Dikumpulkan di sini agar konsisten dan mudah dicari.
ACTION_SEND_AS_AGENT = "send_as_agent"
ACTION_DELETE_FILE = "delete_file"
ACTION_DOWNLOAD_FILE = "download_file"
ACTION_CHANGE_PASSWORD = "change_password"
ACTION_LOGIN_FAILED = "login_failed"
ACTION_ROTATE_TOKEN = "rotate_token"
ACTION_CREATE_TOKEN = "create_token"
ACTION_REVOKE_TOKEN = "revoke_token"
ACTION_GROUP_MEMBER_CHANGE = "group_member_change"
ACTION_GROUP_JOIN_CODE = "group_join_code"


def record(
    conn: sqlite3.Connection,
    *,
    actor: str,
    actor_type: str,
    action: str,
    target: str | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    result: str = "ok",
) -> None:
    """Catat satu baris audit. TIDAK commit — ikut transaksi pemanggil."""
    conn.execute(
        """INSERT INTO audit_log (actor, actor_type, action, target, detail, ip, result)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            actor,
            actor_type,
            action,
            target,
            json.dumps(detail, ensure_ascii=False) if detail is not None else None,
            ip,
            result,
        ),
    )


def safe_record(conn: sqlite3.Connection, **kwargs) -> None:
    """Seperti record(), tapi kegagalan audit tidak boleh menggagalkan aksi utama.

    Dipakai untuk aksi yang sudah terlanjur terjadi (mis. login gagal) —
    kehilangan satu baris log lebih baik daripada membalas 500 ke pengguna.
    """
    try:
        record(conn, **kwargs)
        conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
    actor_type: str | None = None,
) -> tuple[list[dict], int]:
    """Ambil baris audit terbaru + totalnya, dengan filter opsional."""
    where, params = [], []
    if action:
        where.append("action = ?")
        params.append(action)
    if actor_type:
        where.append("actor_type = ?")
        params.append(actor_type)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM audit_log {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT id, actor, actor_type, action, target, detail, ip, result, at
            FROM audit_log {clause}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        if item.get("detail"):
            try:
                item["detail"] = json.loads(item["detail"])
            except (ValueError, TypeError):
                pass
        out.append(item)
    return out, total


def prune(conn: sqlite3.Connection, retention_days: int) -> int:
    """Buang baris audit lebih tua dari retention_days. Kembalikan jumlahnya."""
    cur = conn.execute(
        "DELETE FROM audit_log WHERE at < "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (f"-{retention_days} days",),
    )
    return cur.rowcount
