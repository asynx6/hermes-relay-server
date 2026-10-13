"""Retention: hapus pesan dan file lama. Dijalankan via cron.

Contoh cron (tiap hari pukul 03:00):
    0 3 * * * cd /opt/relay-server && . venv/bin/activate && python scripts/cleanup.py

Pakai:
    python scripts/cleanup.py            # pakai RETENTION_DAYS dari .env
    python scripts/cleanup.py --days 14  # override
    python scripts/cleanup.py --dry-run  # hanya tampilkan rencana
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import connect


def cleanup(days: int, dry_run: bool) -> None:
    conn = connect()
    try:
        # File: tandai deleted_at dan hapus fisiknya.
        old_files = conn.execute(
            "SELECT file_id, stored_name FROM file_logs "
            "WHERE deleted_at IS NULL AND uploaded_at < "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{days} days",),
        ).fetchall()
        for f in old_files:
            print(f"file {f['file_id']} ({f['stored_name']}) -> hapus")
            if not dry_run:
                path = settings.filemanager_dir / f["stored_name"]
                path.unlink(missing_ok=True)
                conn.execute(
                    "UPDATE file_logs SET deleted_at = "
                    "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE file_id = ?",
                    (f["file_id"],),
                )

        # Pesan: hapus baris yang lebih tua dari masa retensi.
        old_msgs = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE created_at < "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{days} days",),
        ).fetchone()["n"]
        print(f"{old_msgs} pesan lebih tua dari {days} hari -> hapus")
        if not dry_run:
            conn.execute(
                "DELETE FROM messages WHERE created_at < "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                (f"-{days} days",),
            )
        conn.commit()
    finally:
        conn.close()

    if dry_run:
        print("(dry-run: tidak ada perubahan yang disimpan)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Retention pesan dan file relay")
    parser.add_argument("--days", type=int, default=settings.retention_days)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cleanup(args.days, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
