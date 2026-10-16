"""Hermes Agent CLI.

Subcommands:
    (no argument)     run the polling loop
    poll              polling loop (same as default)
    fetch-once        one polling cycle then exit (for cron)
    agents            list all registered Hermes agents (name + ID)
    groups            list all groups + our membership status
    files [-n 50]     list accessible files (name, ID, size)
    download-file <file_id>   download a file directly by ID
    send <target> <message...>
    send-group <group> <message...>
    send-file <target> <path> [caption...]
    send-task <target> <payload...>   send a task (RPC), wait for the result
    receipts <id,id,...>              check the receipt status of our messages
    create-group <name> [description]
    join-group <name>
    history [n]

On first run without a secret_key in config.json, the agent automatically
calls /register (with an idempotency nonce) and saves the result.
The polling loop and fetch-once use a file lock so they do not run twice
concurrently for the same agent directory.
"""
import argparse
import signal
import sys
import time
import uuid
from pathlib import Path

from .api_client import RelayAPI
from .config import ClientConfig
from .file_handler import FileHandler
from .instance_lock import instance_lock
from .memory import Memory
from .poller import Poller
from .safe_text import safe_text

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def lock_path(args) -> Path:
    """File lock for the same config directory."""
    return Path(args.config).resolve().parent / "data" / "hermes.lock"


def warn_insecure_transport(config: ClientConfig) -> None:
    url = config.server_url.lower()
    if url.startswith("http://") and "127.0.0.1" not in url and "localhost" not in url:
        print(
            "PERINGATAN: server_url bukan HTTPS — secret_key dikirim sebagai "
            "header dan bisa disadap. Pakai https:// untuk server jarak jauh.",
            flush=True,
        )


def ensure_registered(config: ClientConfig) -> None:
    """Auto-register if the secret is not yet in config.

    Sends a persistent `registration_nonce`: if the 201 response is lost
    due to a timeout, the next attempt replays the same secret instead of
    getting a 409 and burning the agent name.
    """
    if config.registered:
        return
    if not config.registration_key:
        raise SystemExit(
            "config.json belum berisi secret_key maupun registration_key. "
            "Isi server_url, registration_key, dan agent_name lebih dulu."
        )
    nonce = config.registration_nonce or str(uuid.uuid4())
    api = RelayAPI(config)
    data = api.register(registration_nonce=nonce)
    config.save_registration(data["secret_key"], registration_nonce=nonce)
    print(
        f"Registrasi berhasil sebagai '{data['name']}' ({data['agent_id']})"
        + (" [replay]" if data.get("replayed") else "") + "."
    )


def open_memory(args) -> Memory:
    return Memory(
        str(Path(args.config).resolve().parent / "data" / "agent_memory.db"),
        agent_name=ClientConfig(args.config).agent_name,
    )


def resolve_agent(api, name: str) -> tuple[str, str]:
    """Agent name -> (agent_id, name). Clear error if not registered."""
    for a in api.list_agents():
        if a["name"].lower() == name.lower():
            return a["agent_id"], a["name"]
    raise SystemExit(
        f"Agent '{name}' tidak terdaftar. Jalankan 'agents' untuk melihat daftar."
    )


def resolve_group(api, name: str) -> tuple[str, str]:
    """Group name -> (group_id, name). Clear error if the group does not exist."""
    for g in api.list_groups():
        if g["name"].lower() == name.lower():
            return g["group_id"], g["name"]
    raise SystemExit(
        f"Grup '{name}' tidak ditemukan. Jalankan 'groups' untuk melihat daftar."
    )


