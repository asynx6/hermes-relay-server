"""POST /send-message, GET /fetch-messages, ack, receipts, dan tasks."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..database import begin_immediate, connect
from ..ratelimit import limiter
from ..schemas import AckRequest, SendMessageRequest
from ..security import api_error, get_current_agent
from ..services import message_service

router = APIRouter()


@router.post("/send-message")
@limiter.limit(lambda: settings.rate_limit_default)
def send_message(body: SendMessageRequest, request: Request, agent=Depends(get_current_agent)):
    # Satu koneksi untuk cek duplikat + insert; transaksi membuat retry aman.
    conn = connect()
    try:
        begin_immediate(conn)
        duplicate = message_service.find_by_client_msg_id(
            conn, agent["id"], body.client_msg_id
        )
        if duplicate:
            conn.commit()
            # client_msg_id sudah pernah dikirim: balas 200 dengan data pesan lama.
            return JSONResponse(
                status_code=200,
                content={
                    "status": "ok",
                    "data": {
                        "message_id": duplicate["id"],
                        "target_type": (
                            "group" if duplicate["group_id"] else "direct"
                        ),
                        "target": duplicate["group_name"] or duplicate["recipient_name"],
                        "created_at": duplicate["created_at"],
                        "duplicate": True,
                    },
                },
            )

        if body.target_type == "direct":
            if body.target_name.strip().lower() == agent["name"].lower():
                raise api_error(400, "BAD_REQUEST", "Tidak bisa mengirim pesan ke diri sendiri.")
            recipient = message_service.resolve_direct_target(
                conn, body.target_id, body.target_name
            )
            recipient_id, group_id = recipient["id"], None
        else:
            group = message_service.resolve_group_target(
                conn, body.target_id, body.target_name
            )
            message_service.ensure_group_member(conn, group["id"], agent["id"])
            recipient_id, group_id = None, group["id"]

        if body.reply_to is not None:
            message_service.validate_reply_to(conn, body.reply_to, agent["id"])

        row = message_service.store_message(
            conn,
            sender_row_id=agent["id"],
            client_msg_id=body.client_msg_id,
            recipient_row_id=recipient_id,
            group_id=group_id,
            content=body.content,
            reply_to=body.reply_to,
            msg_type=body.msg_type,
            task_id=body.task_id,
            task_status=body.task_status,
        )
        conn.commit()
        message_id, created_at = row["id"], row["created_at"]
    finally:
        conn.close()

    return JSONResponse(
        status_code=201,
        content={
            "status": "ok",
            "data": {
                "message_id": message_id,
                "target_type": body.target_type,
                "target": body.target_name,
                "target_id": body.target_id,
                "msg_type": body.msg_type,
                "task_id": body.task_id,
                "created_at": created_at,
                "duplicate": False,
            },
        },
    )


@router.get("/fetch-messages")
@limiter.limit(lambda: settings.rate_limit_default)
def fetch_messages(
    request: Request,
    since_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    agent=Depends(get_current_agent),
):
    conn = connect()
    try:
        messages, next_since_id, has_more = message_service.fetch_messages(
            conn, agent["id"], since_id, limit
        )
        conn.execute(
            "UPDATE agents SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (agent["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "data": {
                "messages": messages,
                "next_since_id": next_since_id,
                "has_more": has_more,
                # Berubah kalau DB di-reset; client harus menyadari cursor
                # lama tidak valid lagi kalau nilai ini berbeda.
                "server_epoch": settings.server_epoch,
            },
        },
    )


@router.post("/ack")
@limiter.limit(lambda: settings.rate_limit_default)
def ack_messages(body: AckRequest, request: Request, agent=Depends(get_current_agent)):
    """Tandai pesan sebagai diterima/dibaca oleh agent pemanggil.

    Hanya pesan yang benar-benar ditujukan ke agent ini (langsung atau lewat
    grupnya) yang boleh di-ack; pesan milik agent lain diabaikan.
    """
    conn = connect()
    try:
        for message_id in body.message_ids:
            visible = conn.execute(
                """SELECT 1 FROM messages m
                   WHERE m.id = ?
                     AND (
                           m.recipient_id = ?
                           OR EXISTS (
                               SELECT 1 FROM group_members gm
                               WHERE gm.group_id = m.group_id AND gm.agent_id = ?
                           )
                         )""",
                (message_id, agent["id"], agent["id"]),
            ).fetchone()
            if not visible:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO message_receipts (message_id, agent_id, status) "
                "VALUES (?, ?, ?)",
                (message_id, agent["id"], body.status),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {"acked": len(body.message_ids), "status": body.status},
    }


@router.get("/receipts")
@limiter.limit(lambda: settings.rate_limit_default)
def receipts(
    request: Request,
    message_ids: str = Query(..., description="Daftar message_id dipisah koma"),
    agent=Depends(get_current_agent),
):
    """Status penerimaan pesan yang dikirim agent ini (butuh daftar ID)."""
    try:
        ids = [int(x) for x in message_ids.split(",") if x.strip()][:100]
    except ValueError:
        raise api_error(400, "BAD_REQUEST", "message_ids harus berupa angka.")
    if not ids:
        raise api_error(400, "BAD_REQUEST", "message_ids kosong.")

    placeholders = ",".join("?" * len(ids))
    conn = connect()
    try:
        rows = conn.execute(
            f"""SELECT mr.message_id, mr.agent_id, a.name AS agent_name,
                       mr.status, mr.at
                FROM message_receipts mr
                JOIN agents a ON a.id = mr.agent_id
                JOIN messages m ON m.id = mr.message_id
                WHERE mr.message_id IN ({placeholders}) AND m.sender_id = ?""",
            (*ids, agent["id"]),
        ).fetchall()
    finally:
        conn.close()

    by_message: dict[int, list[dict]] = {}
    for r in rows:
        by_message.setdefault(r["message_id"], []).append(
            {"agent": r["agent_name"], "status": r["status"], "at": r["at"]}
        )
    return {
        "status": "ok",
        "data": {
            "receipts": [
                {"message_id": k, "seen_by": v} for k, v in by_message.items()
            ]
        },
    }


@router.get("/tasks/{task_id}")
@limiter.limit(lambda: settings.rate_limit_default)
def get_task(request: Request, task_id: str, agent=Depends(get_current_agent)):
    """Riwayat satu tugas (task + result) yang terlihat oleh agent ini."""
    conn = connect()
    try:
        thread = message_service.task_thread(conn, task_id, agent["id"])
    finally:
        conn.close()
    if not thread:
        raise api_error(404, "NOT_FOUND", f"Tugas '{task_id}' tidak ditemukan.")
    return {
        "status": "ok",
        "data": {"task_id": task_id, "messages": thread},
    }
