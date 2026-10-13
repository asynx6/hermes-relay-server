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

    def _unique_dest(self, original: str) -> Path:
        """Path di downloads/; kalau nama sudah ada, tambah akhiran angka."""
        dest = self.download_dir / original
        n = 1
        while dest.exists():
            stem, ext = os.path.splitext(original)
            dest = self.download_dir / f"{stem}_{n}{ext}"
            n += 1
        return dest

    def _save_stream(
        self, api, file_id: str, original: str, size_expected: int | None,
        sha_expected: str | None, scope: str, peer: str, memory,
    ) -> Path:
        """Inti unduhan: stream ke disk, verifikasi ukuran + SHA-256,
        catat files_history. File parsial selalu dihapus kalau gagal."""
        original = safe_local_name(original)
        row_id = memory.record_file(
            file_id=file_id,
            direction="download",
            scope=scope,
            peer=peer,
            original_name=original,
            size_bytes=size_expected,
        )

        dest = self._unique_dest(original)
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

        expected_sha = sha_expected or resp.headers.get("X-File-SHA256")
        actual_sha = digest.hexdigest()
        if expected_sha and actual_sha != expected_sha:
            dest.unlink(missing_ok=True)
            memory.update_file_status(row_id, "failed")
            raise RuntimeError(
                f"SHA-256 tidak cocok untuk {original}: {actual_sha} != {expected_sha}"
            )
        if size_expected and size != size_expected:
            dest.unlink(missing_ok=True)
            memory.update_file_status(row_id, "failed")
            raise RuntimeError(f"Ukuran file tidak cocok untuk {original}")

        memory.update_file_status(row_id, "done", local_path=str(dest), sha256=actual_sha)
        return dest

    def download(self, api, msg: dict, memory) -> Path:
        """Unduh file dari pesan masuk (dipanggil poller)."""
        info = msg["file"]
        return self._save_stream(
            api,
            file_id=info["file_id"],
            original=info["original_name"],
            size_expected=info.get("size_bytes"),
            sha_expected=info.get("sha256"),
            scope=msg["scope"],
            peer=msg["group"] or msg["from"],
            memory=memory,
        )

    def download_by_id(self, api, file_meta: dict, memory) -> Path:
        """Unduh file langsung dengan file_id (dari daftar /files)."""
        return self._save_stream(
            api,
            file_id=file_meta["file_id"],
            original=file_meta["original_name"],
            size_expected=file_meta.get("size_bytes"),
            sha_expected=file_meta.get("sha256"),
            scope=file_meta.get("scope", "direct"),
            peer=file_meta.get("target") or file_meta.get("uploader") or "?",
            memory=memory,
        )
