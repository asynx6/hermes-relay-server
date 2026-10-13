"""Retention + penyapuan: hapus pesan/file lama, staging nyasar, file orphan.

Dijalankan via cron.

Contoh cron (tiap hari pukul 03:00):
    0 3 * * * cd /opt/relay-server && . venv/bin/activate && python scripts/cleanup.py

Pakai:
    python scripts/cleanup.py                 # pakai RETENTION_DAYS dari .env
    python scripts/cleanup.py --days 14       # override masa retensi
    python scripts/cleanup.py --staging-hours 6  # umur staging sebelum disapu
    python scripts/cleanup.py --dry-run       # hanya tampilkan rencana
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import connect

STAGING_DIRNAME = ".staging"


def cleanup(days: int, dry_run: bool, staging_hours: int) -> None:
    conn = connect()
    try:
        # 1) File lama: tandai deleted_at dan hapus fisiknya.
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

        # 2) Pesan lama: hapus barisnya.
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
        print("(dry-run: perubahan database tidak disimpan)")

    # 3) Staging nyasar: proses yang mati di tengah unggahan bisa meninggalkan
    #    file .part. Sapu yang lebih tua dari staging_hours.
    staging_dir = settings.filemanager_dir / STAGING_DIRNAME
    if staging_dir.is_dir():
        cutoff = time.time() - staging_hours * 3600
        for part in staging_dir.glob("*.part"):
            if part.stat().st_mtime < cutoff:
                print(f"staging {part.name} -> hapus (nyasar > {staging_hours} jam)")
                if not dry_run:
                    part.unlink(missing_ok=True)

    # 4) Orphan: file di filemanager yang tidak punya baris file_logs aktif.
    #    Terjadi kalau proses mati di antara rename dan commit (jendela sangat
    #    kecil, tapi tetap perlu disapu agar disk tidak bocor).
    if settings.filemanager_dir.is_dir():
        conn = connect()
        try:
            known = {
                r["stored_name"]
                for r in conn.execute("SELECT stored_name FROM file_logs").fetchall()
            }
        finally:
            conn.close()
        orphans = [
            p
            for p in settings.filemanager_dir.iterdir()
            if p.is_file() and p.name not in known and p.name != ".gitkeep"
        ]
        for p in orphans:
            print(f"orphan {p.name} -> hapus (tanpa baris file_logs)")
            if not dry_run:
                p.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retention pesan/file + sapu staging nyasar dan orphan"
    )
    parser.add_argument("--days", type=int, default=settings.retention_days)
    parser.add_argument("--staging-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cleanup(args.days, args.dry_run, args.staging_hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
