"""POST /upload-file dan GET /download-file/<file_id>."""
import secrets

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import settings
from ..database import connect
from ..ratelimit import limiter
from ..schemas import UUID4_PATTERN
from ..security import api_error, get_current_agent
from ..services import file_service, message_service

router = APIRouter()


def _new_file_id(conn) -> str:
    while True:
        candidate = "fl_" + secrets.token_hex(5)
        if not conn.execute(
            "SELECT 1 FROM file_logs WHERE file_id = ?", (candidate,)
        ).fetchone():
            return candidate


@router.post("/upload-file")
@limiter.limit(lambda: settings.rate_limit_default)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    target_type: str = Form(...),
    target: str = Form(...),
    client_msg_id: str = Form(..., pattern=UUID4_PATTERN),
    caption: str | None = Form(default=None, max_length=500),
    agent=Depends(get_current_agent),
):
    if target_type not in ("direct", "group"):
        raise api_error(400, "BAD_REQUEST", "target_type harus 'direct' atau 'group'.")

    # Cek Content-Length lebih awal sebagai pertahanan pertama.
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > settings.max_upload_bytes:
        raise api_error(413, "FILE_TOO_LARGE", "Melebihi batas ukuran unggahan.")

    original_name = file_service.sanitize_filename(file.filename or "file")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Duplikat: file dengan client_msg_id yang sama sudah pernah dikirim.
        existing = conn.execute(
            """SELECT fl.*, m.id AS message_id FROM file_logs fl
               JOIN messages m ON m.file_id = fl.file_id
               WHERE fl.uploader_id = ? AND m.client_msg_id = ?""",
            (agent["id"], client_msg_id),
        ).fetchone()
        if existing:
            conn.commit()
            return JSONResponse(
                status_code=200,
                content={
                    "status": "ok",
                    "data": {
                        "file_id": existing["file_id"],
                        "message_id": existing["message_id"],
                        "original_name": existing["original_name"],
                        "size_bytes": existing["size_bytes"],
                        "mime_type": existing["mime_type"],
                        "sha256": existing["sha256"],
                        "created_at": existing["uploaded_at"],
                        "duplicate": True,
                    },
                },
            )

        if target_type == "direct":
            if target.strip().lower() == agent["name"].lower():
                raise api_error(400, "BAD_REQUEST", "Tidak bisa mengirim file ke diri sendiri.")
            recipient = message_service.resolve_direct_target(conn, target)
            recipient_id, group_id = recipient["id"], None
        else:
            group = message_service.resolve_group_target(conn, target)
            message_service.ensure_group_member(conn, group["id"], agent["id"])
            recipient_id, group_id = None, group["id"]

        dest = file_service.stored_path(original_name)
        size, sha256 = await file_service.save_upload(file, dest, settings.max_upload_bytes)

        file_id = _new_file_id(conn)
        conn.execute(
            """INSERT INTO file_logs
                 (file_id, original_name, stored_name, mime_type, size_bytes, sha256,
                  uploader_id, recipient_id, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_id,
                original_name,
                dest.name,
                file.content_type,
                size,
                sha256,
                agent["id"],
                recipient_id,
                group_id,
            ),
        )
        cur = conn.execute(
            """INSERT INTO messages
                 (client_msg_id, sender_id, recipient_id, group_id, msg_type, content, file_id)
               VALUES (?, ?, ?, ?, 'file', ?, ?)""",
            (client_msg_id, agent["id"], recipient_id, group_id, caption, file_id),
        )
        message_id = cur.lastrowid
        uploaded_at = conn.execute(
            "SELECT uploaded_at FROM file_logs WHERE file_id = ?", (file_id,)
        ).fetchone()["uploaded_at"]
        conn.commit()
    finally:
        conn.close()

    return JSONResponse(
        status_code=201,
        content={
            "status": "ok",
            "data": {
                "file_id": file_id,
                "message_id": message_id,
                "original_name": original_name,
                "size_bytes": size,
                "mime_type": file.content_type,
                "sha256": sha256,
                "created_at": uploaded_at,
            },
        },
    )


@router.get("/download-file/{file_id}")
@limiter.limit(lambda: settings.rate_limit_default)
def download_file(request: Request, file_id: str, agent=Depends(get_current_agent)):
    conn = connect()
    try:
        file_row = conn.execute(
            "SELECT * FROM file_logs WHERE file_id = ?", (file_id,)
        ).fetchone()
        file_service.check_download_permission(conn, file_row, agent["id"])

        stored = settings.filemanager_dir / file_row["stored_name"]
        if not stored.is_file():
            raise api_error(404, "NOT_FOUND", "File fisik tidak ditemukan di server.")

        conn.execute(
            "UPDATE file_logs SET download_count = download_count + 1 WHERE file_id = ?",
            (file_id,),
        )
        conn.commit()
    finally:
        conn.close()

    def iter_file():
        with stored.open("rb") as f:
            while chunk := f.read(file_service.CHUNK):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file_row["original_name"]}"',
            "X-File-SHA256": file_row["sha256"],
            "X-Content-Type-Options": "nosniff",
        },
    )
