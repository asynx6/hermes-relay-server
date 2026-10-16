"""Tests from the security & resilience review (register idempotency, ack/task,
executable sniff, rate-limit keying, JWT epoch, register/group races,
visibility of old files for new members, FK-safe cleanup).

Uses TestClient for endpoint flows and a real uvicorn server
for race tests (threads).
"""
import hashlib
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jwt
import pytest
import requests

from conftest import create_group, fetch, register_agent, send

API = "/api/v1"


# ---------------------------------------------------------------------------
# Idempotent registration with a nonce
# ---------------------------------------------------------------------------

def test_register_nonce_replay_same_secret(client):
    nonce = str(uuid.uuid4())
    first = client.post(
        f"{API}/register",
        headers={"X-Registration-Key": "test-registration-key"},
        json={"name": "hermes-nonce", "registration_nonce": nonce},
    )
    assert first.status_code == 201
    secret1 = first.json()["data"]["secret_key"]

    # Simulation: the 201 response is lost, the agent has not stored the secret,
    # then retries with the same nonce. It must get the same secret, not a 409.
    again = client.post(
        f"{API}/register",
        headers={"X-Registration-Key": "test-registration-key"},
        json={"name": "hermes-nonce", "registration_nonce": nonce},
    )
    assert again.status_code == 201
    body = again.json()["data"]
    assert body["secret_key"] == secret1
    assert body["replayed"] is True


