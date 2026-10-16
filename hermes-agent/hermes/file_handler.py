"""Streaming file downloads to downloads/ with SHA-256 verification."""
import hashlib
import os
import re
import unicodedata
from pathlib import Path

CHUNK = 64 * 1024
SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# Failures worth retrying in the next polling cycle.
RETRYABLE = (ConnectionError, TimeoutError)


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
        """Path in downloads/; if the name already exists, append a numeric suffix."""
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
        row_id: int | None = None,
    ) -> Path:
        """Core of the download: stream to disk, verify size + SHA-256,
        record files_history (a new row, or update the `row_id` row if
        this is a retry).

        Transient failures (network) are recorded as `pending` so they are
        retried in the next cycle; permanent failures (hash/size mismatch)
        are recorded as `failed`. Partial files are always deleted.
        """
        original = safe_local_name(original)
        if row_id is None:
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
        except RETRYABLE:
            dest.unlink(missing_ok=True)
            memory.update_file_status(row_id, "pending")  # retry later
            raise
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
        """Download a file from an incoming message (called by the poller)."""
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
        """Download a file directly by file_id (from the /files listing)."""
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

    def retry_pending(self, api, memory) -> int:
        """Retry downloads that are pending (status pending)."""
        recovered = 0
        for row in memory.pending_downloads():
            try:
                self._save_stream(
                    api,
                    file_id=row["file_id"],
                    original=row["original_name"],
                    size_expected=row["size_bytes"],
                    sha_expected=row["sha256"],
                    scope=row["scope"],
                    peer=row["peer"],
                    memory=memory,
                    row_id=row["id"],  # update the same row
                )
                recovered += 1
            except Exception as exc:
                print(f"Unduhan {row['file_id']} masih gagal: {exc}", flush=True)
        return recovered
