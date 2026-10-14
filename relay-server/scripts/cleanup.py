"""Retention + penyapuan: hapus pesan/file lama, staging nyasar, file orphan.

Dijalankan via cron.

Contoh cron (tiap hari pukul 03:00):
    0 3 * * * cd /opt/relay-server && . venv/bin/activate && python scripts/cleanup.py

Pakai:
    python scripts/cleanup.py                 # pakai RETENTION_DAYS dari .env
    python scripts/cleanup.py --days 14       # override masa retensi
    python scripts/cleanup.py --staging-hours 6  # umur staging sebelum disapu
    python scripts/cleanup.py --orphan-min-age-hours 1  # jangan sapu orphan muda
    python scripts/cleanup.py --dry-run       # hanya tampilkan rencana
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import connect
from app.services import audit_service

STAGING_DIRNAME = ".staging"
OLD_SQL = "created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)"


def cleanup(days: int, dry_run: bool, staging_hours: int, orphan_min_age_hours: int) -> None:
    conn = connect()
    try:
        # 1) Referensi reply_to ke pesan lama harus dilepas dulu; kalau tidak,
        #    DELETE gagal karena foreign_keys=ON dan transaksi rollback
        #    SETELAH file fisik terhapus (DB dan disk jadi tidak sinkron).
        old_ids = f"SELECT id FROM messages WHERE {OLD_SQL}"
        n_refs = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE reply_to IN ({old_ids})",
            (f"-{days} days",),
        ).fetchone()[0]
        if n_refs:
            print(f"{n_refs} referensi reply_to ke pesan lama -> lepas")
            if not dry_run:
                conn.execute(
                    f"UPDATE messages SET reply_to = NULL WHERE reply_to IN ({old_ids})",
                    (f"-{days} days",),
                )
                conn.commit()

        # 2) File lama: kumpulkan, commit penanda deleted_at, BARU hapus fisik.
        old_files = conn.execute(
            "SELECT file_id, stored_name FROM file_logs "
            "WHERE deleted_at IS NULL AND uploaded_at < "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{days} days",),
        ).fetchall()
        for f in old_files:
            print(f"file {f['file_id']} ({f['stored_name']}) -> hapus")
        if not dry_run and old_files:
            conn.execute(
                "UPDATE file_logs SET deleted_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE deleted_at IS NULL AND uploaded_at < "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                (f"-{days} days",),
            )
            conn.commit()  # kalau langkah fisik gagal, DB tetap konsisten
            for f in old_files:
                (settings.filemanager_dir / f["stored_name"]).unlink(missing_ok=True)
                print(f"  fisik {f['stored_name']} dihapus")

        # 3) Pesan lama: hapus barisnya.
        old_msgs = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {OLD_SQL}", (f"-{days} days",)
        ).fetchone()[0]
        print(f"{old_msgs} pesan lebih tua dari {days} hari -> hapus")
        if not dry_run:
            conn.execute(
                f"DELETE FROM messages WHERE {OLD_SQL}", (f"-{days} days",)
            )
        conn.commit()
    finally:
        conn.close()

    if dry_run:
        print("(dry-run: perubahan database tidak disimpan)")

    # 4) Staging nyasar: proses mati di tengah unggahan meninggalkan .part.
    staging_dir = settings.filemanager_dir / STAGING_DIRNAME
    if staging_dir.is_dir():
        cutoff = time.time() - staging_hours * 3600
        for part in staging_dir.glob("*.part"):
            if part.stat().st_mtime < cutoff:
                print(f"staging {part.name} -> hapus (nyasar > {staging_hours} jam)")
                if not dry_run:
                    part.unlink(missing_ok=True)

    # 5) Orphan: file tanpa baris file_logs. Umur minimum supaya file yang
    #    SEDANG di-upload (rename sudah terjadi, commit belum) tidak tersapu.
    if settings.filemanager_dir.is_dir():
        conn = connect()
        try:
            known = {
                r["stored_name"]
                for r in conn.execute("SELECT stored_name FROM file_logs").fetchall()
            }
        finally:
            conn.close()
        age_cutoff = time.time() - orphan_min_age_hours * 3600
        for p in settings.filemanager_dir.iterdir():
            if not p.is_file() or p.name == ".gitkeep" or p.name in known:
                continue
            if p.stat().st_mtime >= age_cutoff:
                continue  # masih muda: mungkin sedang di-commit
            print(f"orphan {p.name} -> hapus (tanpa baris file_logs)")
            if not dry_run:
                p.unlink(missing_ok=True)

    # 6) Sesi unggah berpotong yang ditinggalkan: tandai aborted + buang chunk.
    stale_ids: list[str] = []
    conn = connect()
    try:
        stale = conn.execute(
            "SELECT upload_id FROM upload_sessions WHERE status = 'open' AND "
            "updated_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{settings.chunk_session_hours} hours",),
        ).fetchall()
        stale_ids = [r["upload_id"] for r in stale]
        for uid in stale_ids:
            print(f"sesi unggah {uid} -> batalkan (nyasar > {settings.chunk_session_hours} jam)")
        if stale_ids and not dry_run:
            conn.executemany(
                "UPDATE upload_sessions SET status = 'aborted' WHERE upload_id = ?",
                [(u,) for u in stale_ids],
            )
            conn.commit()

        # 6b) Direktori chunk tanpa sesi aktif (sisa crash setelah init).
        if staging_dir.is_dir():
            active = {
                r["upload_id"]
                for r in conn.execute(
                    "SELECT upload_id FROM upload_sessions WHERE status = 'open'"
                ).fetchall()
            }
        else:
            active = set()
    finally:
        conn.close()

    if not dry_run:
        for uid in stale_ids:
            shutil.rmtree(staging_dir / uid, ignore_errors=True)
        if staging_dir.is_dir():
            for d in staging_dir.iterdir():
                if d.is_dir() and d.name not in active:
                    print(f"staging sesi {d.name} -> hapus (tanpa sesi aktif)")
                    shutil.rmtree(d, ignore_errors=True)

    # 7) Pangkas audit log yang lebih tua dari retensi.
    conn = connect()
    try:
        if dry_run:
            n_old = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE at < "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                (f"-{settings.audit_log_retention_days} days",),
            ).fetchone()[0]
            print(f"{n_old} baris audit lebih tua dari {settings.audit_log_retention_days} hari -> pangkas")
        else:
            n = audit_service.prune(conn, settings.audit_log_retention_days)
            conn.commit()
            print(f"{n} baris audit lama dipangkas")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retention pesan/file + sapu staging nyasar dan orphan"
    )
    parser.add_argument("--days", type=int, default=settings.retention_days)
    parser.add_argument("--staging-hours", type=int, default=24)
    parser.add_argument(
        "--orphan-min-age-hours",
        type=int,
        default=1,
        help="jangan sapu orphan yang lebih muda dari ini (default 1 jam)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cleanup(args.days, args.dry_run, args.staging_hours, args.orphan_min_age_hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
