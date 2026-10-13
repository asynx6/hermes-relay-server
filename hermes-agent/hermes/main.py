"""CLI Hermes Agent.

Subperintah:
    (tanpa argumen)   jalankan loop polling
    send <target> <pesan>
    send-group <grup> <pesan>
    send-file <target> <path> [caption]
    create-group <nama> [deskripsi]
    join-group <nama>
    history [n]

Pertama kali dijalankan tanpa secret_key di config.json, agent otomatis
memanggil /register dan menyimpan hasilnya.
"""
import argparse
import signal
import sys
import uuid
from pathlib import Path

from .api_client import RelayAPI
from .config import ClientConfig
from .file_handler import FileHandler
from .memory import Memory
from .poller import Poller

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def ensure_registered(config: ClientConfig) -> None:
    """Auto-register kalau secret belum ada di config."""
    if config.registered:
        return
    if not config.registration_key:
        raise SystemExit(
            "config.json belum berisi secret_key maupun registration_key. "
            "Isi server_url, registration_key, dan agent_name lebih dulu."
        )
    api = RelayAPI(config)
    data = api.register()
    config.save_registration(data["secret_key"])
    print(f"Registrasi berhasil sebagai '{data['name']}' ({data['agent_id']}).")


def open_memory(args) -> Memory:
    return Memory(
        str(Path(args.config).resolve().parent / "data" / "agent_memory.db"),
        agent_name=ClientConfig(args.config).agent_name,
    )


def send_and_record(api: RelayAPI, memory: Memory, scope: str, peer: str,
                    content: str, msg_type: str = "text",
                    file_id: str | None = None) -> dict:
    """Simpan ke outbox, kirim, lalu tandai terkirim. Aman untuk retry."""
    client_msg_id = str(uuid.uuid4())
    memory.save_outgoing(scope, peer, content, client_msg_id, msg_type, file_id)
    data = api.send_message(
        "group" if scope == "group" else "direct", peer, content, client_msg_id
    )
    row = memory.conn.execute(
        "SELECT id FROM conversations WHERE client_msg_id = ?", (client_msg_id,)
    ).fetchone()
    memory.mark_sent(row["id"], data["message_id"])
    return data


def cmd_send(config, api, memory, args) -> int:
    data = send_and_record(api, memory, "direct", args.target, " ".join(args.message))
    print(f"Terkirim (message_id={data['message_id']}, duplicate={data['duplicate']})")
    return 0


def cmd_send_group(config, api, memory, args) -> int:
    data = send_and_record(api, memory, "group", args.group, " ".join(args.message))
    print(f"Terkirim ke grup (message_id={data['message_id']}, duplicate={data['duplicate']})")
    return 0


def cmd_send_file(config, api, memory, file_handler, args) -> int:
    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"File tidak ditemukan: {path}")
    caption = " ".join(args.caption) if args.caption else ""
    client_msg_id = str(uuid.uuid4())
    data = api.upload_file(str(path), "direct", args.target, client_msg_id, caption)
    # Pesan file tersimpan sebagai pesan bertipe file dengan caption.
    memory.save_outgoing(
        "direct", args.target, caption or f"[file {path.name}]",
        client_msg_id, msg_type="text", file_id=data["file_id"],
    )
    row = memory.conn.execute(
        "SELECT id FROM conversations WHERE client_msg_id = ?", (client_msg_id,)
    ).fetchone()
    memory.mark_sent(row["id"], data["message_id"])
    upload_row = memory.record_file(
        file_id=data["file_id"], direction="upload", scope="direct",
        peer=args.target, original_name=path.name, size_bytes=data["size_bytes"],
    )
    memory.update_file_status(
        upload_row, "done", local_path=str(path), sha256=data["sha256"]
    )
    print(f"File terkirim (file_id={data['file_id']}, message_id={data['message_id']})")
    return 0


def cmd_create_group(config, api, memory, args) -> int:
    data = api.create_group(args.name, " ".join(args.description) if args.description else None)
    print(f"Grup dibuat: {data['name']} (id={data['group_id']})")
    return 0


