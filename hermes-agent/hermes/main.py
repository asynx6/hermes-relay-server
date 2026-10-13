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


def resolve_agent(api, name: str) -> tuple[str, str]:
    """Nama agent -> (agent_id, nama). Error jelas kalau belum terdaftar."""
    for a in api.list_agents():
        if a["name"].lower() == name.lower():
            return a["agent_id"], a["name"]
    raise SystemExit(
        f"Agent '{name}' tidak terdaftar. Jalankan 'agents' untuk melihat daftar."
    )


def resolve_group(api, name: str) -> tuple[str, str]:
    """Nama grup -> (group_id, nama). Error jelas kalau grup tidak ada."""
    for g in api.list_groups():
        if g["name"].lower() == name.lower():
            return g["group_id"], g["name"]
    raise SystemExit(
        f"Grup '{name}' tidak ditemukan. Jalankan 'groups' untuk melihat daftar."
    )


def send_and_record(api: RelayAPI, memory: Memory, scope: str, peer: str,
                    peer_id: str, content: str, msg_type: str = "text",
                    file_id: str | None = None) -> dict:
    """Simpan ke outbox (dengan ID peer), kirim, lalu tandai terkirim.
    Aman untuk retry karena client_msg_id idempotent."""
    client_msg_id = str(uuid.uuid4())
    memory.save_outgoing(scope, peer, peer_id, content, client_msg_id, msg_type, file_id)
    data = api.send_message(
        "group" if scope == "group" else "direct",
        target_id=peer_id,
        target_name=peer,
        content=content,
        client_msg_id=client_msg_id,
    )
    row = memory.conn.execute(
        "SELECT id FROM conversations WHERE client_msg_id = ?", (client_msg_id,)
    ).fetchone()
    memory.mark_sent(row["id"], data["message_id"])
    return data


def cmd_send(config, api, memory, args) -> int:
    target_id, target_name = resolve_agent(api, args.target)
    data = send_and_record(api, memory, "direct", target_name, target_id, " ".join(args.message))
    print(f"Terkirim ke {target_name} ({target_id}) — message_id={data['message_id']}, duplicate={data['duplicate']}")
    return 0


def cmd_send_group(config, api, memory, args) -> int:
    group_id, group_name = resolve_group(api, args.group)
    data = send_and_record(api, memory, "group", group_name, group_id, " ".join(args.message))
    print(f"Terkirim ke grup {group_name} ({group_id}) — message_id={data['message_id']}, duplicate={data['duplicate']}")
    return 0


def cmd_send_file(config, api, memory, file_handler, args) -> int:
    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"File tidak ditemukan: {path}")
    target_id, target_name = resolve_agent(api, args.target)
    caption = " ".join(args.caption) if args.caption else ""
    client_msg_id = str(uuid.uuid4())
    data = api.upload_file(
        str(path), "direct", target_id, target_name, client_msg_id, caption
    )
    # Pesan file tersimpan sebagai pesan bertipe file dengan caption.
    memory.save_outgoing(
        "direct", target_name, target_id, caption or f"[file {path.name}]",
        client_msg_id, msg_type="text", file_id=data["file_id"],
    )
    row = memory.conn.execute(
        "SELECT id FROM conversations WHERE client_msg_id = ?", (client_msg_id,)
    ).fetchone()
    memory.mark_sent(row["id"], data["message_id"])
    upload_row = memory.record_file(
        file_id=data["file_id"], direction="upload", scope="direct",
        peer=target_name, original_name=path.name, size_bytes=data["size_bytes"],
    )
    memory.update_file_status(
        upload_row, "done", local_path=str(path), sha256=data["sha256"]
    )
    print(f"File terkirim ke {target_name} ({target_id}) — file_id={data['file_id']}, message_id={data['message_id']}")
    return 0


def cmd_create_group(config, api, memory, args) -> int:
    data = api.create_group(args.name, " ".join(args.description) if args.description else None)
    print(f"Grup dibuat: {data['name']} (group_id={data['group_id']})")
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


