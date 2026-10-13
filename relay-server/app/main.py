"""Aplikasi FastAPI: Hermes Relay Server."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.filemanager_dir.mkdir(parents=True, exist_ok=True)
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

    app.include_router(register.router, prefix=API_PREFIX)
    app.include_router(messages.router, prefix=API_PREFIX)
    app.include_router(files.router, prefix=API_PREFIX)
    app.include_router(groups.router, prefix=API_PREFIX)
    app.include_router(dashboard_router, prefix=f"{API_PREFIX}/dashboard")

    # Antarmuka web dashboard (statis; login dijaga oleh API di atas).
    app.mount("/dashboard", StaticFiles(directory=str(STATIC_DIR), html=True), name="dashboard")
    return app


app = create_app()
