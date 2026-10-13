"""Unduhan file streaming ke downloads/ dengan verifikasi SHA-256."""
import hashlib
import os
import re
import unicodedata
from pathlib import Path

CHUNK = 64 * 1024
SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def safe_local_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = os.path.basename(name.replace("\\", "/"))
    name = SAFE_CHARS.sub("_", name).lstrip(".")
    return name[:100] or "file"


class FileHandler:
    def __init__(self, download_dir: str):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def download(self, api, msg: dict, memory) -> Path:
        """Unduh file dari pesan, verifikasi SHA-256, catat files_history.

        File parsial dihapus kalau unduhan terputus. Status gagal dicatat
        supaya bisa dicoba lagi di siklus berikutnya.
        """
        info = msg["file"]
        file_id = info["file_id"]
        original = safe_local_name(info["original_name"])
        peer = msg["group"] or msg["from"]
        row_id = memory.record_file(
            file_id=file_id,
            direction="download",
            scope=msg["scope"],
            peer=peer,
            original_name=original,
            size_bytes=info["size_bytes"],
        )

        # Kalau nama sudah ada, tambahkan akhiran angka supaya tidak menimpa.
        dest = self.download_dir / original
        n = 1
        while dest.exists():
            stem, ext = os.path.splitext(original)
            dest = self.download_dir / f"{stem}_{n}{ext}"
            n += 1

        digest = hashlib.sha256()
        size = 0
        try:
            resp = api.download_file(file_id)
            with resp, dest.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=CHUNK):
                    if chunk:
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
        except Exception:
            dest.unlink(missing_ok=True)
            memory.update_file_status(row_id, "failed")
            raise

        expected = info.get("sha256") or resp.headers.get("X-File-SHA256")
        actual = digest.hexdigest()
        if expected and actual != expected:
            dest.unlink(missing_ok=True)
            memory.update_file_status(row_id, "failed")
            raise RuntimeError(
                f"SHA-256 tidak cocok untuk {original}: {actual} != {expected}"
            )
        if info.get("size_bytes") and size != info["size_bytes"]:
            dest.unlink(missing_ok=True)
            memory.update_file_status(row_id, "failed")
            raise RuntimeError(f"Ukuran file tidak cocok untuk {original}")

        memory.update_file_status(row_id, "done", local_path=str(dest), sha256=actual)
        return dest
