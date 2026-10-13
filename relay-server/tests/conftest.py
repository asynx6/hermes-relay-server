"""Fixture bersama: env diatur SEBELUM app diimpor.

Setiap test dapat DB bersih lewat fixture `client`. Test integrasi
memakai server uvicorn sungguhan via fixture `live_server`.
"""
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SERVER_ROOT = TESTS_DIR.parent
REPO_ROOT = SERVER_ROOT.parent

# Direktori kerja khusus test (DB + filemanager terpisah dari data dev).
TMP_ROOT = Path(tempfile.mkdtemp(prefix="relay-test-"))

os.environ["REGISTRATION_KEY"] = "test-registration-key"
os.environ["DB_PATH"] = str(TMP_ROOT / "relay.db")
os.environ["FILEMANAGER_DIR"] = str(TMP_ROOT / "filemanager")
os.environ["RATE_LIMIT_REGISTER"] = "5/minute"
os.environ["RATE_LIMIT_DEFAULT"] = "1000/minute"
os.environ.setdefault("RETENTION_DAYS", "30")

import sys
sys.path.insert(0, str(SERVER_ROOT))

from app.config import settings  # noqa: E402
from app.database import init_db  # noqa: E402
from app.main import create_app  # noqa: E402


def reset_db() -> None:
    """Hapus file DB dan buat ulang skema. Dipanggil per test."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
    init_db()
    if settings.filemanager_dir.exists():
        shutil.rmtree(settings.filemanager_dir)
    settings.filemanager_dir.mkdir(parents=True, exist_ok=True)
    # Sesi dashboard: password kembali ke default dan secret JWT dibuat ulang.
    for name in ("dashboard_auth.json", "dashboard_jwt_secret"):
        (settings.data_dir / name).unlink(missing_ok=True)


@pytest.fixture()
def client():
    reset_db()
    app = create_app()
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def cleanup_tmp():
    yield
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


def fresh_key() -> str:
    """Bucket rate limit unik per test (limiter in-memory dibagi antar test)."""
    return f"test-{uuid.uuid4()}"


def register_agent(client, name: str, description: str | None = None) -> dict:
    r = client.post(
        "/api/v1/register",
        headers={"X-Registration-Key": "test-registration-key", "X-Ratelimit-Key": fresh_key()},
        json={"name": name, "description": description},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}", "X-Ratelimit-Key": fresh_key()}


def create_group(client, secret: str, name: str, description: str | None = None) -> dict:
    """Buat grup, kembalikan data (termasuk group_id publik 'gr_...')."""
    r = client.post(
        "/api/v1/groups",
        headers=auth(secret),
        json={"name": name, "description": description},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def join_group(client, secret: str, name: str) -> dict:
    r = client.post(
        f"/api/v1/groups/{name}/join", headers=auth(secret)
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def send(
    client,
    secret: str,
    target_type: str,
    target_name: str,
    content: str,
    target_id: str | None = None,
) -> dict:
    """Kirim pesan. Untuk direct, target_id di-resolve otomatis dari nama."""
    if target_id is None:
        assert target_type == "direct", "target_id wajib untuk grup"
        r = client.get("/api/v1/agents", headers=auth(secret))
        assert r.status_code == 200, r.text
        agents = r.json()["data"]["agents"]
        match = next(
            (a for a in agents if a["name"].lower() == target_name.lower()), None
        )
        assert match is not None, f"agent {target_name} tidak terdaftar"
        target_id = match["agent_id"]

    r = client.post(
        "/api/v1/send-message",
        headers=auth(secret),
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": target_type,
            "target_id": target_id,
            "target_name": target_name,
            "content": content,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def fetch(client, secret: str, since_id: int = 0, limit: int = 50) -> dict:
    r = client.get(
        "/api/v1/fetch-messages",
        headers=auth(secret),
        params={"since_id": since_id, "limit": limit},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ---------------------------------------------------------------------------
# Server live untuk test integrasi client nyata
# ---------------------------------------------------------------------------

def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveServer:
    """Uvicorn berjalan di thread terpisah, persis seperti proses produksi.

    Bisa di-restart (stop lalu start) untuk menguji pemulihan agent.
    settings.db_path / filemanager_dir diarahkan ke direktori test selama
    server hidup, lalu dipulihkan saat stop.
    """

    def __init__(self):
        self.port = free_port()
        self.db_path = TMP_ROOT / f"live-{self.port}.db"
        self.filemanager = TMP_ROOT / f"live-fm-{self.port}"
        self._saved_db = settings.db_path
        self._saved_fm = settings.filemanager_dir
        self._reset_live_db()
        self.app = create_app()
        self.thread = None
        self.server = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def _reset_live_db(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        settings.db_path = self.db_path
        settings.filemanager_dir = self.filemanager
        init_db()
        settings.filemanager_dir.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        import uvicorn

        # Restart TIDAK menghapus DB: pesan dan registrasi harus utuh.
        settings.db_path = self.db_path
        settings.filemanager_dir = self.filemanager
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app, host="127.0.0.1", port=self.port, log_level="warning"
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        import requests as _requests

        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if _requests.get(f"{self.base_url}/health", timeout=1).status_code == 200:
                    return
            except _requests.RequestException:
                pass
            time.sleep(0.05)
        raise RuntimeError("Server live tidak bisa dijalankan")

    def stop(self) -> None:
        if self.server is not None and self.thread and self.thread.is_alive():
            self.server.should_exit = True
            self.thread.join(timeout=10)
        self.server = None
        self.thread = None
        settings.db_path = self._saved_db
        settings.filemanager_dir = self._saved_fm


@pytest.fixture()
def live_server():
    server = LiveServer()
    server.start()
    yield server
    server.stop()
