"""Chunked + resumable uploads with dedupe based on SHA-256.

Flow:

    POST /upload/init                  -> open a session, get an upload_id
    PUT  /upload/{id}/chunk?index=N    -> send one chunk (raw body)
    GET  /upload/{id}                  -> status: which chunks are present
    POST /upload/{id}/complete         -> assemble, verify, atomic commit
    DELETE /upload/{id}                -> cancel the session

Why this design:
- **Resumable** — the server records the chunk indexes already received, so a
  client whose connection drops at chunk 7 only needs to send chunks 7..N.
- **Dedupe** — if the server already has a file with the same SHA-256 + size
  (and this agent may see it), `/init` marks `deduplicated` and the client can
  go straight to `/complete` without sending a single byte.
- **Integrity** — chunks are assembled and then verified for total SHA-256 +
  size before the atomic `os.replace`. A bad chunk never becomes the final file.

Chunks are stored in `filemanager/.staging/<upload_id>/chunk_<n>`; abandoned
sessions are swept by `scripts/cleanup.py` after `CHUNK_SESSION_HOURS`.
"""
import hashlib
import math
import secrets
import shutil

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..database import begin_immediate, connect
from ..ratelimit import limiter
from ..schemas import ChunkedUploadInitRequest
from ..security import api_error, require_scope
from ..services import file_service, message_service

router = APIRouter()


# --- session helpers -----------------------------------------------------------


def _session_dir(upload_id: str):
    return settings.filemanager_dir / file_service.STAGING_DIRNAME / upload_id


def _parse_received(raw: str) -> set[int]:
    return {int(x) for x in raw.split(",") if x.strip()}


def _dump_received(indexes: set[int]) -> str:
    return ",".join(str(i) for i in sorted(indexes))


def _load_session(conn, upload_id: str, agent_row_id: int):
    row = conn.execute(
        "SELECT * FROM upload_sessions WHERE upload_id = ?", (upload_id,)
    ).fetchone()
    if row is None:
        raise api_error(404, "NOT_FOUND", f"Sesi unggah '{upload_id}' tidak ditemukan.")
    if row["agent_id"] != agent_row_id:
        raise api_error(403, "FORBIDDEN", "Sesi unggah ini milik agent lain.")
    return row


def _chunk_path(upload_id: str, index: int):
    return _session_dir(upload_id) / f"chunk_{index}"


# --- endpoint --------------------------------------------------------------


