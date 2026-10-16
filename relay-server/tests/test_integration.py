"""Phase 4 integration tests: a real uvicorn server + a real Hermes client.

Each scenario uses a full client agent (registration, local SQLite
memory, poller, file handler) communicating over HTTP with the live server.
"""
import hashlib
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "hermes-agent"))

from hermes.api_client import AuthRejected, RelayAPI, RelayUnavailable
from hermes.config import ClientConfig
from hermes.file_handler import FileHandler
from hermes.memory import Memory
from hermes.poller import Poller

REGISTRATION_KEY = "test-registration-key"


class AgentHarness:
    """A real Hermes agent with single-shot polling (poll_once)."""

    def __init__(self, base_url: str, name: str, workdir: Path):
        self.dir = Path(workdir)
        (self.dir / "data").mkdir(parents=True, exist_ok=True)
        self.config = ClientConfig(self.dir / "config.json")
        self.config.server_url = base_url
        self.config.registration_key = REGISTRATION_KEY
        self.config.agent_name = name
        self.config.download_dir = str(self.dir / "downloads")
        self.config.save()

        api = RelayAPI(self.config, retry_max=2, retry_base=0.01)
        data = api.register()
        self.config.save_registration(data["secret_key"])
        self.agent_id = data["agent_id"]

        self.api = RelayAPI(self.config, retry_max=2, retry_base=0.01)
        self.memory = Memory(str(self.dir / "data" / "agent_memory.db"), agent_name=name)
        self.file_handler = FileHandler(self.config.download_dir)
        self.received: list[dict] = []
        self._id_cache: dict[tuple[str, str], str] = {}
        self.poller = Poller(
            self.api,
            self.memory,
            self.file_handler,
            poll_min=0.05,
            poll_max=0.2,
            on_message_fn=self.received.append,
        )

    def poll(self) -> tuple[bool, bool]:
        return self.poller.poll_once()

    def _resolve(self, target_type: str, name: str) -> str:
        """Name -> public ID (ag_/gr_). Addressing is always an ID+name pair.

        The result is cached: the directory does not change during a single test,
        and the load test sends >1000 messages to the same repeated targets.
        """
        key = (target_type, name.lower())
        if key in self._id_cache:
            return self._id_cache[key]
        if target_type == "group":
            match = next(
                (g for g in self.api.list_groups() if g["name"].lower() == name.lower()),
                None,
            )
            if match is None:
                raise RuntimeError(f"Grup '{name}' tidak ditemukan.")
            peer_id = match["group_id"]
        else:
            match = next(
                (a for a in self.api.list_agents() if a["name"].lower() == name.lower()),
                None,
            )
            if match is None:
                raise RuntimeError(f"Agent '{name}' tidak terdaftar.")
            peer_id = match["agent_id"]
        self._id_cache[key] = peer_id
        return peer_id

    def send(self, target_type: str, target: str, content: str) -> dict:
        client_msg_id = str(uuid.uuid4())
        peer_id = self._resolve(target_type, target)
        self.memory.save_outgoing(target_type, target, peer_id, content, client_msg_id)
        data = self.api.send_message(
            target_type, peer_id, target, content, client_msg_id
        )
        row = self.memory.conn.execute(
            "SELECT id FROM conversations WHERE client_msg_id = ?", (client_msg_id,)
        ).fetchone()
        self.memory.mark_sent(row["id"], data["message_id"])
        return data

    def upload(self, path: Path, target_type: str, target: str,
               caption: str = "") -> dict:
        peer_id = self._resolve(target_type, target)
        return self.api.upload_file(
            str(path), target_type, peer_id, target, str(uuid.uuid4()), caption
        )

    def poll_until(self, count: int, timeout: float = 15.0) -> bool:
        """Poll until `count` messages are received or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.received) < count:
            self.poll()
            time.sleep(0.05)
        return len(self.received) >= count


@pytest.fixture()
def agents_dir(tmp_path):
    return tmp_path / "agents"


@pytest.fixture(autouse=True)
def _relax_rate_limits(monkeypatch):
    """The test registers a dozen agents; the 5/minute limit is too strict."""
    monkeypatch.setattr("app.config.settings.rate_limit_register", "10000/minute")
    monkeypatch.setattr("app.config.settings.rate_limit_default", "100000/minute")


# ---------------------------------------------------------------------------
# Scenarios 1-4: messaging
# ---------------------------------------------------------------------------

def test_scenario_1_direct_message(live_server, agents_dir):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")
    c = AgentHarness(live_server.base_url, "hermes-c", agents_dir / "c")

    a.send("direct", "hermes-b", "Ringkas dokumen laporan Q3")

    assert b.poll_until(1)
    assert b.received[0]["content"] == "Ringkas dokumen laporan Q3"
    assert b.received[0]["from"] == "hermes-a"
    assert c.poll() == (True, False)
    assert c.received == []


def test_scenario_2_group_broadcast(live_server, agents_dir):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")
    c = AgentHarness(live_server.base_url, "hermes-c", agents_dir / "c")

    a.api.create_group("research-team", "Grup riset")
    b.api.join_group("research-team")
    c.api.join_group("research-team")
    a.send("group", "research-team", "Dataset sudah siap")

    assert b.poll_until(1) and c.poll_until(1)
    for agent in (b, c):
        assert agent.received[0]["scope"] == "group"
        assert agent.received[0]["group"] == "research-team"
    assert a.poll() == (True, False)
    assert a.received == []


def test_scenario_3_non_member_send_to_group(live_server, agents_dir):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    d = AgentHarness(live_server.base_url, "hermes-d", agents_dir / "d")
    a.api.create_group("research-team")
    with pytest.raises(RuntimeError) as exc:
        d.send("group", "research-team", "intrusi")
    assert "403" in str(exc.value) or "FORBIDDEN" in str(exc.value)


def test_scenario_4_new_member_history(live_server, agents_dir):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")
    c = AgentHarness(live_server.base_url, "hermes-c", agents_dir / "c")

    a.api.create_group("research-team")
    b.api.join_group("research-team")
    a.send("group", "research-team", "pesan-1")

    c.api.join_group("research-team")
    a.send("group", "research-team", "pesan-2")

    assert b.poll_until(2)
    assert [m["content"] for m in b.received] == ["pesan-1", "pesan-2"]
    assert c.poll_until(1)
    assert [m["content"] for m in c.received] == ["pesan-2"]


# ---------------------------------------------------------------------------
# Scenarios 5-8: files
# ---------------------------------------------------------------------------

def test_scenario_5_direct_file_5mb(live_server, agents_dir, tmp_path):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")

    payload = os.urandom(5 * 1024 * 1024)
    src = tmp_path / "dataset-5mb.bin"
    src.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    data = a.upload(src, "direct", "hermes-b", caption="Dataset besar")
    assert data["sha256"] == expected
    assert data["size_bytes"] == len(payload)

    assert b.poll_until(1)
    rows = b.memory.conn.execute(
        "SELECT * FROM files_history ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["sha256"] == expected
    saved = Path(rows[0]["local_path"])
    assert saved.is_file()
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == expected
    assert saved.parent == b.file_handler.download_dir


def test_scenario_6_group_file(live_server, agents_dir, tmp_path):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")
    c = AgentHarness(live_server.base_url, "hermes-c", agents_dir / "c")

    a.api.create_group("research-team")
    b.api.join_group("research-team")
    c.api.join_group("research-team")

    payload = b"id,nilai\n" + os.urandom(100_000)
    src = tmp_path / "dataset.csv"
    src.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    a.upload(src, "group", "research-team", caption="Hasil scraping")

    assert b.poll_until(1) and c.poll_until(1)
    for agent in (b, c):
        row = agent.memory.conn.execute(
            "SELECT * FROM files_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "done"
        assert row["sha256"] == expected
        assert hashlib.sha256(Path(row["local_path"]).read_bytes()).hexdigest() == expected


def test_scenario_7_upload_over_limit(live_server, agents_dir, tmp_path, monkeypatch):
    from conftest import settings

    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")

    # max_upload_bytes is a property; patch it on the class so the instance sees the new value.
    from app.config import Settings

    monkeypatch.setattr(Settings, "max_upload_bytes", 4096)

    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(16 * 1024))
    with pytest.raises(RuntimeError) as exc:
        a.upload(src, "direct", "hermes-b")
    assert "FILE_TOO_LARGE" in str(exc.value) or "413" in str(exc.value)

    # No leftover files in the server filemanager.
    leftovers = [
        p for p in settings.filemanager_dir.iterdir() if p.is_file() and p.name != ".gitkeep"
    ]
    assert leftovers == []
    # The recipient receives no message at all.
    assert b.poll() == (True, False)
    assert b.received == []


def test_scenario_8_dangerous_filename(live_server, agents_dir, tmp_path):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")

    payload = b"isi aman"
    src = tmp_path / "payload.bin"
    src.write_bytes(payload)

    data = a.upload(src, "direct", "hermes-b")
    # Safe name on the recipient side, content intact.
    assert b.poll_until(1)
    row = b.memory.conn.execute(
        "SELECT * FROM files_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    local = Path(row["local_path"])
    assert local.parent == b.file_handler.download_dir
    assert local.read_bytes() == payload
    assert "/" not in local.name and "\\" not in local.name


def test_scenario_8b_dangerous_upload_name(live_server, agents_dir, tmp_path):
    """A malicious file name is uploaded as-is: the server must store a random name."""
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")

    payload = b"tidak berbahaya"
    src = tmp_path / "payload.bin"
    src.write_bytes(payload)

    import requests as _rq

    with open(src, "rb") as f:
        resp = _rq.post(
            f"{live_server.base_url}/upload-file",
            headers={"Authorization": f"Bearer {a.config.secret_key}"},
            files={"file": ("../../etc/passwd", f, "application/octet-stream")},
            data={
                "target_type": "direct",
                "target_id": b._resolve("direct", "hermes-b"),
                "target_name": "hermes-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["original_name"] == "passwd"  # sanitized basename
    assert "/" not in data["original_name"]

    from conftest import settings

    stored = [
        p.name for p in settings.filemanager_dir.iterdir()
        if p.is_file() and p.name != ".gitkeep"
    ]
    assert stored and all(len(Path(n).stem) == 32 for n in stored)


# ---------------------------------------------------------------------------
# Scenarios 9-11: auth, recovery, idempotency
# ---------------------------------------------------------------------------

def test_scenario_9_auth_failures(live_server, agents_dir):
    import requests as _rq

    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")

    r = _rq.get(f"{live_server.base_url}/fetch-messages")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"

    r = _rq.get(
        f"{live_server.base_url}/fetch-messages",
        headers={"Authorization": "Bearer hsk_token_salah"},
    )
    assert r.status_code == 401

    # The real token is rejected by the server: the client stops polling, not crashing.
    agent = AgentHarness(live_server.base_url, "hermes-bad", agents_dir / "bad")
    agent.config.secret_key = "hsk_palsu"
    agent.api.session.headers["Authorization"] = "Bearer hsk_palsu"
    assert agent.poll() == (False, False)
    assert agent.poller.auth_rejected is True


def test_scenario_10_server_outage_recovery(live_server, agents_dir):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")
    # ID resolution is done while the server is still alive.
    peer_id = b._resolve("direct", "hermes-a")

    # The server is shut down while the agent is polling.
    live_server.stop()
    reachable, _ = b.poll()
    assert reachable is False

    # The outgoing message fails to send: it stays in the outbox, not lost.
    client_msg_id = str(uuid.uuid4())
    b.memory.save_outgoing("direct", "hermes-a", peer_id, "pesan-saat-mati", client_msg_id)
    with pytest.raises(RelayUnavailable):
        b.api.send_message(
            "direct", peer_id, "hermes-a", "pesan-saat-mati", client_msg_id
        )
    pending = b.memory.pending_outbox()
    assert len(pending) == 1
    assert pending[0]["content"] == "pesan-saat-mati"

    # The server is started again.
    live_server.start()

    # The outbox is delivered after recovery.
    b.poller.flush_outbox()
    assert b.memory.pending_outbox() == []

    # A receives the message that was sent during the outage.
    assert a.poll_until(1)
    assert a.received[0]["content"] == "pesan-saat-mati"

    # Messages arriving while the server is down are not lost: A sends again and B fetches.
    a.send("direct", "hermes-b", "setelah-pulih")
    assert b.poll_until(1)
    assert b.received[0]["content"] == "setelah-pulih"


def test_scenario_11_idempotent_double_send(live_server, agents_dir):
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")

    client_msg_id = str(uuid.uuid4())
    peer_id = a._resolve("direct", "hermes-b")
    first = a.api.send_message("direct", peer_id, "hermes-b", "pesan sama", client_msg_id)
    assert first["duplicate"] is False
    second = a.api.send_message("direct", peer_id, "hermes-b", "pesan sama", client_msg_id)
    assert second["duplicate"] is True
    assert second["message_id"] == first["message_id"]

    assert b.poll_until(1)
    assert len(b.received) == 1

    # One row in local memory and one message on the server.
    rows = b.memory.conn.execute("SELECT * FROM conversations").fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Scenario 12: load
# ---------------------------------------------------------------------------

def test_scenario_12_load_10_agents(live_server, agents_dir):
    """10 agents polling every 3s; measure send latency and ensure all are delivered."""
    n_agents = 10
    msgs_per_agent = 15

    agents = [
        AgentHarness(live_server.base_url, f"hermes-load{i}", agents_dir / f"load{i}")
        for i in range(n_agents)
    ]

    latencies = []
    for sender in agents:
        for j in range(msgs_per_agent):
            for target in agents:
                if target is sender:
                    continue
                t0 = time.perf_counter()
                sender.send("direct", target.config.agent_name, f"m{j}")
                latencies.append(time.perf_counter() - t0)

    # All agents poll until all messages arrive.
    expected = msgs_per_agent * (n_agents - 1)
    deadline = time.time() + 60
    while time.time() < deadline:
        if all(len(a.received) >= expected for a in agents):
            break
        for a in agents:
            a.poll()
        time.sleep(0.05)

    for agent in agents:
        assert len(agent.received) == expected, (
            f"{agent.config.agent_name} hanya menerima {len(agent.received)}/{expected}"
        )

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    print(
        f"\n[load] {len(latencies)} pesan, {n_agents} agent: "
        f"p50={p50*1000:.1f}ms p95={p95*1000:.1f}ms max={latencies[-1]*1000:.1f}ms"
    )
    # Loose bound: the real failure is a lost message, not performance.
    assert p95 < 1.0


def test_scenario_13_file_manager_by_id(live_server, agents_dir, tmp_path):
    """Agent-side file manager: list files (name, ID, size) then download by ID."""
    a = AgentHarness(live_server.base_url, "hermes-a", agents_dir / "a")
    b = AgentHarness(live_server.base_url, "hermes-b", agents_dir / "b")
    c = AgentHarness(live_server.base_url, "hermes-c", agents_dir / "c")

    payload = os.urandom(2 * 1024 * 1024)
    src = tmp_path / "dataset-final.csv"
    src.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    data = a.upload(src, "direct", "hermes-b", caption="Dataset final")
    file_id = data["file_id"]

    # The recipient sees the file with all three attributes: name, ID, size.
    listing = b.api.list_files()
    assert listing["total"] == 1
    entry = listing["files"][0]
    assert entry["file_id"] == file_id
    assert entry["original_name"] == "dataset-final.csv"
    assert entry["size_bytes"] == len(payload)

    # Download directly with the file_id (not via a message).
    dest = b.file_handler.download_by_id(b.api, entry, b.memory)
    assert dest.is_file()
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == expected

    # The local history records the download by ID.
    row = b.memory.conn.execute(
        "SELECT * FROM files_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "done"
    assert row["file_id"] == file_id
    assert row["sha256"] == expected

    # Other agents do not see A->B direct files.
    assert c.api.list_files()["total"] == 0

    # Re-download with a nonexistent ID -> a clear error.
    with pytest.raises(RuntimeError):
        b.api.download_file("fl_tidakada000")
