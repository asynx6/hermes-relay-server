"""Test dashboard admin: login JWT, agent, chat, dan file manager."""
import hashlib
import os
import time
import uuid
from pathlib import Path

import jwt
import pytest

from conftest import register_agent, send

API = "/api/v1/dashboard"


@pytest.fixture(autouse=True)
def _relax_login_limit(monkeypatch):
    """Login limit produksi 10/menit terlalu ketat untuk rangkaian test."""
    monkeypatch.setattr("app.config.settings.rate_limit_login", "1000/minute")


def login(client, password: str = "123456"):
    return client.post(f"{API}/login", json={"password": password})


def test_login_default_password(client):
    r = login(client)
    assert r.status_code == 200
    assert r.json()["data"]["session_seconds"] > 0
    # Cookie HttpOnly sesi terpasang.
    assert "hermes_dashboard" in r.cookies

    r = client.get(f"{API}/me")
    assert r.status_code == 200
    assert r.json()["data"]["session_seconds"] > 0


def test_login_wrong_password(client):
    r = login(client, "salah")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"
    assert client.get(f"{API}/me").status_code == 401


def test_protected_endpoints_require_session(client):
    for path in ("/agents", "/conversations", "/groups", "/files"):
        r = client.get(f"{API}{path}")
        assert r.status_code == 401, path
        assert r.json()["code"] == "UNAUTHORIZED"


def test_logout_clears_cookie(client):
    login(client)
    r = client.post(f"{API}/logout")
    assert r.status_code == 200
    assert client.get(f"{API}/me").status_code == 401


def test_change_password_flow(client):
    login(client)
    r = client.post(
        f"{API}/change-password",
        json={"old_password": "123456", "new_password": "rahasia-baru-1"},
    )
    assert r.status_code == 200

    client.post(f"{API}/logout")
    assert login(client, "123456").status_code == 401
    assert login(client, "rahasia-baru-1").status_code == 200