def send_and_record(api: RelayAPI, memory: Memory, scope: str, peer: str,
                    peer_id: str, content: str, msg_type: str = "text",
                    file_id: str | None = None) -> dict:
    """Save to the outbox (with the peer ID), send, then mark as sent.
    Safe to retry because client_msg_id is idempotent."""
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
    scope = "group" if getattr(args, "group", False) else "direct"
    if scope == "group":
        target_id, target_name = resolve_group(api, args.target)
    else:
        target_id, target_name = resolve_agent(api, args.target)
    caption = " ".join(args.caption) if args.caption else ""
    client_msg_id = str(uuid.uuid4())
    data = api.upload_file(
        str(path), scope, target_id, target_name, client_msg_id, caption
    )
    # The file message is stored as a file-type message with a caption.
    memory.save_outgoing(
        scope, target_name, target_id, caption or f"[file {path.name}]",
        client_msg_id, msg_type="text", file_id=data["file_id"],
    )
    row = memory.conn.execute(
        "SELECT id FROM conversations WHERE client_msg_id = ?", (client_msg_id,)
    ).fetchone()
    memory.mark_sent(row["id"], data["message_id"])
    upload_row = memory.record_file(
        file_id=data["file_id"], direction="upload", scope=scope,
        peer=target_name, original_name=path.name, size_bytes=data["size_bytes"],
    )
    memory.update_file_status(
        upload_row, "done", local_path=str(path), sha256=data["sha256"]
    )
    where = f"grup {target_name}" if scope == "group" else target_name
    extra = " (dedupe: isi sama sudah ada)" if data.get("deduplicated") else ""
    print(
        f"File terkirim ke {where} ({target_id}) — file_id={data['file_id']}, "
        f"message_id={data['message_id']}{extra}"
    )
    return 0


def cmd_upload_big(config, api, memory, args) -> int:
    """Upload a large file in chunks (chunked) and resumably."""
    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"File tidak ditemukan: {path}")
    if args.target_type == "group":
        target_id, target_name = resolve_group(api, args.target)
    else:
        target_id, target_name = resolve_agent(api, args.target)
    caption = " ".join(args.caption) if args.caption else ""
    client_msg_id = str(uuid.uuid4())
    size = path.stat().st_size

    def progress(done, total):
        print(f"  chunk {done}/{total} terkirim", flush=True)

    print(f"Mengunggah {path.name} ({size} byte) ke {target_name}...", flush=True)
    data = api.chunked_upload(
        str(path), args.target_type, target_id, target_name, client_msg_id,
        caption, chunk_mb=args.chunk_mb, on_progress=progress,
    )
    scope = args.target_type
    memory.save_outgoing(
        scope, target_name, target_id, caption or f"[file {path.name}]",
        client_msg_id, msg_type="text", file_id=data["file_id"],
    )
    row = memory.conn.execute(
        "SELECT id FROM conversations WHERE client_msg_id = ?", (client_msg_id,)
    ).fetchone()
    memory.mark_sent(row["id"], data["message_id"])
    upload_row = memory.record_file(
        file_id=data["file_id"], direction="upload", scope=scope,
        peer=target_name, original_name=path.name, size_bytes=data["size_bytes"],
    )
    memory.update_file_status(
        upload_row, "done", local_path=str(path), sha256=data["sha256"]
    )
    extra = " (dedupe: 0 byte terkirim)" if data.get("deduplicated") else ""
    print(
        f"Selesai: file_id={data['file_id']}, message_id={data['message_id']}{extra}"
    )
    return 0


def cmd_create_group(config, api, memory, args) -> int:
    data = api.create_group(args.name, " ".join(args.description) if args.description else None)
    print(f"Grup dibuat: {data['name']} (group_id={data['group_id']}, role={data['role']})")
    return 0


def cmd_join_group(config, api, memory, args) -> int:
    data = api.join_group(args.group, getattr(args, "code", None))
    state = "sudah anggota" if data.get("already_member") else "bergabung"
    print(f"{data['group']}: {state} (role={data.get('role')}, sejak {data.get('joined_at')})")
    return 0


def cmd_leave_group(config, api, memory, args) -> int:
    api.leave_group(args.group)
    print(f"Keluar dari grup '{args.group}'.")
    return 0


def cmd_group_members(config, api, memory, args) -> int:
    data = api.list_members(args.group)
    print(f"Anggota grup '{data['group']}' ({len(data['members'])}):")
    for m in data["members"]:
        print(f"  {m['name']:<24} {m['role']:<8} sejak {m['joined_at']}")
    return 0


def cmd_group_kick(config, api, memory, args) -> int:
    api.kick_member(args.group, args.member)
    print(f"'{args.member}' dikeluarkan dari grup '{args.group}'.")
    return 0


def cmd_group_role(config, api, memory, args) -> int:
    api.set_role(args.group, args.member, args.role)
    print(f"Role '{args.member}' di grup '{args.group}' -> {args.role}.")
    return 0


def cmd_group_join_code(config, api, memory, args) -> int:
    if args.clear:
        api.clear_join_code(args.group)
        print(f"join_code grup '{args.group}' dihapus (grup terbuka).")
    else:
        data = api.set_join_code(args.group, args.code)
        print(f"join_code grup '{args.group}': {data['join_code']}")
    return 0


