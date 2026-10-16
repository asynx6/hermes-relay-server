"""Tests for roadmap features #1/#2/#3/#5.

#1 Long polling & SSE      — latency & request count
#2 Advanced group mgmt     — role, join_code, leave/kick, mention
#3 Token security          — scope, rotation + grace, audit log
#5 Chunked upload          — chunked, resumable, SHA-256 dedupe
"""
import hashlib
import json
import os
import threading
import time
import uuid

import pytest
import requests

from conftest import auth, create_group, fetch, join_group, register_agent, send

API = "/api/v1"
REG_KEY = "test-registration-key"


def _payload(target_id: str, target_name: str, content: str = "hai") -> dict:
    return {
        "client_msg_id": str(uuid.uuid4()),
        "target_type": "direct",
        "target_id": target_id,
        "target_name": target_name,
        "content": content,
    }


def _login(client, password: str = "123456"):
    return client.post(f"{API}/dashboard/login", json={"password": password})


def _register(base: str, name: str) -> dict:
    r = requests.post(
        f"{base}/register",
        headers={"X-Registration-Key": REG_KEY},
        json={"name": name},
        timeout=10,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _send_direct(base: str, secret: str, target_id: str, target_name: str,
                 content: str) -> None:
    r = requests.post(
        f"{base}/send-message",
        headers={"Authorization": f"Bearer {secret}"},
        json=_payload(target_id, target_name, content),
        timeout=10,
    )
    assert r.status_code == 201, r.text


# ===========================================================================
# #3 Token security: scope, rotation, audit
# ===========================================================================


def test_scoped_token_limits_endpoints(client):
    a = register_agent(client, "tok-a")
    b = register_agent(client, "tok-b")

    r = client.post(
        f"{API}/tokens",
        headers=auth(a["secret_key"]),
        json={"scopes": ["messages:read"], "label": "read-only"},
    )
    assert r.status_code == 201, r.text
    scoped = r.json()["data"]["secret_key"]
    assert scoped.startswith("hsk_")

    # Allowed: read messages.
    assert client.get(f"{API}/fetch-messages", headers=auth(scoped)).status_code == 200
    # Denied: send message (requires messages:write).
    r = client.post(
        f"{API}/send-message", headers=auth(scoped),
        json=_payload(b["agent_id"], "tok-b"),
    )
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"
    # Denied: upload a file.
    r = client.post(
        f"{API}/upload/init", headers=auth(scoped),
        json={
            "original_name": "x.bin", "total_size": 10,
            "sha256": "0" * 64, "chunk_size": 5,
            "target_type": "direct", "target_id": b["agent_id"],
            "target_name": "tok-b", "client_msg_id": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 403

    # The main token still has full access.
    assert client.post(
        f"{API}/send-message", headers=auth(a["secret_key"]),
        json=_payload(b["agent_id"], "tok-b"),
    ).status_code == 201


def test_token_revoke_and_unknown_scope(client):
    a = register_agent(client, "tk-a")

    r = client.post(
        f"{API}/tokens", headers=auth(a["secret_key"]),
        json={"scopes": ["files:read"]},
    )
    token_id = r.json()["data"]["token_id"]
    secret = r.json()["data"]["secret_key"]
    assert client.get(f"{API}/files", headers=auth(secret)).status_code == 200

    assert client.delete(
        f"{API}/tokens/{token_id}", headers=auth(a["secret_key"])
    ).status_code == 200
    # After revocation -> 401.
    assert client.get(f"{API}/files", headers=auth(secret)).status_code == 401

    # Unknown scope is rejected by validation.
    r = client.post(
        f"{API}/tokens", headers=auth(a["secret_key"]),
        json={"scopes": ["scope:ngawur"]},
    )
    assert r.status_code == 422


def test_rotate_token_grace_keeps_old_key(client):
    a = register_agent(client, "rot-a")
    old = a["secret_key"]

    r = client.post(f"{API}/rotate-token", headers=auth(old))
    assert r.status_code == 200, r.text
    new = r.json()["data"]["secret_key"]
    assert new != old

    # The new key takes effect immediately; the old key is still in the grace window.
    assert client.get(f"{API}/agents", headers=auth(new)).status_code == 200
    assert client.get(f"{API}/agents", headers=auth(old)).status_code == 200
    # A nonsense key is still rejected.
    assert client.get(f"{API}/agents", headers=auth("hsk_ngawur")).status_code == 401


def test_rotate_token_without_grace_rejects_old_key(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.token_grace_minutes", 0)
    a = register_agent(client, "rot0-a")
    old = a["secret_key"]

    new = client.post(f"{API}/rotate-token", headers=auth(old)).json()["data"]["secret_key"]
    assert client.get(f"{API}/agents", headers=auth(new)).status_code == 200
    assert client.get(f"{API}/agents", headers=auth(old)).status_code == 401


def test_audit_log_records_admin_actions(client):
    a = register_agent(client, "aud-a")
    b = register_agent(client, "aud-b")
    _login(client)

    r = client.post(
        f"{API}/dashboard/send-message",
        json={
            "as_agent": "aud-a", "target_type": "direct",
            "target_id": b["agent_id"], "target_name": "aud-b",
            "content": "kirim atas nama agent",
        },
    )
    assert r.status_code == 201, r.text

    data = client.get(f"{API}/dashboard/audit").json()["data"]
    entry = next(e for e in data["entries"] if e["action"] == "send_as_agent")
    assert entry["actor_type"] == "dashboard"
    assert "aud-a" in entry["target"]
    assert entry["result"] == "ok"


def test_audit_log_records_failed_login(client):
    _login(client, "password-salah")
    _login(client)  # correct login so the session is valid
    data = client.get(f"{API}/dashboard/audit").json()["data"]
    entry = next(e for e in data["entries"] if e["action"] == "login_failed")
    assert entry["result"] == "denied"


# ===========================================================================
# #2 Group management: role, join_code, leave/kick, mention
# ===========================================================================


def test_group_creator_is_admin_and_members_listed(client):
    a = register_agent(client, "grp-a")
    b = register_agent(client, "grp-b")
    g = create_group(client, a["secret_key"], "tim-riset")
    assert g["role"] == "admin"
    join_group(client, b["secret_key"], "tim-riset")

    data = client.get(
        f"{API}/groups/tim-riset/members", headers=auth(b["secret_key"])
    ).json()["data"]
    roles = {m["name"]: m["role"] for m in data["members"]}
    assert roles == {"grp-a": "admin", "grp-b": "member"}
    # Admin is listed first.
    assert data["members"][0]["name"] == "grp-a"


def test_non_member_cannot_list_members(client):
    a = register_agent(client, "grp2-a")
    c = register_agent(client, "grp2-c")
    create_group(client, a["secret_key"], "rahasia")
    r = client.get(f"{API}/groups/rahasia/members", headers=auth(c["secret_key"]))
    assert r.status_code == 403


def test_join_code_required_and_accepted(client):
    a = register_agent(client, "jc-a")
    b = register_agent(client, "jc-b")
    create_group(client, a["secret_key"], "tertutup")

    r = client.post(
        f"{API}/groups/tertutup/join-code", headers=auth(a["secret_key"]),
        json={"join_code": None},
    )
    assert r.status_code == 200
    code = r.json()["data"]["join_code"]
    assert code

    # Without a code -> 403.
    assert client.post(
        f"{API}/groups/tertutup/join", headers=auth(b["secret_key"])
    ).status_code == 403
    # Wrong code -> 403.
    assert client.post(
        f"{API}/groups/tertutup/join", headers=auth(b["secret_key"]),
        json={"join_code": "salah-salah"},
    ).status_code == 403
    # Correct code -> 200.
    r = client.post(
        f"{API}/groups/tertutup/join", headers=auth(b["secret_key"]),
        json={"join_code": code},
    )
    assert r.status_code == 200
    assert r.json()["data"]["already_member"] is False

    # Cleared -> back to open.
    assert client.delete(
        f"{API}/groups/tertutup/join-code", headers=auth(a["secret_key"])
    ).status_code == 200
    c = register_agent(client, "jc-c")
    assert client.post(
        f"{API}/groups/tertutup/join", headers=auth(c["secret_key"])
    ).status_code == 200


def test_kick_requires_admin_and_protects_last_admin(client):
    a = register_agent(client, "kick-a")
    b = register_agent(client, "kick-b")
    c = register_agent(client, "kick-c")
    create_group(client, a["secret_key"], "kick-grup")
    join_group(client, b["secret_key"], "kick-grup")
    join_group(client, c["secret_key"], "kick-grup")

    # A regular member cannot kick.
    r = client.delete(
        f"{API}/groups/kick-grup/members/kick-c", headers=auth(b["secret_key"])
    )
    assert r.status_code == 403

    # Admin can kick a regular member.
    r = client.delete(
        f"{API}/groups/kick-grup/members/kick-c", headers=auth(a["secret_key"])
    )
    assert r.status_code == 200
    names = [
        m["name"]
        for m in client.get(
            f"{API}/groups/kick-grup/members", headers=auth(a["secret_key"])
        ).json()["data"]["members"]
    ]
    assert "kick-c" not in names

    # The last admin cannot demote themselves.
    r = client.put(
        f"{API}/groups/kick-grup/members/kick-a/role",
        headers=auth(a["secret_key"]), json={"role": "member"},
    )
    assert r.status_code == 409


def test_leave_group_and_last_admin_guard(client):
    a = register_agent(client, "lv-a")
    b = register_agent(client, "lv-b")
    create_group(client, a["secret_key"], "lv-grup")
    join_group(client, b["secret_key"], "lv-grup")

    # The last admin may not leave while other members remain.
    r = client.post(f"{API}/groups/lv-grup/leave", headers=auth(a["secret_key"]))
    assert r.status_code == 409

    # Promote b to admin, then a may leave.
    assert client.put(
        f"{API}/groups/lv-grup/members/lv-b/role",
        headers=auth(a["secret_key"]), json={"role": "admin"},
    ).status_code == 200
    assert client.post(
        f"{API}/groups/lv-grup/leave", headers=auth(a["secret_key"])
    ).status_code == 200

    # b is now the last and only admin -> may leave.
    assert client.post(
        f"{API}/groups/lv-grup/leave", headers=auth(b["secret_key"])
    ).status_code == 200


def test_mention_recorded_for_group_member(client):
    a = register_agent(client, "mn-a")
    b = register_agent(client, "mn-b")
    g = create_group(client, a["secret_key"], "mn-grup")
    join_group(client, b["secret_key"], "mn-grup")

    send(
        client, a["secret_key"], "group", "mn-grup",
        "@mn-b tolong cek laporan", target_id=g["group_id"],
    )
    msgs = fetch(client, b["secret_key"])["messages"]
    assert msgs[0]["mentions"] == ["mn-b"]

    # Mentions to non-members (and non-agents) are ignored.
    send(
        client, a["secret_key"], "group", "mn-grup",
        "@orang-luar halo", target_id=g["group_id"],
    )
    msgs = fetch(client, b["secret_key"], since_id=msgs[0]["id"])["messages"]
    assert msgs[0]["mentions"] == []


# ===========================================================================
# #5 Chunked upload: chunked, resumable, dedupe
# ===========================================================================


def _init_upload(client, secret, payload: bytes, target_id, target_name,
                 chunk_size: int, sha: str | None = None, name: str = "besar.bin"):
    r = client.post(
        f"{API}/upload/init", headers=auth(secret),
        json={
            "original_name": name,
            "total_size": len(payload),
            "sha256": sha or hashlib.sha256(payload).hexdigest(),
            "chunk_size": chunk_size,
            "target_type": "direct",
            "target_id": target_id,
            "target_name": target_name,
            "client_msg_id": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _put_chunk(client, secret, upload_id: int, index: int, data: bytes):
    return client.put(
        f"{API}/upload/{upload_id}/chunk",
        params={"index": index},
        headers={**auth(secret), "Content-Type": "application/octet-stream"},
        content=data,
    )


def test_chunked_upload_roundtrip(client):
    a = register_agent(client, "ch-a")
    b = register_agent(client, "ch-b")
    payload = os.urandom(3000)
    init = _init_upload(client, a["secret_key"], payload, b["agent_id"], "ch-b", 1024)
    assert init["total_chunks"] == 3
    assert init["deduplicated"] is False

    uid = init["upload_id"]
    for i in range(3):
        r = _put_chunk(client, a["secret_key"], uid, i, payload[i * 1024:(i + 1) * 1024])
        assert r.status_code == 200, r.text
    assert r.json()["data"]["complete"] is True

    done = client.post(f"{API}/upload/{uid}/complete", headers=auth(a["secret_key"]))
    assert done.status_code == 201, done.text
    fid = done.json()["data"]["file_id"]
    assert done.json()["data"]["sha256"] == hashlib.sha256(payload).hexdigest()

    dl = client.get(f"{API}/download-file/{fid}", headers=auth(b["secret_key"]))
    assert dl.status_code == 200
    assert dl.content == payload


def test_chunked_upload_resume_reports_missing(client):
    a = register_agent(client, "rs-a")
    b = register_agent(client, "rs-b")
    payload = os.urandom(2500)
    init = _init_upload(client, a["secret_key"], payload, b["agent_id"], "rs-b", 1000)
    uid = init["upload_id"]

    # Send chunks 0 and 2, skip 1.
    assert _put_chunk(client, a["secret_key"], uid, 0, payload[0:1000]).status_code == 200
    assert _put_chunk(client, a["secret_key"], uid, 2, payload[2000:2500]).status_code == 200

    state = client.get(f"{API}/upload/{uid}", headers=auth(a["secret_key"])).json()["data"]
    assert state["missing"] == [1]
    assert state["complete"] is False

    # Complete before it is complete -> 409.
    assert client.post(
        f"{API}/upload/{uid}/complete", headers=auth(a["secret_key"])
    ).status_code == 409

    # Send the remainder, then complete.
    assert _put_chunk(client, a["secret_key"], uid, 1, payload[1000:2000]).status_code == 200
    done = client.post(f"{API}/upload/{uid}/complete", headers=auth(a["secret_key"]))
    assert done.status_code == 201, done.text


def test_chunked_upload_dedupe_skips_transfer(client):
    a = register_agent(client, "dd-a")
    b = register_agent(client, "dd-b")
    payload = os.urandom(2048)

    first = _init_upload(client, a["secret_key"], payload, b["agent_id"], "dd-b", 1024)
    for i in range(first["total_chunks"]):
        _put_chunk(client, a["secret_key"], first["upload_id"], i,
                   payload[i * 1024:(i + 1) * 1024])
    done1 = client.post(
        f"{API}/upload/{first['upload_id']}/complete", headers=auth(a["secret_key"])
    )
    assert done1.status_code == 201
    assert done1.json()["data"]["deduplicated"] is False

    # Same content, new client_msg_id: the server marks it duplicate, 0 chunks sent.
    second = _init_upload(client, a["secret_key"], payload, b["agent_id"], "dd-b", 1024)
    assert second["deduplicated"] is True

    done2 = client.post(
        f"{API}/upload/{second['upload_id']}/complete", headers=auth(a["secret_key"])
    )
    assert done2.status_code == 201, done2.text
    assert done2.json()["data"]["deduplicated"] is True
    assert done2.json()["data"]["file_id"] != done1.json()["data"]["file_id"]

    # Two rows, but the content on disk is shared (hardlink) -> downloads stay intact.
    for fid in (done1.json()["data"]["file_id"], done2.json()["data"]["file_id"]):
        dl = client.get(f"{API}/download-file/{fid}", headers=auth(b["secret_key"]))
        assert dl.content == payload


def test_chunked_upload_checksum_mismatch(client):
    a = register_agent(client, "ck-a")
    b = register_agent(client, "ck-b")
    payload = os.urandom(1500)
    init = _init_upload(
        client, a["secret_key"], payload, b["agent_id"], "ck-b", 1000,
        sha="a" * 64,  # intentionally wrong
    )
    uid = init["upload_id"]
    _put_chunk(client, a["secret_key"], uid, 0, payload[0:1000])
    _put_chunk(client, a["secret_key"], uid, 1, payload[1000:1500])

    r = client.post(f"{API}/upload/{uid}/complete", headers=auth(a["secret_key"]))
    assert r.status_code == 400
    assert r.json()["code"] == "CHECKSUM_MISMATCH"


def test_chunked_upload_rejects_executable_content(client):
    a = register_agent(client, "ex-a")
    b = register_agent(client, "ex-b")
    payload = b"MZ" + os.urandom(1500)
    init = _init_upload(client, a["secret_key"], payload, b["agent_id"], "ex-b", 1000)
    uid = init["upload_id"]
    _put_chunk(client, a["secret_key"], uid, 0, payload[0:1000])
    _put_chunk(client, a["secret_key"], uid, 1, payload[1000:1502])

    r = client.post(f"{API}/upload/{uid}/complete", headers=auth(a["secret_key"]))
    assert r.status_code == 415
    assert r.json()["code"] == "EXECUTABLE_CONTENT"


def test_chunked_upload_abort(client):
    a = register_agent(client, "ab-a")
    b = register_agent(client, "ab-b")
    payload = os.urandom(1024)
    init = _init_upload(client, a["secret_key"], payload, b["agent_id"], "ab-b", 512)
    uid = init["upload_id"]
    _put_chunk(client, a["secret_key"], uid, 0, payload[0:512])

    assert client.delete(
        f"{API}/upload/{uid}", headers=auth(a["secret_key"])
    ).status_code == 200
    assert client.post(
        f"{API}/upload/{uid}/complete", headers=auth(a["secret_key"])
    ).status_code == 409


# ===========================================================================
# #1 Long polling & SSE
# ===========================================================================

def test_long_poll_returns_immediately_when_message_ready(client):
    a = register_agent(client, "lp-a")
    b = register_agent(client, "lp-b")
    send(client, b["secret_key"], "direct", "lp-a", "sudah ada")

    t0 = time.monotonic()
    r = client.get(
        f"{API}/fetch-messages", headers=auth(a["secret_key"]),
        params={"since_id": 0, "wait": 10},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["messages"]) == 1
    assert time.monotonic() - t0 < 5


def test_long_poll_disabled_returns_immediately(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.long_poll_enabled", False)
    a = register_agent(client, "lpdis-a")
    t0 = time.monotonic()
    r = client.get(
        f"{API}/fetch-messages", headers=auth(a["secret_key"]),
        params={"since_id": 0, "wait": 10},
    )
    assert r.status_code == 200
    assert time.monotonic() - t0 < 3


def test_sse_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.sse_enabled", False)
    a = register_agent(client, "sseoff-a")
    r = client.get(f"{API}/events", headers=auth(a["secret_key"]))
    assert r.status_code == 404


def test_long_poll_waits_then_delivers(live_server, agents_dir):
    """The request is held until a new message arrives, then answered immediately."""
    base = live_server.base_url
    a = _register(base, "lpw-a")
    b = _register(base, "lpw-b")
    result: dict = {}
    started = threading.Event()

    def waiter():
        with requests.Session() as s:
            s.headers["Authorization"] = f"Bearer {a['secret_key']}"
            t0 = time.monotonic()
            started.set()
            r = s.get(
                f"{base}/fetch-messages",
                params={"since_id": 0, "wait": 15},
                timeout=(5, 40),
            )
            result["elapsed"] = time.monotonic() - t0
            result["messages"] = r.json()["data"]["messages"]

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    assert started.wait(5)
    time.sleep(1.5)  # give the request time to actually be held on the server
    _send_direct(base, b["secret_key"], a["agent_id"], "lpw-a", "bangun")
    thread.join(timeout=30)

    assert not thread.is_alive(), "long polling tidak kunjung selesai"
    assert result["messages"][0]["content"] == "bangun"
    assert result["elapsed"] < 12, "harusnya balas begitu pesan tiba, bukan menunggu timeout"


def test_sse_streams_new_message(live_server, agents_dir):
    base = live_server.base_url
    a = _register(base, "sse-a")
    b = _register(base, "sse-b")
    got: dict = {}
    connected = threading.Event()

    def listener():
        with requests.Session() as s:
            s.headers["Authorization"] = f"Bearer {a['secret_key']}"
            with s.get(
                f"{base}/events", params={"since_id": 0},
                stream=True, timeout=(5, 40),
            ) as resp:
                assert resp.status_code == 200
                event = None
                for raw in resp.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    if raw.startswith("event:"):
                        event = raw[6:].strip()
                        if event == "epoch":
                            connected.set()  # connection ready; safe to send
                    elif raw.startswith("data:") and event == "messages":
                        got["payload"] = json.loads(raw[5:].strip())
                        return

    thread = threading.Thread(target=listener, daemon=True)
    thread.start()
    assert connected.wait(15), "SSE tidak terhubung"
    _send_direct(base, b["secret_key"], a["agent_id"], "sse-a", "ping")
    thread.join(timeout=30)

    assert "payload" in got, "SSE tidak mengirim event messages"
    assert got["payload"]["messages"][0]["content"] == "ping"
