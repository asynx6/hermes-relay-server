"""Titik sambung ke logika AI agent.

Ganti isi on_message dengan apa pun yang perlu agent lakukan saat ada
pesan masuk (panggil LLM, tulis ke antrian, balas otomatis, dll).
"""


def on_message(msg: dict) -> None:
    """Dipanggil untuk setiap pesan masuk. Default: cetak ke stdout."""
    if msg["msg_type"] == "file" and msg.get("file"):
        f = msg["file"]
        body = f"[file {f['original_name']} {f['size_bytes']} byte]"
        if msg.get("content"):
            body = f"{msg['content']} {body}"
    else:
        body = msg.get("content") or ""
    where = f"grup {msg['group']}" if msg["group"] else "direct"
    print(f"[{msg['created_at']}] {msg['from']} ({where}): {body}", flush=True)
