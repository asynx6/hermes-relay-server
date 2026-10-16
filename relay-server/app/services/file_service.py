"""File storage: validation, staging + atomic commit, download authorization.

The upload flow is designed for many parallel agents:

1. The upload is streamed to a **staging** file (`filemanager/.staging/`)
   outside the database transaction, while computing the real size and
   SHA-256. The DB lock is not held during the network transfer — only for
   the last few moments.
2. Once the transfer is done, a short transaction (BEGIN IMMEDIATE) does:
   duplicate `client_msg_id` check -> target resolution -> quota check ->
   `os.replace` (atomic) of the staging file to the final name
   `<uuid4>.<ext>` -> INSERT `file_logs` + `messages` -> COMMIT.
3. Every failure discards the staging file; no partial file is ever visible
   at the final location. A process that dies between the rename and the
   commit may leave an orphan file — swept by `scripts/cleanup.py`.

A client-provided filename never becomes a path on disk: files are stored as
`<uuid4>.<ext>`, and the original name lives only in the `original_name` column.
"""
import hashlib
import os
import re
import unicodedata
import uuid
from pathlib import Path

from fastapi import UploadFile

from ..config import settings
from ..security import api_error

CHUNK = 64 * 1024
SNIFF_BYTES = 32 * 1024

SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
STAGING_DIRNAME = ".staging"

# ---------------------------------------------------------------------------
# Type detection from magic bytes (magic key, mime, needs rejection)
# ---------------------------------------------------------------------------

MAGIC_SIGNATURES: list[tuple[bytes, str, bool]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", False),
    (b"\xff\xd8\xff", "image/jpeg", False),
    (b"GIF87a", "image/gif", False),
    (b"GIF89a", "image/gif", False),
    (b"%PDF-", "application/pdf", False),
    (b"PK\x03\x04", "application/zip", False),      # also docx/xlsx/pptx/jar
    (b"PK\x05\x06", "application/zip", False),
    (b"PK\x07\x08", "application/zip", False),
    (b"\x1f\x8b", "application/gzip", False),
    (b"Rar!", "application/vnd.rar", False),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed", False),
    (b"ID3", "audio/mpeg", False),
    (b"fLaC", "audio/flac", False),
    (b"OggS", "application/ogg", False),
    (b"RIFF", "audio/wav", False),                   # riff container; AVI is riff too
    (b"\x1aE\xdf\xa3", "video/webm", False),         # matroska/webm
    (b"SQLite format 3\x00", "application/vnd.sqlite3", False),
    (b"\x00asm", "application/wasm", False),
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage", False),  # legacy doc/xls
    (b"\xef\xbb\xbf", "text/plain", False),          # UTF-8 BOM
    # Executable payloads: rejected even if the extension looks safe.
    (b"MZ", "application/x-dosexec", True),
    (b"\x7fELF", "application/x-executable", True),
    (b"\xfe\xed\xfa\xce", "application/x-mach-binary", True),
    (b"\xfe\xed\xfa\xcf", "application/x-mach-binary", True),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-binary", True),
    (b"\xca\xfe\xba\xbe", "application/java-vm", True),  # java class / mach-o fat
    (b"#!", "text/x-shellscript", True),             # shebang script
    (b"<%", "application/x-asp", True),              # ASP
    (b"<?php", "application/x-php", True),           # PHP
]

EXT_TO_MIME = {
    ".txt": "text/plain", ".csv": "text/csv", ".md": "text/markdown",
    ".json": "application/json", ".xml": "application/xml", ".html": "text/html",
    ".log": "text/plain", ".yml": "application/yaml", ".yaml": "application/yaml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".pdf": "application/pdf", ".zip": "application/zip",
    ".gz": "application/gzip", ".tar": "application/x-tar",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".mp4": "video/mp4",
    ".webm": "video/webm", ".db": "application/vnd.sqlite3",
}


