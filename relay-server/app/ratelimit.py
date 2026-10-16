"""Single rate limiter for the application.

Bucket keys:
- Token-bearing endpoints (Authorization) -> hash of the token, NOT the IP.
  Behind a reverse proxy all agents appear as one proxy IP, so a per-IP
  bucket would be shared and easily exhausted; per-token also throttles a
  single misbehaving agent without punishing the others.
- Public endpoints (/register, dashboard /login) -> IP, because there is no
  token. Since behind a proxy the visible IP is the proxy IP, the limits
  for public endpoints must be loose and passwords must be strong.

Buckets are kept in process memory. For multiple uvicorn workers, switch to
Redis storage (see the note in the README).
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