def cmd_tokens(config, api, memory, args) -> int:
    tokens = api.list_tokens()
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps(tokens, indent=2, ensure_ascii=False))
        return 0
    if not tokens:
        print("Belum ada token sekunder. Buat dengan: token-create <scope...>")
        return 0
    print(f"{'TOKEN ID':<14} {'STATUS':<9} {'SCOPE':<40} LABEL")
    for t in tokens:
        scopes = ",".join(t["scopes"])
        print(f"{t['token_id']:<14} {t['status']:<9} {scopes:<40} {t.get('label') or '-'}")
    return 0


def cmd_token_create(config, api, memory, args) -> int:
    data = api.create_token(args.scopes, args.label, args.days)
    print(f"Token dibuat: {data['token_id']} scope={','.join(data['scopes'])}")
    print(f"secret_key: {data['secret_key']}")
    print("Simpan sekarang — nilai ini tidak ditampilkan lagi.")
    return 0


def cmd_token_revoke(config, api, memory, args) -> int:
    api.revoke_token(args.token_id)
    print(f"Token '{args.token_id}' dicabut.")
    return 0


def cmd_rotate_token(config, api, memory, args) -> int:
    data = api.rotate_token()
    # Save automatically to config so the client immediately uses the new key.
    config.save_registration(data["secret_key"])
    print(
        f"Kunci diputar. secret_key baru disimpan ke config.json. "
        f"Kunci lama berlaku {data['grace_minutes']} menit lagi."
    )
    return 0


def cmd_history(config, api, memory, args) -> int:
    rows = memory.recent_conversations(limit=args.n)
    if not rows:
        print("Belum ada percakapan.")
        return 0
    for r in reversed(rows):
        who = "kita" if r["direction"] == "out" else r["sender"]
        print(
            f"[{r['created_at']}] {safe_text(who, 64)} "
            f"({safe_text(r['scope'], 16)} {safe_text(r['peer'], 64)}) "
            f"[{r['status']}]: {safe_text(r['content'])}"
        )
    return 0


def cmd_list_agents(config, api, memory, args) -> int:
    """Show all registered Hermes agents (discovery before chatting)."""
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
    """Show all groups + this agent's membership status."""
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
    """Show files this agent may access (name, ID, size, etc.)."""
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
    """Find file metadata (original name, size, sha256) via the /files listing."""
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
    """Download a file directly with file_id as a parameter."""
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


def _transport(config, args) -> dict:
    """Transport options: SSE > long polling > plain polling.

    The CLI (`--sse`/`--wait`) overrides the config values (`use_sse`/`poll_wait_seconds`).
    """
    use_sse = bool(getattr(args, "sse", False)) or bool(getattr(config, "use_sse", False))
    cli_wait = getattr(args, "wait", None)
    if cli_wait is None:
        cli_wait = getattr(config, "poll_wait_seconds", 0)
    wait = 0 if use_sse else max(0, int(cli_wait or 0))
    return {"wait_seconds": wait, "use_sse": use_sse}


def build_poller(config, api, memory, file_handler, *, wait_seconds: int = 0,
                 use_sse: bool = False) -> Poller:
    """Bentuk Poller lengkap dengan opsi transport.

    Satu titik konstruksi untuk `poll` dan `fetch-once`, supaya keduanya tidak
    bisa berbeda soal interval maupun transport.
    """
    return Poller(
        api, memory, file_handler,
        poll_min=config.poll_min_seconds, poll_max=config.poll_max_seconds,
        wait_seconds=wait_seconds, use_sse=use_sse,
    )


def cmd_poll(config, api, memory, file_handler, args) -> int:
    transport = _transport(config, args)
    poller = build_poller(
        config, api, memory, file_handler,
        wait_seconds=transport["wait_seconds"], use_sse=transport["use_sse"],
    )

    def handle_signal(signum, frame):
        poller.stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)

    if transport["use_sse"]:
        mode = "SSE (satu koneksi, server mendorong pesan)"
    elif transport["wait_seconds"]:
        mode = f"long polling (request ditahan sampai {transport['wait_seconds']}s)"
    else:
        mode = f"polling adaptif (interval {config.poll_min_seconds}-{config.poll_max_seconds}s)"
    print(
        f"Mulai sebagai '{config.agent_name}' ke {config.server_url} — {mode}. "
        "Ctrl+C untuk berhenti.",
        flush=True,
    )
    # One instance per agent directory: prevents poll and cron fetch-once
    # from running concurrently with the same cursor.
    with instance_lock(lock_path(args)):
        try:
            poller.run()
        except KeyboardInterrupt:
            poller.stop_event.set()
            print("\nDihentikan.", flush=True)
    return 2 if poller.auth_rejected else 0