def sanitize_filename(name: str) -> str:
    """The original name is for display only. A client name never becomes a path on disk."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = os.path.basename(name.replace("\\", "/"))
    name = SAFE_CHARS.sub("_", name).lstrip(".")
    return name[:100] or "file"


def safe_extension(original: str) -> str:
    """The extension is already sanitized; reject if it is in the blocklist."""
    ext = os.path.splitext(sanitize_filename(original))[1].lower()
    if ext in settings.blocked_ext_set:
        raise api_error(415, "UNSUPPORTED_TYPE", f"Ekstensi '{ext}' diblokir server.")
    return ext


def sniff_content(head: bytes) -> tuple[str | None, bool, str]:
    """Detect the type from magic bytes. Return (mime, is_executable, note).

    Order matters: the magic list is checked FIRST, and the ISO-BMFF container
    (`....ftyp`, used by MP4/MOV) is only checked if no executable signature
    matches. If the order were reversed, a `MZ....ftyp...` file could pass as
    a video.
    """
    if not head:
        return None, False, "empty"
    for magic, mime, is_exec in MAGIC_SIGNATURES:
        if head.startswith(magic):
            return mime, is_exec, "magic"
    if head[4:8] == b"ftyp":
        return "video/mp4", False, "isobmff"
    # Text (all bytes printable / whitespace)?
    if all(b < 128 and (b in (9, 10, 13) or 32 <= b < 127) for b in head):
        return "text/plain", False, "text"
    return None, False, "unknown"


class UploadRejected(Exception):
    """Upload rejected by validation; carries the HTTP code and structured error code."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def staging_path() -> Path:
    """Unique staging path per upload."""
    staging = settings.filemanager_dir / STAGING_DIRNAME
    staging.mkdir(parents=True, exist_ok=True)
    return staging / f"{uuid.uuid4().hex}.part"


async def stage_upload(upload: UploadFile, dest: Path, max_bytes: int) -> dict:
    """Stream the upload to staging while computing the real size + SHA-256.

    Reject: exceeds the size limit (413), empty file (400), executable
    payload (415). The staging file is always discarded on any failure.
    """
    size = 0
    digest = hashlib.sha256()
    first_chunk: bytes | None = None
    try:
        with dest.open("wb") as out:
            while chunk := await upload.read(CHUNK):
                if first_chunk is None:
                    first_chunk = chunk[:SNIFF_BYTES]
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected(
                        413, "FILE_TOO_LARGE", "Melebihi batas ukuran unggahan."
                    )
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    if size == 0:
        dest.unlink(missing_ok=True)
        raise UploadRejected(400, "EMPTY_FILE", "File kosong tidak diterima.")

    declared = (upload.content_type or "").split(";")[0].strip().lower()
    detected, is_executable, how = sniff_content(first_chunk or b"")
    if is_executable:
        dest.unlink(missing_ok=True)
        raise UploadRejected(
            415,
            "EXECUTABLE_CONTENT",
            "Isi file terdeteksi sebagai program/eksekusi; ditolak server.",
        )
    if settings.strict_mime_match and detected and how == "magic":
        expected = EXT_TO_MIME.get(os.path.splitext(upload.filename or "")[1].lower())
        if expected and expected != detected:
            dest.unlink(missing_ok=True)
            raise UploadRejected(
                415,
                "MIME_MISMATCH",
                f"Ekstensi mengarah ke {expected} tetapi isi terdeteksi {detected}.",
            )
    mime = resolve_mime(detected, declared, upload.filename or "")
    return {
        "size": size,
        "sha256": digest.hexdigest(),
        "declared_mime": declared or None,
        "detected_mime": detected,
        "mime": mime,
    }


def resolve_mime(detected: str | None, declared: str | None, filename: str) -> str:
    """Determine the final mime.

    - A specific binary detection (e.g. image/png, application/pdf) is most trusted.
    - Generic "text/plain" detection: the extension map wins if consistent
      (csv, md, json, xml...), because magic bytes cannot tell them apart.
    - Not detected: trust the client's declaration, fall back to octet-stream.
    """
    if detected and detected != "text/plain":
        return detected
    ext = os.path.splitext(sanitize_filename(filename))[1].lower()
    if detected == "text/plain" and ext in EXT_TO_MIME:
        return EXT_TO_MIME[ext]
    if declared:
        return declared
    return detected or "application/octet-stream"


