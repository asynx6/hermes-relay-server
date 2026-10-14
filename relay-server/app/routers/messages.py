"""POST /send-message, GET /fetch-messages, ack, receipts, tasks, dan SSE.

Dua jalur pengambilan pesan tersedia:

- **Polling** (`GET /fetch-messages`) — client bertanya berkala. Sederhana dan
  selalu bekerja.
- **Long polling** (`GET /fetch-messages?wait=25`) — request ditahan sampai
  ada pesan baru atau timeout, sehingga latensi turun ke hampir instan tanpa
  menambah jumlah request.
- **SSE** (`GET /events`) — satu koneksi terbuka; server mendorong pesan
  begitu tiba. Paling hemat, tapi butuh proxy yang tidak mem-buffer respons.

Keduanya opsional (`LONG_POLL_ENABLED`, `SSE_ENABLED`); polling biasa tetap
jadi jalur fallback.
"""
import asyncio
import json
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..database import begin_immediate, connect
from ..ratelimit import limiter
from ..schemas import AckRequest, SendMessageRequest
from ..security import api_error, require_scope
from ..services import message_service

router = APIRouter()


def _fetch_only(agent_row_id: int, since_id: int, limit: int):
    """Ambil pesan saja (read-only) — dipakai di dalam loop tunggu."""
    conn = connect()
    try:
        return message_service.fetch_messages(conn, agent_row_id, since_id, limit)
    finally:
        conn.close()


def _touch_last_seen(agent_row_id: int) -> None:
    """Perbarui `last_seen_at` SEKALI per request.

    Sengaja dipisah dari `_fetch_only`: long polling/SSE memanggil fetch
    berkali-kali selama menunggu, dan menulis setiap iterasi akan menjadi
    amplifikasi tulis besar kalau banyak agent menunggu bersamaan.
    """
    conn = connect()
    try:
        conn.execute(
            "UPDATE agents SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id = ?",
            (agent_row_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _current_max_id() -> int:
    """ID pesan tertinggi saat ini — titik mulai SSE agar hanya pesan BARU
    yang didorong ke koneksi yang baru dibuka."""
    conn = connect()
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM messages").fetchone()
        return row["n"]
    finally:
        conn.close()


@router.post("/send-message")
@limiter.limit(lambda: settings.rate_limit_default)
def send_message(
    body: SendMessageRequest,
    request: Request,
    agent=Depends(require_scope("messages:write")),
):
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
        # Mention @nama: dicatat agar penerima bisa menyaring pesan yang
        # benar-benar menyebut dirinya. Di grup hanya anggota yang dicatat.
        mentions = message_service.store_mentions(
            conn,
            row["id"],
            group_id,
            message_service.extract_mentions(body.content),
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
                "mentions": mentions,
                "created_at": created_at,
                "duplicate": False,
            },
        },
    )


@router.get("/fetch-messages")
@limiter.limit(lambda: settings.rate_limit_default)
async def fetch_messages(
    request: Request,
    since_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    wait: int = Query(
        default=0,
        ge=0,
        le=60,
        description="Long polling: tahan request sampai ada pesan atau N detik.",
    ),
    agent=Depends(require_scope("messages:read")),
):
    """Ambil pesan baru untuk agent ini.

    `wait=0` (default) mengembalikan segera — perilaku polling biasa. `wait>0`
    menahan request di server sampai ada pesan baru atau timeout, jadi client
    tahu soal pesan baru hampir seketika tanpa harus sering bertanya. Nilai
    dibatasi `LONG_POLL_MAX_SECONDS` (default 25) dan bisa dimatikan dengan
    `LONG_POLL_ENABLED=false`.
    """
    agent_row_id = agent["id"]
    effective_wait = min(wait, settings.long_poll_max_seconds) if settings.long_poll_enabled else 0
    deadline = time.monotonic() + effective_wait

    while True:
        messages, next_since_id, has_more = await run_in_threadpool(
            _fetch_only, agent_row_id, since_id, limit
        )
        if messages or has_more or time.monotonic() >= deadline:
            break
        if await request.is_disconnected():
            break
        await asyncio.sleep(0.4)

    await run_in_threadpool(_touch_last_seen, agent_row_id)

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "data": {
                "messages": messages,
                "next_since_id": next_since_id,
                "has_more": has_more,
                "waited": effective_wait,
                # Berubah kalau DB di-reset; client harus menyadari cursor
                # lama tidak valid lagi kalau nilai ini berbeda.
                "server_epoch": settings.server_epoch,
            },
        },
    )


@router.get("/events")
@limiter.limit(lambda: settings.rate_limit_default)
async def events(
    request: Request,
    since_id: int | None = Query(default=None, ge=0),
    agent=Depends(require_scope("messages:read")),
):
    """SSE: satu koneksi, server mendorong pesan baru begitu tiba.

    Kejadian yang dikirim:
      `event: messages` — payload sama dengan /fetch-messages.
      `event: epoch`    — dikirim sekali di awal (identitas instance DB).
      komentar `: ping` — heartbeat berkala agar koneksi tidak diputus proxy.

    Kalau `since_id` tidak diberikan, hanya pesan yang datang SETELAH koneksi
    dibuka yang didorong. Butuh `SSE_ENABLED=true` dan proxy tanpa buffering
    respons.
    """
    if not settings.sse_enabled:
        raise api_error(404, "NOT_FOUND", "SSE tidak diaktifkan di server ini.")

    agent_row_id = agent["id"]
    epoch = settings.server_epoch
    start = since_id if since_id is not None else await run_in_threadpool(_current_max_id)
    heartbeat = settings.sse_heartbeat_seconds
    await run_in_threadpool(_touch_last_seen, agent_row_id)  # tandai hidup saat connect

    async def stream():
        cursor = start
        yield f"event: epoch\ndata: {json.dumps({'server_epoch': epoch})}\n\n"
        last_beat = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            try:
                messages, cursor, has_more = await run_in_threadpool(
                    _fetch_only, agent_row_id, cursor, 50
                )
            except Exception:
                # Jangan matikan stream karena satu query gagal.
                await asyncio.sleep(1.0)
                continue
            if messages:
                payload = json.dumps(
                    {
                        "messages": messages,
                        "next_since_id": cursor,
                        "has_more": has_more,
                        "server_epoch": epoch,
                    }
                )
                yield f"event: messages\ndata: {payload}\n\n"
                # Sekali per batch yang benar-benar dikirim, bukan per iterasi.
                await run_in_threadpool(_touch_last_seen, agent_row_id)
                last_beat = time.monotonic()
                continue
            if time.monotonic() - last_beat >= heartbeat:
                yield ": ping\n\n"
                last_beat = time.monotonic()
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx: matikan buffering respons
        },
    )


@router.post("/ack")
@limiter.limit(lambda: settings.rate_limit_default)
def ack_messages(
    body: AckRequest,
    request: Request,
    agent=Depends(require_scope("messages:write")),
):
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
    agent=Depends(require_scope("messages:read")),
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
def get_task(
    request: Request, task_id: str, agent=Depends(require_scope("messages:read"))
):
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