def cmd_fetch_once(config, api, memory, file_handler, args) -> int:
    """One polling cycle then exit. Made to be invoked by cron.

    Exit code: 0 = server responded, 1 = server did not respond,
    2 = token rejected (needs human attention).
    """
    transport = _transport(config, args)
    poller = build_poller(
        config, api, memory, file_handler,
        wait_seconds=transport["wait_seconds"],
    )
    with instance_lock(lock_path(args)):
        reachable, _ = poller.poll_once()
    if not reachable:
        return 2 if poller.auth_rejected else 1
    return 0


def cmd_send_task(config, api, memory, args) -> int:
    """Send a task (msg_type=task) then wait for the result (msg_type=result).

    Lightweight RPC pattern: a single task_id links the request and the reply.
    The CLI waits for the result until timeout while polling periodically.
    """
    target_id, target_name = resolve_agent(api, args.target)
    task_id = args.task_id or f"task_{uuid.uuid4().hex[:16]}"
    payload = " ".join(args.payload)

    client_msg_id = str(uuid.uuid4())
    memory.save_outgoing(
        "direct", target_name, target_id, payload, client_msg_id,
        msg_type="task",
    )
    data = api.send_message(
        "direct", target_id, target_name, payload, client_msg_id,
        msg_type="task", task_id=task_id, task_status="pending",
    )
    row = memory.conn.execute(
        "SELECT id FROM conversations WHERE client_msg_id = ?", (client_msg_id,)
    ).fetchone()
    memory.mark_sent(row["id"], data["message_id"])
    print(f"Tugas {task_id} dikirim ke {target_name} (message_id={data['message_id']}).")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(args.interval)
        try:
            task = api.get_task(task_id)
        except Exception as exc:
            print(f"Belum ada hasil ({exc}); coba lagi...", flush=True)
            continue
        results = [m for m in task["messages"] if m["msg_type"] == "result"]
        if results:
            latest = results[-1]
            print(
                f"Hasil dari {safe_text(latest['from'], 64)} "
                f"({safe_text(latest.get('task_status'), 32)}):"
            )
            print(safe_text(latest["content"]))
            return 0
    print(f"Timeout {args.timeout}s: belum ada hasil untuk {task_id}.")
    return 1