def _unique_final_path(ext: str, conn) -> Path:
    """Final path `<uuid4>.<ext>` guaranteed unique on disk and in file_logs."""
    fm_root = settings.filemanager_dir.resolve()
    for _ in range(10):
        candidate = (fm_root / f"{uuid.uuid4().hex}{ext}").resolve()
        if fm_root not in candidate.parents:
            raise api_error(400, "BAD_REQUEST", "Path traversal terdeteksi.")
        if candidate.exists():
            continue
        row = conn.execute(
            "SELECT 1 FROM file_logs WHERE stored_name = ?", (candidate.name,)
        ).fetchone()
        if row:
            continue
        return candidate
    raise UploadRejected(500, "INTERNAL_ERROR", "Gagal menentukan nama penyimpanan unik.")


def finalize(staging: Path, ext: str, conn) -> Path:
    """Move staging to the final name `<uuid4>.<ext>` atomically.

    The final name is guaranteed unique: it is looped until it exists neither
    on disk nor in the file_logs table, so two uploads with the same original
    name cannot overwrite each other. `os.replace` is atomic on the same
    filesystem, so readers never see a half-written file.
    """
    candidate = _unique_final_path(ext, conn)
    os.replace(staging, candidate)
    return candidate


def dedupe_source(conn, agent_row_id: int, sha256: str, size_bytes: int) -> dict | None:
    """Find a file with identical content that this agent **may already see**.

    Dedupe is limited to files actually visible to the caller (own uploads,
    direct deliveries, or a group it is a member of). Without this limit, an
    agent could guess the content of someone else's file just by asking about
    its hash.

    Return a dict `{stored_name, file_id, mime_type}` if found, else None.
    """
    row = conn.execute(
        """SELECT fl.stored_name, fl.file_id, fl.mime_type
           FROM file_logs fl
           WHERE fl.sha256 = :sha AND fl.size_bytes = :size AND fl.deleted_at IS NULL
             AND (
                   fl.uploader_id = :me
                   OR fl.recipient_id = :me
                   OR EXISTS (
                       SELECT 1 FROM group_members gm
                       WHERE gm.group_id = fl.group_id AND gm.agent_id = :me
                         AND fl.uploaded_at >= gm.joined_at
                   )
                 )
           ORDER BY fl.id LIMIT 1""",
        {"sha": sha256, "size": size_bytes, "me": agent_row_id},
    ).fetchone()
    if row is None:
        return None
    if not (settings.filemanager_dir / row["stored_name"]).is_file():
        return None
    return {
        "stored_name": row["stored_name"],
        "file_id": row["file_id"],
        "mime_type": row["mime_type"],
    }


def link_blob(source_name: str, ext: str, conn) -> Path:
    """Create a new final name pointing to the same blob content.

    Uses a hardlink: two `file_logs` rows with identical content share one
    inode, so disk space is not used twice and is only truly freed when the
    last link is removed. If the filesystem does not support hardlinks, fall
    back to copying the file.
    """
    source = settings.filemanager_dir / source_name
    candidate = _unique_final_path(ext, conn)
    try:
        os.link(source, candidate)
    except OSError:
        import shutil

        shutil.copyfile(source, candidate)
    return candidate


def discard(path: Path | None) -> None:
    """Discard a leftover staging file (e.g. duplicate request)."""
    if path:
        Path(path).unlink(missing_ok=True)


def new_file_id(conn) -> str:
    """Unused public file ID (`fl_<10 hex>`)."""
    import secrets as _secrets

    while True:
        candidate = "fl_" + _secrets.token_hex(5)
        if not conn.execute(
            "SELECT 1 FROM file_logs WHERE file_id = ?", (candidate,)
        ).fetchone():
            return candidate


def check_download_permission(conn, file_row, agent_row_id: int) -> None:
    """Only the uploader, the direct recipient, or a member of the target
    group may download. A group member may only download files uploaded
    AFTER they joined — the same rule as the group message filter."""
    if file_row is None:
        raise api_error(404, "NOT_FOUND", "File tidak ditemukan.")
    if file_row["deleted_at"]:
        raise api_error(410, "GONE", "File sudah dihapus oleh retention policy.")
    allowed = file_row["uploader_id"] == agent_row_id or file_row["recipient_id"] == agent_row_id
    if not allowed and file_row["group_id"] is not None:
        member = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND agent_id = ? "
            "AND ? >= joined_at",
            (file_row["group_id"], agent_row_id, file_row["uploaded_at"]),
        ).fetchone()
        allowed = member is not None
    if not allowed:
        raise api_error(403, "FORBIDDEN", "Tidak berhak mengunduh file ini.")
