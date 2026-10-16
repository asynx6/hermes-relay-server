"""Shared fixtures: env is set BEFORE the app is imported.

Each test gets a clean DB through the `client` fixture. Integration tests
use a real uvicorn server via the `live_server` fixture.
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

# Test-specific working directory (DB + filemanager separate from dev data).
TMP_ROOT = Path(tempfile.mkdtemp(prefix="relay-test-"))

os.environ["REGISTRATION_KEY"] = "test-registration-key"
os.environ["DB_PATH"] = str(TMP_ROOT / "relay.db")
os.environ["FILEMANAGER_DIR"] = str(TMP_ROOT / "filemanager")
# Limits are relaxed for the test suite; the dedicated rate limit tests
# lower them themselves with a unique bucket key.
os.environ["RATE_LIMIT_REGISTER"] = "10000/minute"
os.environ["RATE_LIMIT_DEFAULT"] = "100000/minute"
os.environ["RATE_LIMIT_LOGIN"] = "10000/minute"
# Explicit dashboard password for tests (without this env, auth.py generates
# a random password and prints it once to stdout).
os.environ["DASHBOARD_PASSWORD"] = "123456"
os.environ.setdefault("RETENTION_DAYS", "30")

import sys
sys.path.insert(0, str(SERVER_ROOT))

from app.config import settings  # noqa: E402
from app.database import init_db  # noqa: E402
from app.main import create_app  # noqa: E402


def reset_db() -> None:
    """Delete the DB file and recreate the schema. Called per test."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
    init_db()
    if settings.filemanager_dir.exists():
        shutil.rmtree(settings.filemanager_dir)
    settings.filemanager_dir.mkdir(parents=True, exist_ok=True)
    # Dashboard session: password resets to default and the JWT secret is regenerated.
    for name in (
        "dashboard_auth.json",
        "dashboard_jwt_secret",
        "dashboard_jwt_epoch",
        "server_epoch",
    ):
        (settings.data_dir / name).unlink(missing_ok=True)


@pytest.fixture()
def agents_dir(tmp_path):
    return tmp_path / "agents"


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


def register_agent(client, name: str, description: str | None = None,
                    registration_nonce: str | None = None) -> dict:
    payload = {"name": name, "description": description}
    if registration_nonce:
        payload["registration_nonce"] = registration_nonce
    r = client.post(
        "/api/v1/register",
        headers={"X-Registration-Key": "test-registration-key"},
        json=payload,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def auth(secret: str) -> dict:
    # The rate limit bucket key is the token hash, not a test-specific header,
    # so each agent naturally gets its own bucket.
    return {"Authorization": f"Bearer {secret}"}


def create_group(client, secret: str, name: str, description: str | None = None) -> dict:
    """Create a group, return the data (including the public group_id 'gr_...')."""
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
    """Send a message. For direct, target_id is resolved automatically from the name."""
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
# Live server for real-client integration tests
# ---------------------------------------------------------------------------

def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveServer:
    """Uvicorn running in a separate thread, exactly like the production process.

    Can be restarted (stop then start) to test agent recovery.
    settings.db_path / filemanager_dir are pointed at the test directory while
    the server is alive, then restored on stop.
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

        # Restart does NOT delete the DB: messages and registrations must stay intact.
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
