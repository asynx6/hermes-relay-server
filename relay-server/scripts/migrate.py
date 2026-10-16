"""Migrasi database: lihat status, terapkan yang tertunda, cetak skema.

Migrasi berjalan otomatis saat server start (AUTO_MIGRATE=true). Script ini
untuk menjalankannya manual — mis. saat AUTO_MIGRATE=false, atau sebelum
deploy supaya backup diambil lebih dulu.

Pakai:
    python scripts/migrate.py status                 # apa yang sudah/akan jalan
    python scripts/migrate.py up                     # terapkan (backup dulu)
    python scripts/migrate.py up --dry-run           # hanya tampilkan rencana
    python scripts/migrate.py up --to 2              # berhenti di versi 2
    python scripts/migrate.py up --no-backup         # tanpa snapshot
    python scripts/migrate.py up --backup-path b.db  # snapshot ke path tertentu
    python scripts/migrate.py schema                 # cetak DDL hasil rantai

Data tidak pernah dihapus: setiap migrasi diterapkan satu kali, dicatat di
tabel `schema_migrations`, dan dijalankan dalam satu transaksi. Rollback =
kembalikan file backup.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import migrations  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import connect  # noqa: E402


def _fmt_applied(row) -> str:
    duration = row["duration_ms"]
    timing = f"{duration} ms" if duration is not None else "-"
    return f"  v{row['version']:04d}  {row['name']:<28} {row['applied_at']}  {timing}"


def cmd_status(_args) -> int:
    conn = connect()
    try:
        info = migrations.status(conn)
    finally:
        conn.close()

    print(f"Database : {settings.db_path}")
    print(f"Versi    : {info['current']} dari {info['target']}")

    if info["applied"]:
        print("\nSudah diterapkan:")
        for row in info["applied"]:
            print(_fmt_applied(row))
    else:
        print("\nBelum ada migrasi yang diterapkan.")

    if info["pending"]:
        print("\nTertunda:")
        for migration in info["pending"]:
            print(f"  v{migration.version:04d}  {migration.name}")
    else:
        print("\nTidak ada migrasi tertunda.")

    if info["drifted"]:
        print("\nBERMASALAH - file migrasi diubah setelah diterapkan:")
        for migration, recorded in info["drifted"]:
            print(f"  v{migration.version:04d}  {migration.name}  (tercatat {recorded[:8]})")
        print("  Kembalikan file aslinya, atau pulihkan dari backup sebelum migrasi.")

    if info["orphaned"]:
        print("\nBERMASALAH - tercatat tapi filenya tidak ada:")
        for row in info["orphaned"]:
            print(f"  v{row['version']:04d}  {row['name']}")

    return 1 if (info["drifted"] or info["orphaned"]) else 0


def cmd_up(args) -> int:
    conn = connect()
    try:
        problems = migrations.drifted(conn)
        if problems:
            print("Dibatalkan: ada file migrasi yang diubah setelah diterapkan.")
            for migration, recorded in problems:
                print(f"  v{migration.version:04d}  {migration.name}  (tercatat {recorded[:8]})")
            return 1

        todo = migrations.pending(conn)
        if args.to is not None:
            todo = [m for m in todo if m.version <= args.to]

        if not todo:
            print("Skema sudah terbaru. Tidak ada yang dikerjakan.")
            return 0

        print(f"Akan diterapkan ({len(todo)}):")
        for migration in todo:
            print(f"  v{migration.version:04d}  {migration.name}")

        if args.dry_run:
            print("\n(dry-run: tidak ada perubahan yang disimpan)")
            return 0

        if not args.no_backup:
            dest = migrations.backup(Path(args.backup_path) if args.backup_path else None)
            print(f"\nBackup: {dest}")

        print()
        migrations.apply(conn, todo, log=lambda m: print(m, flush=True))
    except migrations.MigrationError as exc:
        print(f"Gagal: {exc}")
        return 1
    except Exception as exc:
        print(f"Gagal: {exc!r}")
        print("Database tidak berubah sebagian: migrasi yang gagal sudah di-rollback.")
        return 1
    finally:
        conn.close()

    print("\nSelesai.")
    return 0


def cmd_schema(_args) -> int:
    print(migrations.dump_schema(), end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrasi database Hermes Relay Server"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="tampilkan migrasi yang sudah dan akan diterapkan")

    p_up = sub.add_parser("up", help="terapkan migrasi yang tertunda")
    p_up.add_argument("--dry-run", action="store_true", help="hanya tampilkan rencana")
    p_up.add_argument("--to", type=int, default=None, help="berhenti di versi ini")
    p_up.add_argument("--no-backup", action="store_true", help="lewati snapshot sebelum migrasi")
    p_up.add_argument("--backup-path", default=None, help="path snapshot (default: data/backups/)")

    sub.add_parser("schema", help="cetak DDL lengkap hasil rantai migrasi")

    args = parser.parse_args()
    command = args.command or "status"
    if command == "status":
        return cmd_status(args)
    if command == "up":
        return cmd_up(args)
    if command == "schema":
        return cmd_schema(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
