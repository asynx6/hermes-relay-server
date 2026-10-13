"""Penyimpanan file: sanitasi nama, streaming + SHA-256, otorisasi unduhan."""
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

SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_filename(name: str) -> str:
    """Nama asli hanya untuk tampilan. Nama dari client tidak pernah jadi path di disk."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = os.path.basename(name.replace("\\", "/"))
    name = SAFE_CHARS.sub("_", name).lstrip(".")
    return name[:100] or "file"


def stored_path(original: str) -> Path:
    """Path acak <uuid4>.<ext> di dalam filemanager_dir."""
    ext = os.path.splitext(sanitize_filename(original))[1].lower()
    if ext in settings.blocked_ext_set:
        raise api_error(415, "UNSUPPORTED_TYPE", f"Ekstensi '{ext}' diblokir.")
    path = (settings.filemanager_dir / f"{uuid.uuid4().hex}{ext}").resolve()
    if settings.filemanager_dir.resolve() not in path.parents:
        raise api_error(400, "BAD_REQUEST", "Path traversal terdeteksi.")
    return path


async def save_upload(upload: UploadFile, dest: Path, max_bytes: int) -> tuple[int, str]:
    """Simpan unggahan per chunk sambil menghitung SHA-256. Batas ukuran dari byte nyata."""
    size = 0
    digest = hashlib.sha256()
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as out:
            while chunk := await upload.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise api_error(413, "FILE_TOO_LARGE", "Melebihi batas ukuran unggahan.")
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()


def check_download_permission(conn, file_row, agent_row_id: int) -> None:
    """Hanya uploader, penerima direct, atau anggota grup tujuan yang boleh mengunduh."""
    if file_row is None:
        raise api_error(404, "NOT_FOUND", "File tidak ditemukan.")
    if file_row["deleted_at"]:
        raise api_error(410, "GONE", "File sudah dihapus oleh retention policy.")
    allowed = file_row["uploader_id"] == agent_row_id or file_row["recipient_id"] == agent_row_id
    if not allowed and file_row["group_id"] is not None:
        member = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND agent_id = ?",
            (file_row["group_id"], agent_row_id),
        ).fetchone()
        allowed = member is not None
    if not allowed:
        raise api_error(403, "FORBIDDEN", "Tidak berhak mengunduh file ini.")
