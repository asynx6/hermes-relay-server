"""CLI admin relay server.

Pakai:
    python scripts/manage.py list-agents
    python scripts/manage.py reset-token <name>
    python scripts/manage.py revoke <name>
    python scripts/manage.py suspend <name>
    python scripts/manage.py reactivate <name>
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import connect
from app.security import api_error, generate_secret, hash_secret


def list_agents() -> None:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT agent_id, name, status, last_seen_at, created_at FROM agents ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("Belum ada agent terdaftar.")
        return
    for r in rows:
        last_seen = r["last_seen_at"] or "-"
        print(f"{r['agent_id']:<16} {r['name']:<20} {r['status']:<10} last_seen={last_seen}")


def reset_token(name: str) -> None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row is None:
            raise api_error(404, "NOT_FOUND", f"Agent '{name}' tidak ditemukan.")
        secret = generate_secret()
        conn.execute(
            "UPDATE agents SET secret_hash = ? WHERE id = ?", (hash_secret(secret), row["id"])
        )
        conn.commit()
    finally:
        conn.close()
    print(f"Secret baru untuk '{name}' (tampil sekali saja):\n{secret}")


def set_status(name: str, new_status: str) -> None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row is None:
            raise api_error(404, "NOT_FOUND", f"Agent '{name}' tidak ditemukan.")
        conn.execute(
            "UPDATE agents SET status = ? WHERE id = ?", (new_status, row["id"])
        )
        conn.commit()
    finally:
        conn.close()
    print(f"Agent '{name}' -> {new_status}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Admin CLI relay server")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-agents", help="Tampilkan semua agent")

    p_reset = sub.add_parser("reset-token", help="Buat secret baru untuk agent")
    p_reset.add_argument("name")

    p_revoke = sub.add_parser("revoke", help="Cabut akses agent")
    p_revoke.add_argument("name")

    p_suspend = sub.add_parser("suspend", help="Suspend agent sementara")
    p_suspend.add_argument("name")

    p_react = sub.add_parser("reactivate", help="Aktifkan kembali agent")
    p_react.add_argument("name")

    args = parser.parse_args()

    if args.command == "list-agents":
        list_agents()
    elif args.command == "reset-token":
        reset_token(args.name)
    elif args.command == "revoke":
        set_status(args.name, "revoked")
    elif args.command == "suspend":
        set_status(args.name, "suspended")
    elif args.command == "reactivate":
        set_status(args.name, "active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
