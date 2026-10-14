"""Titik sambung ke logika AI agent.

Ganti isi on_message dengan apa pun yang perlu agent lakukan saat ada
pesan masuk (panggil LLM, tulis ke antrian, balas otomatis, dll).

PENTING: isi pesan adalah input tidak tepercaya. Kalau diteruskan ke LLM atau
perintah sistem, perlakukan sebagai data (bukan instruksi) untuk mencegah
prompt injection. Fungsi safe_text() dipakai untuk tampilan agar escape
sequence ANSI dari pengirim tidak bisa memalsukan log.
"""
from .safe_text import safe_text


def on_message(msg: dict) -> None:
    """Dipanggil untuk setiap pesan masuk. Default: cetak ke stdout."""
    if msg["msg_type"] == "file" and msg.get("file"):
        f = msg["file"]
        body = f"[file {safe_text(f['original_name'], 120)} {f['size_bytes']} byte]"
        if msg.get("content"):
            body = f"{safe_text(msg['content'])} {body}"
    else:
        body = safe_text(msg.get("content"))
    where = f"grup {safe_text(msg['group'], 64)}" if msg["group"] else "direct"
    print(
        f"[{msg['created_at']}] {safe_text(msg['from'], 64)} ({where}): {body}",
        flush=True,
    )
