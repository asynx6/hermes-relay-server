"""Test autentikasi dan registrasi (Fase 1)."""
import sqlite3
import uuid

from conftest import fetch, register_agent


def test_health_no_auth(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_register_without_key(client):
    r = client.post("/api/v1/register", json={"name": "hermes-nokey"})
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_register_wrong_key(client):
    r = client.post(
        "/api/v1/register",
        headers={"X-Registration-Key": "salah", "X-Ratelimit-Key": f"k-{uuid.uuid4()}"},
        json={"name": "hermes-wrong"},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_register_success_stores_only_hash(client):
    data = register_agent(client, "hermes-alpha")
    assert data["agent_id"].startswith("ag_")
    assert data["name"] == "hermes-alpha"
    assert data["secret_key"].startswith("hsk_")

    from conftest import settings

    conn = sqlite3.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT secret_hash, status FROM agents WHERE name = 'hermes-alpha'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    secret_hash, status = row
    assert status == "active"
    assert secret_hash != data["secret_key"]
    assert len(secret_hash) == 64  # SHA-256 hex


def test_register_duplicate_name(client):
    register_agent(client, "hermes-alpha")
    r = client.post(
        "/api/v1/register",
        headers={"X-Registration-Key": "test-registration-key", "X-Ratelimit-Key": f"k-{uuid.uuid4()}"},
        json={"name": "hermes-alpha"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"


def test_register_uppercase_name_rejected(client):
    # Regex nama hanya menerima huruf kecil; huruf besar ditolak validasi.
    r = client.post(
        "/api/v1/register",
        headers={"X-Registration-Key": "test-registration-key", "X-Ratelimit-Key": f"k-{uuid.uuid4()}"},
        json={"name": "Hermes-Alpha"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_register_invalid_name(client):
    for bad in ("ab", "UPPER", "-dash", "a" * 33, "sp ace"):
        r = client.post(
            "/api/v1/register",
            headers={"X-Registration-Key": "test-registration-key", "X-Ratelimit-Key": f"k-{bad}"},
            json={"name": bad},
        )
        assert r.status_code == 422, bad
        assert r.json()["code"] == "VALIDATION_ERROR"


def test_fetch_without_token(client):
    r = client.get("/api/v1/fetch-messages")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_fetch_with_garbage_token(client):
    r = client.get(
        "/api/v1/fetch-messages", headers={"Authorization": "Bearer hsk_nonsense"}
    )
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_fetch_with_valid_token(client):
    agent = register_agent(client, "hermes-alpha")
    r = client.get("/api/v1/fetch-messages", headers={"Authorization": f"Bearer {agent['secret_key']}"})
    assert r.status_code == 200
    assert r.json()["data"]["messages"] == []


def test_revoked_agent_blocked(client):
    from app.database import connect

    agent = register_agent(client, "hermes-revoked")
    conn = connect()
    conn.execute("UPDATE agents SET status = 'revoked' WHERE name = 'hermes-revoked'")
    conn.commit()
    conn.close()
    r = client.get(
        "/api/v1/fetch-messages",
        headers={"Authorization": f"Bearer {agent['secret_key']}"},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_fetch_updates_last_seen(client):
    from app.database import connect

    agent = register_agent(client, "hermes-alpha")
    fetch(client, agent["secret_key"])
    conn = connect()
    try:
        row = conn.execute(
            "SELECT last_seen_at FROM agents WHERE name = 'hermes-alpha'"
        ).fetchone()
    finally:
        conn.close()
    assert row["last_seen_at"] is not None


def test_register_rate_limited(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.rate_limit_register", "2/minute")
    key = f"rl-{uuid.uuid4()}"
    statuses = []
    for i in range(3):
        r = client.post(
            "/api/v1/register",
            headers={"X-Registration-Key": "test-registration-key", "X-Ratelimit-Key": key},
            json={"name": f"hermes-rl{i}"},
        )
        statuses.append(r.status_code)
    assert statuses[:2] == [201, 201]
    assert statuses[2] == 429
    assert client.get(
        "/api/v1/fetch-messages",
        headers={"X-Ratelimit-Key": key},
    ).json()["code"]  # tetap dibatasi, tapi endpoint butuh auth dulu
    r = client.get("/api/v1/fetch-messages", headers={"X-Ratelimit-Key": key})
    # 401 auth diperiksa sebelum rate limit; yang penting 429 muncul di register
    assert r.status_code in (401, 429)
