"""Siapkan database: buat kalau belum ada, naikkan skema kalau sudah ada.

Aman dijalankan berulang dan tidak pernah menghapus data. Untuk kontrol lebih
halus (dry-run, berhenti di versi tertentu, path backup), pakai
`python scripts/migrate.py`.

Usage:
    python scripts/init_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import init_db


def main() -> int:
    applied = init_db(log=lambda m: print(m, flush=True))
    if applied:
        print(f"{len(applied)} migrasi diterapkan.")
    else:
        print("Skema sudah terbaru.")
    settings.filemanager_dir.mkdir(parents=True, exist_ok=True)
    print(f"Database siap di {settings.db_path}")
    print(f"Filemanager siap di {settings.filemanager_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