def test_change_password_wrong_old(client):
    login(client)
    r = client.post(
        f"{API}/change-password",
        json={"old_password": "salah", "new_password": "apapun-aja"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "BAD_REQUEST"


def test_change_password_too_short(client):
    login(client)
    r = client.post(
        f"{API}/change-password",
        json={"old_password": "123456", "new_password": "123"},
    )
    assert r.status_code == 422  # validasi Pydantic (min 6 karakter)


def test_expired_token_rejected(client):
    from app.dashboard import auth

    past = int(time.time()) - 10
    token = jwt.encode(
        {"sub": "dashboard", "iat": past, "exp": past},
        auth._jwt_secret(),
        algorithm="HS256",
    )
    client.cookies.set("hermes_dashboard", token)
    assert client.get(f"{API}/me").status_code == 401
    assert client.get(f"{API}/agents").status_code == 401


def test_login_rate_limited(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.rate_limit_login", "2/minute")
    codes = [login(client).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = client.post(f"{API}/login", json={"password": "123456"})
    assert r.json()["code"] == "RATE_LIMITED"


def test_static_dashboard_served(client):
    r = client.get("/dashboard/")
    assert r.status_code == 200
    assert "Hermes Relay" in r.text


def test_agents_list(client):
    register_agent(client, "hermes-alpha")
    register_agent(client, "hermes-beta")
    login(client)
    data = client.get(f"{API}/agents").json()["data"]
    names = [a["name"] for a in data["agents"]]
    assert names == ["hermes-alpha", "hermes-beta"]
    for a in data["agents"]:
        assert a["status"] == "active"
        assert a["agent_id"].startswith("ag_")


def test_direct_conversation_view(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")
    send(client, a["secret_key"], "direct", "hermes-b", "pesan 1")
    send(client, b["secret_key"], "direct", "hermes-a", "balasan 2")
    # Pesan dari C ke pihak lain tidak boleh muncul di thread A-B.
    send(client, c["secret_key"], "direct", "hermes-b", "pesan C")

    login(client)
    data = client.get(f"{API}/conversations").json()["data"]
    pairs = [tuple(x["agents"]) for x in data["conversations"]]
    assert ("hermes-a", "hermes-b") in pairs

    thread = client.get(f"{API}/conversations/hermes-a/hermes-b").json()["data"]
    assert [m["content"] for m in thread["messages"]] == ["pesan 1", "balasan 2"]
    assert thread["messages"][0]["from"] == "hermes-a"
    assert thread["messages"][0]["to"] == "hermes-b"


def test_conversation_unknown_agent(client):
    login(client)
    r = client.get(f"{API}/conversations/hermes-a/hermes-b")
    assert r.status_code == 404


def test_groups_and_group_messages_view(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    client.post(
        "/api/v1/groups",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={"name": "research-team"},
    )
    client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
    )
    send(client, a["secret_key"], "group", "research-team", "hai grup")

    login(client)
    groups = client.get(f"{API}/groups").json()["data"]["groups"]
    assert len(groups) == 1
    assert sorted(groups[0]["members"]) == ["hermes-a", "hermes-b"]

    msgs = client.get(f"{API}/groups/research-team/messages").json()["data"]
    assert msgs["group"] == "research-team"
    assert [m["content"] for m in msgs["messages"]] == ["hai grup"]
    assert msgs["messages"][0]["from"] == "hermes-a"


def test_group_messages_unknown(client):
    login(client)
    r = client.get(f"{API}/groups/nonexistent/messages")
    assert r.status_code == 404


def test_files_list_download_delete(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")

    payload = os.urandom(2048)
    src = tmp_path / "laporan.pdf"
    src.write_bytes(payload)
    with open(src, "rb") as f:
        r = client.post(
            "/api/v1/upload-file",
            headers={"Authorization": f"Bearer {a['secret_key']}"},
            files={"file": ("laporan.pdf", f, "application/pdf")},
            data={
                "target_type": "direct",
                "target": "hermes-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    assert r.status_code == 201
    stored_name = None

    login(client)
    files = client.get(f"{API}/files").json()["data"]["files"]
    assert len(files) == 1
    f0 = files[0]
    stored_name = f0["stored_name"]
    assert f0["original_name"] == "laporan.pdf"
    assert f0["uploader"] == "hermes-a"
    assert f0["target"] == "hermes-b"
    assert f0["size_bytes"] == 2048
    assert f0["exists_on_disk"] is True
    assert f0["deleted_at"] is None

    r = client.get(f"{API}/files/{stored_name}/download")
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["x-content-type-options"] == "nosniff"

    # Download count naik lewat dashboard.
    files = client.get(f"{API}/files").json()["data"]["files"]
    assert files[0]["download_count"] == 1

    # Hapus: file hilang dari disk, status jadi deleted_at.
    r = client.delete(f"{API}/files/{stored_name}")
    assert r.status_code == 200
    from app.config import settings

    assert not (settings.filemanager_dir / stored_name).is_file()

    files = client.get(f"{API}/files").json()["data"]["files"]
    assert files[0]["deleted_at"] is not None
    assert files[0]["exists_on_disk"] is False

    # Download setelah hapus -> 410 (konsisten dengan API agent).
    assert client.get(f"{API}/files/{stored_name}/download").status_code == 410
    # Hapus dua kali -> 409.
    assert client.delete(f"{API}/files/{stored_name}").status_code == 409


def test_file_download_unknown(client):
    login(client)
    r = client.get(f"{API}/files/fl_tidakada.bin/download")
    assert r.status_code == 404


def test_file_view_requires_session(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = Path(__file__)  # file apa pun
    with open(src, "rb") as f:
        client.post(
            "/api/v1/upload-file",
            headers={"Authorization": f"Bearer {a['secret_key']}"},
            files={"file": ("x.bin", f, "application/octet-stream")},
            data={
                "target_type": "direct",
                "target": "hermes-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    r = client.get(f"{API}/files")
    assert r.status_code == 401


def test_file_message_metadata_in_chat_views(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    client.post(
        "/api/v1/groups",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={"name": "research-team"},
    )
    client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
    )
    payload = b"id,nilai\n1,10\n"
    src = tmp_path / "dataset.csv"
    src.write_bytes(payload)
    with open(src, "rb") as f:
        client.post(
            "/api/v1/upload-file",
            headers={"Authorization": f"Bearer {a['secret_key']}"},
            files={"file": ("dataset.csv", f, "text/csv")},
            data={
                "target_type": "group",
                "target": "research-team",
                "client_msg_id": str(uuid.uuid4()),
                "caption": "Dataset scraping",
            },
        )

    login(client)
    msgs = client.get(f"{API}/groups/research-team/messages").json()["data"]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["msg_type"] == "file"
    assert msgs[0]["content"] == "Dataset scraping"
    assert msgs[0]["file"]["original_name"] == "dataset.csv"
    assert msgs[0]["file"]["size_bytes"] == len(payload)