@router.post("/upload/init", status_code=201)
@limiter.limit(lambda: settings.rate_limit_default)
def upload_init(
    body: ChunkedUploadInitRequest,
    request: Request,
    agent=Depends(require_scope("files:write")),
):
    """Open a chunked upload session and (if any) flag a duplicate."""
    if body.total_size > settings.max_chunked_bytes:
        raise api_error(
            413,
            "FILE_TOO_LARGE",
            f"Melebihi batas unggahan berpotong ({settings.max_chunked_mb} MB).",
        )
    if body.chunk_size > settings.max_chunk_bytes:
        raise api_error(
            400,
            "BAD_REQUEST",
            f"chunk_size maksimal {settings.chunk_mb} MB.",
        )

    original_name = file_service.sanitize_filename(body.original_name)
    ext = file_service.safe_extension(original_name)  # 415 before accepting chunks

    total_chunks = math.ceil(body.total_size / body.chunk_size)
    if total_chunks > 100_000:
        raise api_error(400, "BAD_REQUEST", "Terlalu banyak chunk.")

    conn = connect()
    try:
        begin_immediate(conn)

        # Idempotency: an open session with the same client_msg_id is returned.
        existing = conn.execute(
            "SELECT * FROM upload_sessions WHERE agent_id = ? AND client_msg_id = ? "
            "AND status = 'open'",
            (agent["id"], body.client_msg_id),
        ).fetchone()
        if existing:
            conn.commit()
            received = _parse_received(existing["received"])
            return {
                "status": "ok",
                "data": {
                    "upload_id": existing["upload_id"],
                    "chunk_size": existing["chunk_size"],
                    "total_chunks": existing["total_chunks"],
                    "received": sorted(received),
                    "deduplicated": False,
                    "resumed_session": True,
                },
            }

        upload_id = "up_" + secrets.token_hex(8)
        source = file_service.dedupe_source(conn, agent["id"], body.sha256, body.total_size)

        conn.execute(
            """INSERT INTO upload_sessions
                 (upload_id, agent_id, original_name, total_size, chunk_size,
                  total_chunks, sha256, target_type, target_id, target_name,
                  client_msg_id, caption)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                upload_id,
                agent["id"],
                original_name,
                body.total_size,
                body.chunk_size,
                total_chunks,
                body.sha256,
                body.target_type,
                body.target_id,
                body.target_name,
                body.client_msg_id,
                body.caption,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    _session_dir(upload_id).mkdir(parents=True, exist_ok=True)
    return {
        "status": "ok",
        "data": {
            "upload_id": upload_id,
            "chunk_size": body.chunk_size,
            "total_chunks": total_chunks,
            "received": [],
            "deduplicated": source is not None,
            "existing_file_id": source["file_id"] if source else None,
            "resumed_session": False,
            "ext": ext,
        },
        "note": (
            "File dengan isi sama sudah ada di server; panggil /complete langsung "
            "tanpa mengirim chunk."
            if source is not None
            else None
        ),
    }


@router.get("/upload/{upload_id}")
@limiter.limit(lambda: settings.rate_limit_default)
def upload_status(
    upload_id: str, request: Request, agent=Depends(require_scope("files:write"))
):
    """Session status: which chunks have been received and which are still missing."""
    conn = connect()
    try:
        row = _load_session(conn, upload_id, agent["id"])
    finally:
        conn.close()
    received = _parse_received(row["received"])
    missing = sorted(set(range(row["total_chunks"])) - received)
    return {
        "status": "ok",
        "data": {
            "upload_id": upload_id,
            "status": row["status"],
            "total_chunks": row["total_chunks"],
            "chunk_size": row["chunk_size"],
            "received": sorted(received),
            "missing": missing,
            "complete": not missing,
        },
    }


@router.put("/upload/{upload_id}/chunk")
@limiter.limit(lambda: settings.rate_limit_default)
async def upload_chunk(
    upload_id: str,
    request: Request,
    index: int = Query(..., ge=0),
    agent=Depends(require_scope("files:write")),
):
    """Receive one chunk (raw body, `application/octet-stream`).

    Safe to repeat: resending the same chunk just overwrites its file.
    """
    conn = connect()
    try:
        row = _load_session(conn, upload_id, agent["id"])
        if row["status"] != "open":
            raise api_error(409, "CONFLICT", "Sesi unggah sudah selesai/dibatalkan.")
        if index >= row["total_chunks"]:
            raise api_error(400, "BAD_REQUEST", f"index di luar rentang (0..{row['total_chunks'] - 1}).")
        chunk_size, total_chunks = row["chunk_size"], row["total_chunks"]
    finally:
        conn.close()

    dest = _chunk_path(upload_id, index)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with dest.open("wb") as out:
            async for block in request.stream():
                size += len(block)
                if size > chunk_size:
                    raise api_error(
                        413, "FILE_TOO_LARGE", "Chunk melebihi ukuran yang dideklarasikan."
                    )
                out.write(block)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise api_error(400, "BAD_REQUEST", "Chunk kosong tidak diterima.")

    conn = connect()
    try:
        begin_immediate(conn)
        row = _load_session(conn, upload_id, agent["id"])
        received = _parse_received(row["received"])
        received.add(index)
        conn.execute(
            "UPDATE upload_sessions SET received = ?, updated_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE upload_id = ?",
            (_dump_received(received), upload_id),
        )
        conn.commit()
    finally:
        conn.close()

    missing = sorted(set(range(total_chunks)) - received)
    return {
        "status": "ok",
        "data": {
            "upload_id": upload_id,
            "received_count": len(received),
            "total_chunks": total_chunks,
            "missing": missing,
            "complete": not missing,
        },
    }


def _assemble(upload_id: str, total_chunks: int, declared_sha: str, declared_size: int):
    """Assemble chunks into a single staging file, computing size + SHA-256 along the way."""
    session_dir = _session_dir(upload_id)
    part = session_dir / "assembled.part"
    digest = hashlib.sha256()
    size = 0
    first_chunk: bytes | None = None
    with part.open("wb") as out:
        for i in range(total_chunks):
            chunk = session_dir / f"chunk_{i}"
            if not chunk.is_file():
                part.unlink(missing_ok=True)
                raise api_error(409, "CONFLICT", f"Chunk {i} belum diterima.")
            with chunk.open("rb") as f:
                while block := f.read(file_service.CHUNK):
                    if first_chunk is None:
                        first_chunk = block[: file_service.SNIFF_BYTES]
                    size += len(block)
                    digest.update(block)
                    out.write(block)
    if size != declared_size:
        part.unlink(missing_ok=True)
        raise api_error(
            400,
            "SIZE_MISMATCH",
            f"Ukuran hasil rakit {size} byte tidak sama dengan deklarasi {declared_size}.",
        )
    if digest.hexdigest() != declared_sha:
        part.unlink(missing_ok=True)
        raise api_error(400, "CHECKSUM_MISMATCH", "SHA-256 hasil rakit tidak cocok.")
    return part, first_chunk or b""


@router.post("/upload/{upload_id}/complete")
@limiter.limit(lambda: settings.rate_limit_default)
def upload_complete(
    upload_id: str, request: Request, agent=Depends(require_scope("files:write"))
):
    """Assemble chunks, verify, then commit as a file + message."""
    conn = connect()
    try:
        row = _load_session(conn, upload_id, agent["id"])
    finally:
        conn.close()
    if row["status"] != "open":
        raise api_error(409, "CONFLICT", "Sesi unggah sudah selesai/dibatalkan.")

    original_name = row["original_name"]
    ext = file_service.safe_extension(original_name)
    received = _parse_received(row["received"])
    missing = sorted(set(range(row["total_chunks"])) - received)

    # Dedupe path: the server already has identical content -> no chunks needed.
    conn = connect()
    try:
        dedupe = file_service.dedupe_source(
            conn, agent["id"], row["sha256"], row["total_size"]
        )
    finally:
        conn.close()

    if dedupe is None and missing:
        raise api_error(
            409, "CONFLICT", f"Masih ada {len(missing)} chunk belum diterima."
        )

    staging_part = None
    if dedupe is None:
        staging_part, first_bytes = _assemble(
            upload_id, row["total_chunks"], row["sha256"], row["total_size"]
        )
        detected, is_executable, how = file_service.sniff_content(first_bytes)
        if is_executable:
            staging_part.unlink(missing_ok=True)
            raise api_error(
                415, "EXECUTABLE_CONTENT",
                "Isi file terdeteksi sebagai program/eksekusi; ditolak server.",
            )
        if settings.strict_mime_match and detected and how == "magic":
            expected = file_service.EXT_TO_MIME.get(ext)
            if expected and expected != detected:
                staging_part.unlink(missing_ok=True)
                raise api_error(
                    415, "MIME_MISMATCH",
                    f"Ekstensi mengarah ke {expected} tetapi isi terdeteksi {detected}.",
                )
        mime = file_service.resolve_mime(detected, row["mime_type"], original_name)
    else:
        # Identical content already exists: use the mime already stored for that file.
        mime = dedupe["mime_type"] or "application/octet-stream"

    conn = connect()
    final_path = None
    committed = False
    try:
        begin_immediate(conn)

        # Duplicate client_msg_id: reply with the old message (idempotent).
        existing = conn.execute(
            """SELECT fl.*, m.id AS message_id FROM file_logs fl
               JOIN messages m ON m.file_id = fl.file_id
               WHERE fl.uploader_id = ? AND m.client_msg_id = ?""",
            (agent["id"], row["client_msg_id"]),
        ).fetchone()
        if existing:
            conn.commit()
            _cleanup_session(upload_id)
            return JSONResponse(
                status_code=200,
                content={
                    "status": "ok",
                    "data": {
                        "file_id": existing["file_id"],
                        "message_id": existing["message_id"],
                        "original_name": existing["original_name"],
                        "size_bytes": existing["size_bytes"],
                        "sha256": existing["sha256"],
                        "deduplicated": False,
                        "duplicate": True,
                    },
                },
            )

        if row["target_type"] == "direct":
            if row["target_name"].strip().lower() == agent["name"].lower():
                raise api_error(400, "BAD_REQUEST", "Tidak bisa mengirim file ke diri sendiri.")
            recipient = message_service.resolve_direct_target(
                conn, row["target_id"], row["target_name"]
            )
            recipient_id, group_id = recipient["id"], None
        else:
            group = message_service.resolve_group_target(
                conn, row["target_id"], row["target_name"]
            )
            message_service.ensure_group_member(conn, group["id"], agent["id"])
            recipient_id, group_id = None, group["id"]

        if settings.max_storage_mb_per_agent > 0:
            used = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS used FROM file_logs "
                "WHERE uploader_id = ? AND deleted_at IS NULL",
                (agent["id"],),
            ).fetchone()["used"]
            if used + row["total_size"] > settings.max_storage_mb_per_agent * 1024 * 1024:
                raise api_error(
                    413, "QUOTA_EXCEEDED",
                    f"Kuota penyimpanan {settings.max_storage_mb_per_agent} MB per agent terlampaui.",
                )

        used_dedupe = dedupe is not None
        if used_dedupe:
            dest = file_service.link_blob(dedupe["stored_name"], ext, conn)
        else:
            dest = file_service.finalize(staging_part, ext, conn)
        final_path = dest

        file_id = file_service.new_file_id(conn)
        conn.execute(
            """INSERT INTO file_logs
                 (file_id, original_name, stored_name, mime_type, size_bytes, sha256,
                  uploader_id, recipient_id, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_id,
                original_name,
                dest.name,
                mime,
                row["total_size"],
                row["sha256"],
                agent["id"],
                recipient_id,
                group_id,
            ),
        )
        cur = conn.execute(
            """INSERT INTO messages
                 (client_msg_id, sender_id, recipient_id, group_id, msg_type, content, file_id)
               VALUES (?, ?, ?, ?, 'file', ?, ?)""",
            (row["client_msg_id"], agent["id"], recipient_id, group_id, row["caption"], file_id),
        )
        message_id = cur.lastrowid
        conn.execute(
            "UPDATE upload_sessions SET status = 'completed', file_id = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE upload_id = ?",
            (file_id, upload_id),
        )
        uploaded_at = conn.execute(
            "SELECT uploaded_at FROM file_logs WHERE file_id = ?", (file_id,)
        ).fetchone()["uploaded_at"]
        conn.commit()
        committed = True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
        if not committed:
            if staging_part is not None:
                file_service.discard(staging_part)
            if final_path is not None:
                final_path.unlink(missing_ok=True)

    _cleanup_session(upload_id)
    return JSONResponse(
        status_code=201,
        content={
            "status": "ok",
            "data": {
                "file_id": file_id,
                "message_id": message_id,
                "original_name": original_name,
                "size_bytes": row["total_size"],
                "mime_type": mime,
                "sha256": row["sha256"],
                "created_at": uploaded_at,
                "deduplicated": dedupe is not None,
                "duplicate": False,
            },
        },
    )


@router.delete("/upload/{upload_id}")
@limiter.limit(lambda: settings.rate_limit_default)
def upload_abort(
    upload_id: str, request: Request, agent=Depends(require_scope("files:write"))
):
    """Cancel the upload session and discard the chunks already received."""
    conn = connect()
    try:
        begin_immediate(conn)
        row = _load_session(conn, upload_id, agent["id"])
        if row["status"] == "open":
            conn.execute(
                "UPDATE upload_sessions SET status = 'aborted', updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE upload_id = ?",
                (upload_id,),
            )
        conn.commit()
    finally:
        conn.close()
    _cleanup_session(upload_id)
    return {"status": "ok", "data": {"upload_id": upload_id, "aborted": True}}


def _cleanup_session(upload_id: str) -> None:
    """Discard the session chunk directory (best-effort)."""
    shutil.rmtree(_session_dir(upload_id), ignore_errors=True)
