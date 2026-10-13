"""Inisialisasi database relay dari schema.sql.

Pakai:
    python scripts/init_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import init_db


def main() -> int:
    init_db()
    settings.filemanager_dir.mkdir(parents=True, exist_ok=True)
    print(f"Database siap di {settings.db_path}")
    print(f"Filemanager siap di {settings.filemanager_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
