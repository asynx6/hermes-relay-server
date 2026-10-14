"""Kelola grup: buat, daftar, join (dengan join_code), leave, anggota, role.

Model keamanan grup:
- Pembuat grup otomatis menjadi **admin**.
- Admin bisa mengatur `join_code`, mengangkat/menurunkan role anggota, dan
  mengeluarkan (kick) anggota.
- Grup tanpa `join_code` bersifat terbuka (siapa pun yang terautentikasi
  boleh join) — ini menjaga kompatibilitas dengan alur lama.
- Admin terakhir tidak bisa keluar atau diturunkan sebelum ada admin lain,
  supaya grup tidak pernah kehilangan pengelola.
"""
import secrets
import sqlite3

from fastapi import APIRouter, Depends, Request, status

from ..config import settings
from ..database import begin_immediate, connect
from ..ratelimit import limiter
from ..schemas import CreateGroupRequest, JoinGroupRequest, SetJoinCodeRequest, SetRoleRequest
from ..security import api_error, require_scope
from ..services import audit_service, message_service

router = APIRouter()


def _new_join_code() -> str:
    return secrets.token_hex(6)


def _group_by_name(conn, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM groups WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def _member_row(conn, group_row_id: int, agent_row_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM group_members WHERE group_id = ? AND agent_id = ?",
        (group_row_id, agent_row_id),
    ).fetchone()


def _require_admin(conn, group_row_id: int, agent_row_id: int) -> sqlite3.Row:
    row = _member_row(conn, group_row_id, agent_row_id)
    if row is None:
        raise api_error(403, "FORBIDDEN", "Bukan anggota grup ini.")
    if row["role"] != "admin":
        raise api_error(403, "FORBIDDEN", "Hanya admin grup yang boleh melakukan ini.")
    return row


def _admin_count(conn, group_row_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM group_members WHERE group_id = ? AND role = 'admin'",
        (group_row_id,),
    ).fetchone()["n"]


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/groups", status_code=status.HTTP_201_CREATED)
@limiter.limit(lambda: settings.rate_limit_default)
def create_group(
    body: CreateGroupRequest,
    request: Request,
    agent=Depends(require_scope("groups:write")),
):
    """Buat grup. Pembuat otomatis menjadi admin; join_code opsional."""
    conn = connect()
    try:
        begin_immediate(conn)  # cek-tulis harus atomik
        clash = _group_by_name(conn, body.name)
        if clash:
            conn.commit()
            raise api_error(409, "CONFLICT", f"Nama grup '{body.name}' sudah dipakai.")

        public_id = message_service.new_group_id(conn)
        cur = conn.execute(
            "INSERT INTO groups (group_id, name, description, created_by) VALUES (?, ?, ?, ?)",
            (public_id, body.name, body.description, agent["id"]),
        )
        row_id = cur.lastrowid
        conn.execute(
            "INSERT INTO group_members (group_id, agent_id, role) VALUES (?, ?, 'admin')",
            (row_id, agent["id"]),
        )
        conn.commit()
    except (sqlite3.IntegrityError, sqlite3.OperationalError):
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise api_error(409, "CONFLICT", f"Nama grup '{body.name}' sudah dipakai.")
    finally:
        conn.close()

    return {
        "status": "ok",
        "data": {"group_id": public_id, "name": body.name, "role": "admin"},
    }


@router.get("/groups")
@limiter.limit(lambda: settings.rate_limit_default)
def list_groups(request: Request, agent=Depends(require_scope("groups:read"))):
    """Direktori grup: semua grup + status keanggotaan dan role pemanggil."""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT g.group_id, g.name, g.description, g.created_at,
                      (g.join_code IS NOT NULL) AS has_join_code,
                      (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS member_count,
                      (SELECT gm2.joined_at FROM group_members gm2
                        WHERE gm2.group_id = g.id AND gm2.agent_id = ?) AS my_joined_at,
                      (SELECT gm3.role FROM group_members gm3
                        WHERE gm3.group_id = g.id AND gm3.agent_id = ?) AS my_role
               FROM groups g ORDER BY g.created_at""",
            (agent["id"], agent["id"]),
        ).fetchall()
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "groups": [
                {
                    "group_id": r["group_id"],
                    "name": r["name"],
                    "description": r["description"],
                    "created_at": r["created_at"],
                    "member_count": r["member_count"],
                    "is_member": r["my_joined_at"] is not None,
                    "joined_at": r["my_joined_at"],
                    "role": r["my_role"],
                    "requires_join_code": bool(r["has_join_code"]),
                }
                for r in rows
            ]
        },
    }


@router.get("/groups/{name}/members")
@limiter.limit(lambda: settings.rate_limit_default)
def list_members(name: str, request: Request, agent=Depends(require_scope("groups:read"))):
    """Daftar anggota grup + role. Hanya anggota yang boleh melihat."""
    conn = connect()
    try:
        group = _group_by_name(conn, name)
        if group is None:
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
        if _member_row(conn, group["id"], agent["id"]) is None:
            raise api_error(403, "FORBIDDEN", "Bukan anggota grup ini.")
        rows = conn.execute(
            """SELECT a.name, a.agent_id, gm.role, gm.joined_at
               FROM group_members gm JOIN agents a ON a.id = gm.agent_id
               WHERE gm.group_id = ?
               ORDER BY CASE gm.role WHEN 'admin' THEN 0 ELSE 1 END, gm.joined_at""",
            (group["id"],),
        ).fetchall()
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "group": group["name"],
            "group_id": group["group_id"],
            "members": [
                {
                    "name": r["name"],
                    "agent_id": r["agent_id"],
                    "role": r["role"],
                    "joined_at": r["joined_at"],
                }
                for r in rows
            ],
        },
    }


@router.post("/groups/{name}/join")
@limiter.limit(lambda: settings.rate_limit_default)
def join_group(
    name: str,
    request: Request,
    body: JoinGroupRequest | None = None,
    agent=Depends(require_scope("groups:write")),
):
    """Gabung grup. Kalau grup punya join_code, kode itu wajib cocok."""
    supplied_code = body.join_code if body else None
    conn = connect()
    try:
        begin_immediate(conn)
        group = _group_by_name(conn, name)
        if group is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")

        existing = _member_row(conn, group["id"], agent["id"])
        if existing:
            conn.commit()
            return {
                "status": "ok",
                "data": {
                    "group": group["name"],
                    "joined_at": existing["joined_at"],
                    "role": existing["role"],
                    "already_member": True,
                },
            }

        if group["join_code"] is not None:
            if not supplied_code or supplied_code != group["join_code"]:
                conn.commit()
                raise api_error(403, "FORBIDDEN", "join_code salah atau tidak diberikan.")

        conn.execute(
            "INSERT INTO group_members (group_id, agent_id, role) VALUES (?, ?, 'member')",
            (group["id"], agent["id"]),
        )
        joined_at = _member_row(conn, group["id"], agent["id"])["joined_at"]
        conn.commit()
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK") if conn.in_transaction else None
        return {
            "status": "ok",
            "data": {"group": name, "joined_at": None, "role": "member", "already_member": True},
        }
    finally:
        conn.close()

    return {
        "status": "ok",
        "data": {
            "group": name,
            "joined_at": joined_at,
            "role": "member",
            "already_member": False,
        },
    }


@router.post("/groups/{name}/leave")
@limiter.limit(lambda: settings.rate_limit_default)
def leave_group(name: str, request: Request, agent=Depends(require_scope("groups:write"))):
    """Keluar dari grup. Admin terakhir tidak boleh keluar selama masih ada
    anggota lain (angkat admin lain dulu)."""
    conn = connect()
    try:
        begin_immediate(conn)
        group = _group_by_name(conn, name)
        if group is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
        row = _member_row(conn, group["id"], agent["id"])
        if row is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", "Kamu bukan anggota grup ini.")

        others = conn.execute(
            "SELECT COUNT(*) AS n FROM group_members WHERE group_id = ? AND agent_id <> ?",
            (group["id"], agent["id"]),
        ).fetchone()["n"]
        if row["role"] == "admin" and others > 0 and _admin_count(conn, group["id"]) <= 1:
            conn.commit()
            raise api_error(
                409,
                "CONFLICT",
                "Kamu admin terakhir; angkat admin lain sebelum keluar.",
            )

        conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND agent_id = ?",
            (group["id"], agent["id"]),
        )
        audit_service.record(
            conn,
            actor=agent["name"],
            actor_type="agent",
            action=audit_service.ACTION_GROUP_MEMBER_CHANGE,
            target=group["name"],
            detail={"change": "leave"},
            ip=_client_ip(request),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "data": {"group": name, "left": True}}


@router.delete("/groups/{name}/members/{agent_name}")
@limiter.limit(lambda: settings.rate_limit_default)
def kick_member(
    name: str,
    agent_name: str,
    request: Request,
    agent=Depends(require_scope("groups:admin")),
):
    """Keluarkan anggota dari grup (admin saja)."""
    conn = connect()
    try:
        begin_immediate(conn)
        group = _group_by_name(conn, name)
        if group is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
        _require_admin(conn, group["id"], agent["id"])

        target = conn.execute(
            "SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (agent_name,)
        ).fetchone()
        if target is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Agent '{agent_name}' tidak ditemukan.")
        if target["id"] == agent["id"]:
            conn.commit()
            raise api_error(400, "BAD_REQUEST", "Gunakan /leave untuk keluar sendiri.")

        row = _member_row(conn, group["id"], target["id"])
        if row is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"'{target['name']}' bukan anggota grup ini.")
        if row["role"] == "admin" and _admin_count(conn, group["id"]) <= 1:
            conn.commit()
            raise api_error(409, "CONFLICT", "Tidak bisa mengeluarkan admin terakhir.")

        conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND agent_id = ?",
            (group["id"], target["id"]),
        )
        audit_service.record(
            conn,
            actor=agent["name"],
            actor_type="agent",
            action=audit_service.ACTION_GROUP_MEMBER_CHANGE,
            target=group["name"],
            detail={"change": "kick", "member": target["name"]},
            ip=_client_ip(request),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "data": {"group": name, "kicked": agent_name}}


@router.put("/groups/{name}/members/{agent_name}/role")
@limiter.limit(lambda: settings.rate_limit_default)
def set_role(
    name: str,
    agent_name: str,
    body: SetRoleRequest,
    request: Request,
    agent=Depends(require_scope("groups:admin")),
):
    """Ubah role anggota (admin saja). Admin terakhir tidak bisa diturunkan."""
    conn = connect()
    try:
        begin_immediate(conn)
        group = _group_by_name(conn, name)
        if group is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
        _require_admin(conn, group["id"], agent["id"])

        target = conn.execute(
            "SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (agent_name,)
        ).fetchone()
        if target is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Agent '{agent_name}' tidak ditemukan.")
        row = _member_row(conn, group["id"], target["id"])
        if row is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"'{target['name']}' bukan anggota grup ini.")

        if body.role == "member" and row["role"] == "admin" and _admin_count(conn, group["id"]) <= 1:
            conn.commit()
            raise api_error(409, "CONFLICT", "Tidak bisa menurunkan admin terakhir.")

        conn.execute(
            "UPDATE group_members SET role = ? WHERE group_id = ? AND agent_id = ?",
            (body.role, group["id"], target["id"]),
        )
        audit_service.record(
            conn,
            actor=agent["name"],
            actor_type="agent",
            action=audit_service.ACTION_GROUP_MEMBER_CHANGE,
            target=group["name"],
            detail={"change": "role", "member": target["name"], "role": body.role},
            ip=_client_ip(request),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "data": {"group": name, "member": agent_name, "role": body.role}}


@router.post("/groups/{name}/join-code")
@limiter.limit(lambda: settings.rate_limit_default)
def set_join_code(
    name: str,
    body: SetJoinCodeRequest,
    request: Request,
    agent=Depends(require_scope("groups:admin")),
):
    """Atur/ganti/kosongkan join_code grup (admin saja).

    `join_code` tidak di-set -> grup terbuka. `join_code` di-set -> join
    wajib menyertakan kode yang cocok. Kirim `null` untuk mengosongkan.
    """
    conn = connect()
    try:
        begin_immediate(conn)
        group = _group_by_name(conn, name)
        if group is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
        _require_admin(conn, group["id"], agent["id"])

        code = body.join_code if body.join_code is not None else _new_join_code()
        conn.execute(
            "UPDATE groups SET join_code = ? WHERE id = ?", (code, group["id"])
        )
        audit_service.record(
            conn,
            actor=agent["name"],
            actor_type="agent",
            action=audit_service.ACTION_GROUP_JOIN_CODE,
            target=group["name"],
            ip=_client_ip(request),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {"group": name, "join_code": code},
    }


@router.delete("/groups/{name}/join-code")
@limiter.limit(lambda: settings.rate_limit_default)
def clear_join_code(
    name: str, request: Request, agent=Depends(require_scope("groups:admin"))
):
    """Kosongkan join_code sehingga grup kembali terbuka (admin saja)."""
    conn = connect()
    try:
        begin_immediate(conn)
        group = _group_by_name(conn, name)
        if group is None:
            conn.commit()
            raise api_error(404, "NOT_FOUND", f"Grup '{name}' tidak ditemukan.")
        _require_admin(conn, group["id"], agent["id"])
        conn.execute("UPDATE groups SET join_code = NULL WHERE id = ?", (group["id"],))
        audit_service.record(
            conn,
            actor=agent["name"],
            actor_type="agent",
            action=audit_service.ACTION_GROUP_JOIN_CODE,
            target=group["name"],
            detail={"change": "cleared"},
            ip=_client_ip(request),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "data": {"group": name, "join_code": None}}