def cmd_list_agents(config, api, memory, args) -> int:
    """Tampilkan semua Hermes terdaftar (discovery sebelum chat)."""
    agents = api.list_agents()
    if not agents:
        print("Belum ada agent terdaftar.")
        return 0
    for a in agents:
        me = " (kita)" if a["name"] == config.agent_name else ""
        last_seen = a["last_seen_at"] or "-"
        print(f"{a['name']:<24} {a['agent_id']:<16} {a['status']:<10} aktif={last_seen}{me}")
    return 0


def cmd_list_groups(config, api, memory, args) -> int:
    """Tampilkan semua grup + status keanggotaan agent ini."""
    groups = api.list_groups()
    if not groups:
        print("Belum ada grup. Buat dengan: create-group <nama>")
        return 0
    for g in groups:
        status = "anggota" if g["is_member"] else "bukan anggota"
        print(f"{g['group_id']:<14} {g['name']:<24} {g['member_count']} anggota  [{status}]")
    return 0


def fmt_size(n) -> str:
    if n is None:
        return "-"
    units = ["B", "KB", "MB", "GB"]
    i = 0
    x = float(n)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    return f"{x:.0f} {units[i]}" if i == 0 else f"{x:.1f} {units[i]}"


def cmd_list_files(config, api, memory, args) -> int:
    """Tampilkan file yang bisa diakses agent ini (nama, ID, ukuran, dll)."""
    data = api.list_files(limit=args.n)
    files = data["files"]
    if not files:
        print("Tidak ada file yang bisa diakses agent ini.")
        return 0
    print(f"{'NAMA':<28} {'FILE ID':<16} {'UKURAN':>9}  {'DARI':<16} {'TUJUAN':<20} WAKTU")
    print("-" * 108)
    for f in files:
        target = f"grup {f['target']}" if f["scope"] == "group" else f["target"]
        print(
            f"{f['original_name'][:27]:<28} {f['file_id']:<16} {fmt_size(f['size_bytes']):>9}  "
            f"{f['uploader'][:15]:<16} {target[:19]:<20} {f['uploaded_at']}"
        )
    if data["has_more"]:
        print(f"... dan {data['total'] - len(files)} file lain; naikkan batas dengan -n.")
    return 0


def _find_file(api, file_id: str, max_pages: int = 20) -> dict | None:
    """Cari metadata file (nama asli, ukuran, sha256) lewat daftar /files."""
    offset = 0
    for _ in range(max_pages):
        data = api.list_files(limit=50, offset=offset)
        for f in data["files"]:
            if f["file_id"] == file_id:
                return f
        if not data["has_more"]:
            return None
        offset += data["limit"]
    return None


def cmd_download_file(config, api, memory, file_handler, args) -> int:
    """Unduh file langsung dengan file_id sebagai parameter."""
    meta = _find_file(api, args.file_id)
    if meta is None:
        raise SystemExit(
            f"File '{args.file_id}' tidak ada di daftar file yang bisa diakses "
            "agent ini. Jalankan 'files' untuk melihat daftarnya."
        )
    print(
        f"Mengunduh {meta['original_name']} ({fmt_size(meta['size_bytes'])}) "
        f"dari {meta['file_id']}..."
    )
    dest = file_handler.download_by_id(api, meta, memory)
    print(f"Tersimpan: {dest} (SHA-256 cocok)")
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

    sub.add_parser("agents", help="daftar semua Hermes terdaftar (nama + ID)")
    sub.add_parser("groups", help="daftar semua grup + status keanggotaan kita")

    p_files = sub.add_parser("files", help="daftar file yang bisa diakses (nama, ID, ukuran)")
    p_files.add_argument("-n", type=int, default=50, help="jumlah baris (default 50, maks 200)")

    p_dl = sub.add_parser("download-file", help="unduh file langsung dengan file_id")
    p_dl.add_argument("file_id")

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
    if command == "agents":
        return cmd_list_agents(config, api, memory, args)
    if command == "groups":
        return cmd_list_groups(config, api, memory, args)
    if command == "files":
        return cmd_list_files(config, api, memory, args)
    if command == "download-file":
        return cmd_download_file(config, api, memory, file_handler, args)
    if command == "history":
        return cmd_history(config, api, memory, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
