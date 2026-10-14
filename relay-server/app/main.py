"""Aplikasi FastAPI: Hermes Relay Server."""
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from .config import settings
from .dashboard.routes import router as dashboard_router
from .database import init_db
from .ratelimit import limiter
from .routers import files, groups, messages, register

API_PREFIX = "/api/v1"
STATIC_DIR = Path(__file__).resolve().parent / "dashboard" / "static"

logger = logging.getLogger("hermes.relay")

WEAK_REGISTRATION_KEYS = {"", "change-me-in-env", "change-me", "ganti-dengan-string-acak-panjang"}


def check_security_defaults() -> None:
    """Tolak start kalau kredensial masih default pendekatan 'ganti saya'."""
    if settings.registration_key.strip().lower() in WEAK_REGISTRATION_KEYS:
        raise SystemExit(
            "REFUSING TO START: REGISTRATION_KEY masih nilai default.\n"
            "Set REGISTRATION_KEY di .env dengan string acak panjang, contoh:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_security_defaults()
    init_db()
    settings.filemanager_dir.mkdir(parents=True, exist_ok=True)
    (settings.filemanager_dir / ".staging").mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Hermes Relay Server", version="1.0.0", lifespan=lifespan)
    app.state.limiter = limiter

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if not isinstance(detail, dict):
            detail = {
                "status": "error",
                "code": "ERROR",
                "message": str(detail),
            }
        return JSONResponse(status_code=exc.status_code, content=detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0]
        message = f"{first.get('loc', ['?'])[-1]}: {first.get('msg', 'invalid')}"
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "code": "VALIDATION_ERROR",
                "message": f"Validasi gagal - {message}",
            },
        )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={
                "status": "error",
                "code": "RATE_LIMITED",
                "message": "Terlalu banyak request. Coba lagi nanti.",
            },
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_handler(request: Request, exc: sqlite3.IntegrityError):
        # Jaring pengaman: endpoint yang mengecek-lalu-INSERT tanpa transaksi
        # bisa kena UNIQUE saat dua request bersamaan. Endpoint endpoint itu
        # menangani IntegrityError sendiri; ini fallback agar tidak jadi 500.
        logger.warning("IntegrityError di %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=409,
            content={
                "status": "error",
                "code": "CONFLICT",
                "message": "Data sudah ada (kemungkinan request bersamaan).",
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # Semua error tak terduga tetap memakai envelope error, bukan HTML
        # 500 mentah yang membuat client gagal parse JSON.
        logger.exception("Error tak tertangani di %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "code": "INTERNAL_ERROR",
                "message": "Kesalahan server tak terduga.",
            },
        )

    app.include_router(register.router, prefix=API_PREFIX)
    app.include_router(messages.router, prefix=API_PREFIX)
    app.include_router(files.router, prefix=API_PREFIX)
    app.include_router(groups.router, prefix=API_PREFIX)
    app.include_router(dashboard_router, prefix=f"{API_PREFIX}/dashboard")

    # Antarmuka web dashboard (statis; login dijaga oleh API di atas).
    app.mount("/dashboard", StaticFiles(directory=str(STATIC_DIR), html=True), name="dashboard")
    return app


app = create_app()
