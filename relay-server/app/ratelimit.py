"""Rate limiter tunggal untuk aplikasi.

Kunci bucket:
- Endpoint ber-token (Authorization) -> hash token, BUKAN IP. Di belakang
  reverse proxy semua agent tampak sebagai satu IP proxy, jadi bucket per-IP
  akan dibagi bersama dan mudah habis; per-token juga menahan satu agent
  yang bermasalah tanpa menghukum agent lain.
- Endpoint publik (/register, /login dashboard) -> IP, karena tidak ada
  token. Karena di belakang proxy IP yang terlihat adalah IP proxy, batas
  untuk endpoint publik harus longgar dan password wajib kuat.

Bucket disimpan di memori proses. Untuk beberapa worker uvicorn, pindah ke
storage Redis (lihat catatan di README).
"""
import hashlib

from slowapi import Limiter
from slowapi.util import get_remote_address

PUBLIC_SUFFIXES = ("/register", "/login")


def key_func(request) -> str:
    path = request.url.path.rstrip("/")
    auth = request.headers.get("authorization")
    if auth and not path.endswith(PUBLIC_SUFFIXES):
        return "tok:" + hashlib.sha256(auth.encode("utf-8")).hexdigest()[:16]
    return get_remote_address(request)


limiter = Limiter(key_func=key_func)