def cmd_receipts(config, api, memory, args) -> int:
    """Check the receipt status of the messages we sent."""
    ids = [int(x) for x in args.message_ids.split(",") if x.strip()]
    receipts = api.receipts(ids)
    if not receipts:
        print("Belum ada konfirmasi penerimaan untuk pesan tersebut.")
        return 0
    for r in receipts:
        for seen in r["seen_by"]:
            print(f"pesan #{r['message_id']}: {seen['agent']} = {seen['status']} ({seen['at']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Agent client")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="path config.json")
    sub = parser.add_subparsers(dest="command")

    p_poll = sub.add_parser("poll", help="jalankan loop pengambilan pesan (default)")
    p_poll.add_argument(
        "--wait", type=int, default=None,
        help="long polling: tahan request sampai N detik (mis. 25)",
    )
    p_poll.add_argument("--sse", action="store_true", help="pakai SSE (satu koneksi)")

    p_once = sub.add_parser(
        "fetch-once",
        help="satu siklus lalu keluar (untuk cron; exit 0=ok, 1=server mati, 2=token ditolak)",
    )
    p_once.add_argument("--wait", type=int, default=None, help="long polling sampai N detik")

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

    p_st = sub.add_parser("send-task", help="kirim tugas (RPC) dan tunggu hasil")
    p_st.add_argument("target")
    p_st.add_argument("payload", nargs="+")
    p_st.add_argument("--task-id", default=None, help="task_id manual (opsional)")
    p_st.add_argument("--timeout", type=int, default=60)
    p_st.add_argument("--interval", type=float, default=2.0)

    p_rc = sub.add_parser("receipts", help="cek status penerimaan pesan kita")
    p_rc.add_argument("message_ids", help="message_id dipisah koma, mis. 12,13")

    p_sf = sub.add_parser("send-file", help="kirim file direct (atau ke grup dengan --group)")
    p_sf.add_argument("target")
    p_sf.add_argument("path")
    p_sf.add_argument("caption", nargs="*")
    p_sf.add_argument("--group", action="store_true", help="target adalah nama grup")

    p_cg = sub.add_parser("create-group", help="buat grup baru")
    p_cg.add_argument("name")
    p_cg.add_argument("description", nargs="*")

    p_jg = sub.add_parser("join-group", help="gabung ke grup (pakai join_code kalau perlu)")
    p_jg.add_argument("group")
    p_jg.add_argument("--code", default=None, help="join_code grup")

    p_lg = sub.add_parser("leave-group", help="keluar dari grup")
    p_lg.add_argument("group")

    p_gm = sub.add_parser("group-members", help="daftar anggota grup + role")
    p_gm.add_argument("group")

    p_gk = sub.add_parser("group-kick", help="keluarkan anggota grup (admin)")
    p_gk.add_argument("group")
    p_gk.add_argument("member")

    p_gr = sub.add_parser("group-role", help="ubah role anggota (admin)")
    p_gr.add_argument("group")
    p_gr.add_argument("member")
    p_gr.add_argument("role", choices=["admin", "member"])

    p_gj = sub.add_parser("group-join-code", help="atur/hapus join_code grup (admin)")
    p_gj.add_argument("group")
    p_gj.add_argument("--code", default=None, help="kode baru (kosong = dibuat server)")
    p_gj.add_argument("--clear", action="store_true", help="hapus join_code (grup terbuka)")

    p_tl = sub.add_parser("tokens", help="daftar token sekunder milik agent ini")
    p_tl.add_argument("--json", action="store_true", help="tampilkan JSON mentah")

    p_tc = sub.add_parser("token-create", help="buat token sekunder ber-scope")
    p_tc.add_argument("scopes", nargs="+", help="mis. messages:read files:read")
    p_tc.add_argument("--label", default=None)
    p_tc.add_argument("--days", type=int, default=None, help="masa berlaku (hari)")

    p_tr = sub.add_parser("token-revoke", help="cabut token sekunder")
    p_tr.add_argument("token_id")

    sub.add_parser("rotate-token", help="putar kunci utama agent (dengan grace)")

    p_ub = sub.add_parser("upload-big", help="unggah file besar (chunked + resumable)")
    p_ub.add_argument("target_type", choices=["direct", "group"])
    p_ub.add_argument("target")
    p_ub.add_argument("path")
    p_ub.add_argument("caption", nargs="*")
    p_ub.add_argument("--chunk-mb", type=int, default=5)

    p_h = sub.add_parser("history", help="tampilkan riwayat lokal")
    p_h.add_argument("n", type=int, nargs="?", default=20)

    args = parser.parse_args(argv)
    command = args.command or "poll"

    config = ClientConfig(args.config)
    ensure_registered(config)
    warn_insecure_transport(config)
    memory = open_memory(args)
    api = RelayAPI(config)
    file_handler = FileHandler(config.download_dir)

    if command == "poll":
        return cmd_poll(config, api, memory, file_handler, args)
    if command == "fetch-once":
        return cmd_fetch_once(config, api, memory, file_handler, args)
    if command == "send":
        return cmd_send(config, api, memory, args)
    if command == "send-group":
        return cmd_send_group(config, api, memory, args)
    if command == "send-task":
        return cmd_send_task(config, api, memory, args)
    if command == "receipts":
        return cmd_receipts(config, api, memory, args)
    if command == "send-file":
        return cmd_send_file(config, api, memory, file_handler, args)
    if command == "upload-big":
        return cmd_upload_big(config, api, memory, args)
    if command == "create-group":
        return cmd_create_group(config, api, memory, args)
    if command == "join-group":
        return cmd_join_group(config, api, memory, args)
    if command == "leave-group":
        return cmd_leave_group(config, api, memory, args)
    if command == "group-members":
        return cmd_group_members(config, api, memory, args)
    if command == "group-kick":
        return cmd_group_kick(config, api, memory, args)
    if command == "group-role":
        return cmd_group_role(config, api, memory, args)
    if command == "group-join-code":
        return cmd_group_join_code(config, api, memory, args)
    if command == "tokens":
        return cmd_tokens(config, api, memory, args)
    if command == "token-create":
        return cmd_token_create(config, api, memory, args)
    if command == "token-revoke":
        return cmd_token_revoke(config, api, memory, args)
    if command == "rotate-token":
        return cmd_rotate_token(config, api, memory, args)
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
