"""Sanitasi teks tidak tepercaya sebelum dicetak ke terminal.

Isi pesan datang dari agent lain (dan, lewat LLM, dari input publik). Tanpa
sanitasi, penyerang bisa menyisipkan escape sequence ANSI untuk memalsukan
baris log, menyembunyikan teks, atau mengacaukan tampilan terminal. Fungsi di
sini hanya untuk **tampilan** — data asli tetap utuh di database.
"""
import re

# CSI/OSC/escape ANSI + karakter kontrol lain (kecuali tab dan newline).
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MAX_DISPLAY = 500


def safe_text(value: str | None, max_len: int = MAX_DISPLAY) -> str:
    """Bersihkan teks untuk ditampilkan: buang ANSI + kontrol, batasi panjang."""
    if not value:
        return ""
    text = _ANSI.sub("", value)
    text = _CONTROL.sub("", text)
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text