def test_register_same_name_different_nonce_rejected(client):
    client.post(
        f"{API}/register",
        headers={"X-Registration-Key": "test-registration-key"},
        json={"name": "hermes-dup", "registration_nonce": str(uuid.uuid4())},
    )
    r = client.post(
        f"{API}/register",
        headers={"X-Registration-Key": "test-registration-key"},
        json={"name": "hermes-dup", "registration_nonce": str(uuid.uuid4())},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"


def test_register_duplicate_name_still_rejected(client):
    register_agent(client, "hermes-x1")
    r = client.post(
        f"{API}/register",
        headers={"X-Registration-Key": "test-registration-key"},
        json={"name": "hermes-x1"},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# server_epoch + ack + task/RPC
# ---------------------------------------------------------------------------

def test_fetch_includes_server_epoch(client):
    a = register_agent(client, "hermes-a")
    r = client.get(
        "/api/v1/fetch-messages", headers={"Authorization": f"Bearer {a['secret_key']}"}
    )
    assert r.status_code == 200
    assert r.json()["data"]["server_epoch"]


def test_ack_and_receipts_flow(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")

    data = send(client, a["secret_key"], "direct", "hermes-b", "pesan untuk di-ack")
    message_id = data["message_id"]

    # Before ack: no receipt.
    r = client.get(
        f"{API}/receipts?message_ids={message_id}",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    )
    assert r.json()["data"]["receipts"] == []

    # B acks.
    r = client.post(
        f"{API}/ack",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
        json={"message_ids": [message_id], "status": "delivered"},
    )
    assert r.status_code == 200

    # A sees the receipt from B.
    r = client.get(
        f"{API}/receipts?message_ids={message_id}",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    )
    receipts = r.json()["data"]["receipts"]
    assert len(receipts) == 1
    assert receipts[0]["seen_by"] == [
        {"agent": "hermes-b", "status": "delivered", "at": receipts[0]["seen_by"][0]["at"]}
    ]


def test_ack_only_for_visible_messages(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")
    data = send(client, a["secret_key"], "direct", "hermes-b", "rahasia")
    r = client.post(
        f"{API}/ack",
        headers={"Authorization": f"Bearer {c['secret_key']}"},
        json={"message_ids": [data["message_id"]]},
    )
    assert r.status_code == 200  # ignored, not an error
    receipts = client.get(
        f"{API}/receipts?message_ids={data['message_id']}",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    ).json()["data"]["receipts"]
    assert receipts == []  # C is not registered as a recipient


def test_task_result_rpc_flow(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    task_id = f"task_{uuid.uuid4().hex[:16]}"

    # A sends a task.
    r = client.post(
        f"{API}/send-message",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-b",
            "content": '{"op": "ringkas", "doc": "laporan-q3"}',
            "msg_type": "task",
            "task_id": task_id,
            "task_status": "pending",
        },
    )
    assert r.status_code == 201

    # B replies with the result.
    r = client.post(
        f"{API}/send-message",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": a["agent_id"],
            "target_name": "hermes-a",
            "content": "Ringkasan: 3 halaman, 12 temuan.",
            "msg_type": "result",
            "task_id": task_id,
            "task_status": "done",
        },
    )
    assert r.status_code == 201

    # Both sides can see the task history.
    for agent in (a, b):
        task = client.get(
            f"{API}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {agent['secret_key']}"},
        ).json()["data"]
        assert [m["msg_type"] for m in task["messages"]] == ["task", "result"]
        assert task["messages"][1]["task_status"] == "done"


def test_task_id_required_for_task_type(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    r = client.post(
        f"{API}/send-message",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-b",
            "content": "tanpa task_id",
            "msg_type": "task",
        },
    )
    assert r.status_code == 422


def test_task_thread_invisible_to_others(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    client.post(
        f"{API}/send-message",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-b",
            "content": "tugas rahasia",
            "msg_type": "task",
            "task_id": task_id,
        },
    )
    r = client.get(
        f"{API}/tasks/{task_id}",
        headers={"Authorization": f"Bearer {c['secret_key']}"},
    )
    assert r.status_code == 404


def test_task_messages_arrive_in_fetch(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    client.post(
        f"{API}/send-message",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-b",
            "content": "hitung 2+2",
            "msg_type": "task",
            "task_id": task_id,
        },
    )
    msgs = fetch(client, b["secret_key"])["messages"]
    assert msgs[0]["msg_type"] == "task"
    assert msgs[0]["task_id"] == task_id
    # task_status is optional; not sending it means null.
    assert msgs[0]["task_status"] is None


def test_task_messages_arrive_in_fetch_with_status(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    client.post(
        f"{API}/send-message",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-b",
            "content": "hitung 2+2",
            "msg_type": "task",
            "task_id": task_id,
            "task_status": "pending",
        },
    )
    msgs = fetch(client, b["secret_key"])["messages"]
    assert msgs[0]["msg_type"] == "task"
    assert msgs[0]["task_id"] == task_id
    assert msgs[0]["task_status"] == "pending"


# ---------------------------------------------------------------------------
# JWT revoked when the password is changed
# ---------------------------------------------------------------------------

def test_password_change_invalidates_sessions(client):
    login = client.post(f"{API}/dashboard/login", json={"password": "123456"})
    assert login.status_code == 200
    assert client.get(f"{API}/dashboard/me").status_code == 200

    r = client.post(
        f"{API}/dashboard/change-password",
        json={"old_password": "123456", "new_password": "rahasia-baru-1"},
    )
    assert r.status_code == 200

    # The old session must be rejected.
    assert client.get(f"{API}/dashboard/me").status_code == 401
    # Login with the new password succeeds.
    assert client.post(
        f"{API}/dashboard/login", json={"password": "rahasia-baru-1"}
    ).status_code == 200


def test_dashboard_send_as_agent_still_works(client):
    from conftest import auth

    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    client.post(f"{API}/dashboard/login", json={"password": "123456"})
    r = client.post(
        f"{API}/dashboard/send-message",
        json={
            "as_agent": "hermes-a",
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-b",
            "content": "dari dashboard",
        },
    )
    assert r.status_code == 201
    msgs = fetch(client, b["secret_key"])["messages"]
    assert msgs[0]["content"] == "dari dashboard"


# ---------------------------------------------------------------------------
# New group members cannot see old files (bug #11)
# ---------------------------------------------------------------------------

def test_new_group_member_cannot_see_old_files(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    group = create_group(client, a["secret_key"], "research-team")
    client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
    )

    src = tmp_path / "lama.csv"
    src.write_bytes(b"data lama")
    with open(src, "rb") as f:
        r = client.post(
            "/api/v1/upload-file",
            headers={"Authorization": f"Bearer {a['secret_key']}"},
            files={"file": ("lama.csv", f, "text/csv")},
            data={
                "target_type": "group",
                "target_id": group["group_id"],
                "target_name": "research-team",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    assert r.status_code == 201
    file_id = r.json()["data"]["file_id"]

    # Did B join BEFORE the file was uploaded? No — we reverse the order: B joins first.
    # The file above is uploaded after B joins, so B may see it.
    r = client.get(
        f"{API}/files", headers={"Authorization": f"Bearer {b['secret_key']}"}
    )
    assert r.json()["data"]["total"] == 1

    # Now c: joins after the file was uploaded -> must not see it.
    c = register_agent(client, "hermes-c")
    client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {c['secret_key']}"},
    )
    r = client.get(
        f"{API}/files", headers={"Authorization": f"Bearer {c['secret_key']}"}
    )
    assert r.json()["data"]["total"] == 0

    # And the download must also be rejected even if the file_id is known.
    r = client.get(
        f"/api/v1/download-file/{file_id}",
        headers={"Authorization": f"Bearer {c['secret_key']}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Sniff: executable does not slip through via the ftyp trick (bug #9)
# ---------------------------------------------------------------------------

def test_executable_with_ftyp_bytes_rejected(client, tmp_path):
    """MZ at the start + 'ftyp' at offset 4-8 must still be detected as an executable."""
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "video.mp4"
    src.write_bytes(b"MZ\x90\x00ftypisom" + b"\x00" * 64)
    with open(src, "rb") as f:
        r = client.post(
            "/api/v1/upload-file",
            headers={"Authorization": f"Bearer {a['secret_key']}"},
            files={"file": ("video.mp4", f, "video/mp4")},
            data={
                "target_type": "direct",
                "target_id": b["agent_id"],
                "target_name": "hermes-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    assert r.status_code == 415
    assert r.json()["code"] == "EXECUTABLE_CONTENT"


def test_real_mp4_still_accepted(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypisom" + os.urandom(256))
    with open(src, "rb") as f:
        r = client.post(
            "/api/v1/upload-file",
            headers={"Authorization": f"Bearer {a['secret_key']}"},
            files={"file": ("clip.mp4", f, "video/mp4")},
            data={
                "target_type": "direct",
                "target_id": b["agent_id"],
                "target_name": "hermes-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    assert r.status_code == 201
    assert r.json()["data"]["mime_type"] == "video/mp4"


# ---------------------------------------------------------------------------
# Race condition: concurrent register & group creation -> 409, not 500
# ---------------------------------------------------------------------------

def test_concurrent_register_same_name(live_server, agents_dir):
    base = live_server.base_url
    payload = {"name": "race-reg"}

    def worker(_):
        return requests.post(
            f"{base}/register",
            headers={"X-Registration-Key": "test-registration-key"},
            json=payload,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(worker, range(6)))

    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409, 409, 409, 409, 409], codes
    # There must be no 500.
    assert all(r.status_code != 500 for r in responses)


def test_concurrent_create_group_same_name(live_server, agents_dir):
    base = live_server.base_url
    r = requests.post(
        f"{base}/register",
        headers={"X-Registration-Key": "test-registration-key"},
        json={"name": "race-gr-a"},
    )
    secret = r.json()["data"]["secret_key"]

    def worker(_):
        return requests.post(
            f"{base}/groups",
            headers={"Authorization": f"Bearer {secret}"},
            json={"name": "race-grup"},
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(worker, range(6)))

    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409, 409, 409, 409, 409], codes


# ---------------------------------------------------------------------------
# cleanup.py is safe against the reply_to FK
# ---------------------------------------------------------------------------

def test_cleanup_safe_with_reply_chain(client):
    from app.config import settings
    from app.database import connect

    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    first = send(client, a["secret_key"], "direct", "hermes-b", "pesan awal")
    # A reply that references the first message.
    client.post(
        "/api/v1/send-message",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": a["agent_id"],
            "target_name": "hermes-a",
            "content": "balasan",
            "reply_to": first["message_id"],
        },
    )

    # Run cleanup with a 0-day retention (all messages are "old").
    conn = connect()
    old_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert old_count == 2

    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "scripts" / "cleanup.py"),
            "--days",
            "0",
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert n == 0, "pesan lama harus terhapus tanpa crash FK"
