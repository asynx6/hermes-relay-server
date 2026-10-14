"""POST /upload-file, GET /files, dan GET /download-file/<file_id>."""
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import settings
from ..database import begin_immediate, connect
from ..ratelimit import limiter
from ..schemas import PUBLIC_ID_PATTERN, UUID4_PATTERN
from ..security import api_error, get_current_agent
from ..services import file_service, message_service

router = APIRouter()

FILES_SELECT = """
    SELECT fl.file_id, fl.original_name, fl.size_bytes, fl.mime_type,
           fl.sha256, fl.uploaded_at,
           u.name AS uploader, r.name AS recipient, g.name AS group_name
    FROM file_logs fl
    JOIN agents u ON u.id = fl.uploader_id
    LEFT JOIN agents r ON r.id = fl.recipient_id
    LEFT JOIN groups g ON g.id = fl.group_id
    WHERE fl.deleted_at IS NULL
      AND (
            fl.uploader_id = :me
            OR fl.recipient_id = :me
            OR EXISTS (
                -- Anggota grup hanya melihat file yang diunggah setelah
                -- ia bergabung (aturan yang sama dengan pesan grup).
                SELECT 1 FROM group_members gm
                WHERE gm.group_id = fl.group_id AND gm.agent_id = :me
                  AND fl.uploaded_at >= gm.joined_at
            )
          )
"""


def _file_out(r) -> dict:
    if r["recipient"]:
        scope, target = "direct", r["recipient"]
    else:
        scope, target = "group", r["group_name"]
    return {
        "file_id": r["file_id"],
        "original_name": r["original_name"],
        "size_bytes": r["size_bytes"],
        "mime_type": r["mime_type"],
        "sha256": r["sha256"],
        "uploader": r["uploader"],
        "scope": scope,
        "target": target,
        "uploaded_at": r["uploaded_at"],
    }


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
    target_id: str = Form(..., pattern=PUBLIC_ID_PATTERN),
    target_name: str = Form(..., min_length=3, max_length=32),
    client_msg_id: str = Form(..., pattern=UUID4_PATTERN),
    caption: str | None = Form(default=None, max_length=500),
    agent=Depends(get_current_agent),
):
    """Unggah file + buat pesan bertipe file dalam satu alur tervalidasi.

    Validasi berlapis (kegagalan tidak pernah meninggalkan sisa file):
      1. Form & pola ID                      -> 422
      2. Content-Length lebih awal           -> 413
      3. Ekstensi (daftar blokir)            -> 415
      4. Streaming ke staging: ukuran nyata  -> 413
      5. File kosong                         -> 400
      6. Magic bytes: payload eksekusi       -> 415
      7. (opsional) ekstensi vs isi          -> 415
    Lalu, dalam transaksi singkat:
      8. Duplikat client_msg_id (idempotent) -> 200 duplicate
      9. Target: ID+nama cocok, aktif        -> 404 / 400 (self)
     10. Keanggotaan grup                    -> 403
     11. Kuota per agent (kalau diaktifkan)  -> 413
     12. os.replace atomik + INSERT log+msg
    """
    if target_type not in ("direct", "group"):
        raise api_error(400, "BAD_REQUEST", "target_type harus 'direct' atau 'group'.")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise api_error(400, "BAD_REQUEST", "Content-Length tidak valid.")
        if declared > settings.max_upload_bytes:
            raise api_error(413, "FILE_TOO_LARGE", "Melebihi batas ukuran unggahan.")

    original_name = file_service.sanitize_filename(file.filename or "file")
    ext = file_service.safe_extension(original_name)  # 415 sebelum body dibaca

    staging = file_service.staging_path()
    try:
        meta = await file_service.stage_upload(file, staging, settings.max_upload_bytes)
    except file_service.UploadRejected as exc:
        raise api_error(exc.status_code, exc.code, exc.message)

    conn = connect()
    final_path = None
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")

        # (8) Idempotency: client_msg_id yang sama -> balas data pesan lama.
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

        # (9)+(10) Otorisasi target.
        if target_type == "direct":
            if target_name.strip().lower() == agent["name"].lower():
                raise api_error(400, "BAD_REQUEST", "Tidak bisa mengirim file ke diri sendiri.")
            recipient = message_service.resolve_direct_target(conn, target_id, target_name)
            recipient_id, group_id = recipient["id"], None
        else:
            group = message_service.resolve_group_target(conn, target_id, target_name)
            message_service.ensure_group_member(conn, group["id"], agent["id"])
            recipient_id, group_id = None, group["id"]

        # (11) Kuota penyimpanan per agent (0 = nonaktif).
        if settings.max_storage_mb_per_agent > 0:
            used = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS used FROM file_logs "
                "WHERE uploader_id = ? AND deleted_at IS NULL",
                (agent["id"],),
            ).fetchone()["used"]
            quota_bytes = settings.max_storage_mb_per_agent * 1024 * 1024
            if used + meta["size"] > quota_bytes:
                raise api_error(
                    413,
                    "QUOTA_EXCEEDED",
                    f"Kuota penyimpanan {settings.max_storage_mb_per_agent} MB per agent terlampaui.",
                )

        # (12) Commit atomik: rename + dua INSERT dalam satu transaksi.
        dest = file_service.finalize(staging, ext, conn)
        final_path = dest
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
                meta["mime"],
                meta["size"],
                meta["sha256"],
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
        committed = True
    except file_service.UploadRejected as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise api_error(exc.status_code, exc.code, exc.message)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
        if not committed:
            # Kegagalan sebelum commit: staging harus bersih. Kalau rename
            # sudah terjadi tapi commit gagal, buang file finalnya juga.
            file_service.discard(staging)
            if final_path is not None:
                Path(final_path).unlink(missing_ok=True)

    return JSONResponse(
        status_code=201,
        content={
            "status": "ok",
            "data": {
                "file_id": file_id,
                "message_id": message_id,
                "original_name": original_name,
                "size_bytes": meta["size"],
                "mime_type": meta["mime"],
                "declared_mime_type": meta["declared_mime"],
                "sha256": meta["sha256"],
                "created_at": uploaded_at,
                "duplicate": False,
            },
        },
    )


@router.get("/files")
@limiter.limit(lambda: settings.rate_limit_default)
def list_files(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    agent=Depends(get_current_agent),
):
    """Daftar file yang boleh diakses agent ini: yang diunggah sendiri,
    yang diterima langsung, atau file di grup tempat agent ini anggota.
    Setiap baris bisa langsung diunduh lewat /download-file/<file_id>."""
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({FILES_SELECT})", {"me": agent["id"]}
        ).fetchone()["n"]
        rows = conn.execute(
            FILES_SELECT + " ORDER BY fl.uploaded_at DESC, fl.id DESC LIMIT :limit OFFSET :offset",
            {"me": agent["id"], "limit": limit, "offset": offset},
        ).fetchall()
    finally:
        conn.close()
    return {
        "status": "ok",
        "data": {
            "files": [_file_out(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        },
    }


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