def cmd_join_group(config, api, memory, args) -> int:
    data = api.join_group(args.group)
    print(f"Bergabung ke '{data['group']}' sejak {data['joined_at']}")
    return 0


def cmd_history(config, api, memory, args) -> int:
    rows = memory.recent_conversations(limit=args.n)
    if not rows:
        print("Belum ada percakapan.")
        return 0
    for r in reversed(rows):
        who = "kita" if r["direction"] == "out" else r["sender"]
        print(f"[{r['created_at']}] {who} ({r['scope']} {r['peer']}) [{r['status']}]: {r['content']}")
    return 0


def cmd_poll(config, api, memory, file_handler) -> int:
    poller = Poller(
        api, memory, file_handler,
        poll_min=config.poll_min_seconds, poll_max=config.poll_max_seconds,
    )

    def handle_signal(signum, frame):
        poller.stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)

    print(
        f"Polling dimulai sebagai '{config.agent_name}' ke {config.server_url} "
        f"(interval {config.poll_min_seconds}-{config.poll_max_seconds}s). "
        "Ctrl+C untuk berhenti.",
        flush=True,
    )
    try:
        poller.run()
    except KeyboardInterrupt:
        poller.stop_event.set()
        print("\nPolling dihentikan.", flush=True)
    return 2 if poller.auth_rejected else 0


def cmd_fetch_once(config, api, memory, file_handler) -> int:
    """Satu siklus polling lalu keluar. Dibuat untuk dipanggil cron.

    Exit code: 0 = server merespons, 1 = server tidak merespons,
    2 = token ditolak (perlu perhatian manusia).
    """
    poller = Poller(
        api, memory, file_handler,
        poll_min=config.poll_min_seconds, poll_max=config.poll_max_seconds,
    )
    reachable, _ = poller.poll_once()
    if not reachable:
        return 2 if poller.auth_rejected else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Agent client")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="path config.json")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("poll", help="jalankan loop polling (default)")

    sub.add_parser(
        "fetch-once",
        help="satu siklus polling lalu keluar (untuk cron; exit 0=ok, 1=server mati, 2=token ditolak)",
    )

    p_send = sub.add_parser("send", help="kirim pesan direct")
    p_send.add_argument("target")
    p_send.add_argument("message", nargs="+")

    p_sg = sub.add_parser("send-group", help="kirim pesan ke grup")
    p_sg.add_argument("group")
    p_sg.add_argument("message", nargs="+")

    p_sf = sub.add_parser("send-file", help="kirim file direct")
    p_sf.add_argument("target")
    p_sf.add_argument("path")
    p_sf.add_argument("caption", nargs="*")

    p_cg = sub.add_parser("create-group", help="buat grup baru")
    p_cg.add_argument("name")
    p_cg.add_argument("description", nargs="*")

    p_jg = sub.add_parser("join-group", help="gabung ke grup")
    p_jg.add_argument("group")

    p_h = sub.add_parser("history", help="tampilkan riwayat lokal")
    p_h.add_argument("n", type=int, nargs="?", default=20)

    args = parser.parse_args(argv)
    command = args.command or "poll"

    config = ClientConfig(args.config)
    ensure_registered(config)
    memory = open_memory(args)
    api = RelayAPI(config)
    file_handler = FileHandler(config.download_dir)

    if command == "poll":
        return cmd_poll(config, api, memory, file_handler)
    if command == "fetch-once":
        return cmd_fetch_once(config, api, memory, file_handler)
    if command == "send":
        return cmd_send(config, api, memory, args)
    if command == "send-group":
        return cmd_send_group(config, api, memory, args)
    if command == "send-file":
        return cmd_send_file(config, api, memory, file_handler, args)
    if command == "create-group":
        return cmd_create_group(config, api, memory, args)
    if command == "join-group":
        return cmd_join_group(config, api, memory, args)
    if command == "history":
        return cmd_history(config, api, memory, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
