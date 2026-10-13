"""Rate limiter tunggal untuk aplikasi.

Limit dibaca dari settings lewat lambda saat request datang, sehingga
nilai limit bisa diubah tanpa membuat ulang aplikasi. Header
X-Ratelimit-Key memisahkan bucket per klien (dipakai test agar tidak
berbagi kuota).
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from .config import settings


def key_func(request) -> str:
    override = request.headers.get("x-ratelimit-key")
    if override:
        return override
    return get_remote_address(request)


limiter = Limiter(key_func=key_func)
