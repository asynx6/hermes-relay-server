"""POST /send-message dan GET /fetch-messages."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..database import connect
from ..ratelimit import limiter
from ..schemas import SendMessageRequest
from ..security import api_error, get_current_agent
from ..services import message_service

router = APIRouter()


@router.post("/send-message")
@limiter.limit(lambda: settings.rate_limit_default)
def send_message(body: SendMessageRequest, request: Request, agent=Depends(get_current_agent)):
    # Satu koneksi untuk cek duplikat + insert; transaksi membuat retry aman.
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
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
            if body.target.strip().lower() == agent["name"].lower():
                raise api_error(400, "BAD_REQUEST", "Tidak bisa mengirim pesan ke diri sendiri.")
            recipient = message_service.resolve_direct_target(conn, body.target)
            recipient_id, group_id = recipient["id"], None
        else:
            group = message_service.resolve_group_target(conn, body.target)
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
                "target": body.target,
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
            },
        },
    )
