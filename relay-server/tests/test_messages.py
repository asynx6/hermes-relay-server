"""Tests for direct and group messaging (Phase 2)."""
import sqlite3
import uuid

from conftest import create_group, fetch, register_agent, send


def test_direct_message_delivered_to_recipient_only(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")

    data = send(client, a["secret_key"], "direct", "hermes-b", "Halo Beta")
    assert data["message_id"] > 0
    assert data["target"] == "hermes-b"

    msgs = fetch(client, b["secret_key"])["messages"]
    assert len(msgs) == 1
    assert msgs[0]["from"] == "hermes-a"
    assert msgs[0]["scope"] == "direct"
    assert msgs[0]["content"] == "Halo Beta"

    assert fetch(client, c["secret_key"])["messages"] == []


def test_sender_does_not_receive_own_message(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    send(client, a["secret_key"], "direct", "hermes-b", "echo")
    assert fetch(client, a["secret_key"])["messages"] == []


def test_send_to_unknown_agent(client):
    a = register_agent(client, "hermes-a")
    r = client.post(
        "/api/v1/send-message",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": "ag_0000000000",
            "target_name": "hermes-ghost",
            "content": "boo",
        },
    )
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


def test_send_to_self_rejected(client):
    a = register_agent(client, "hermes-a")
    r = client.post(
        "/api/v1/send-message",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": a["agent_id"],
            "target_name": "hermes-a",
            "content": "me",
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "BAD_REQUEST"


def test_idempotent_retry_same_client_msg_id(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    client_msg_id = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {a['secret_key']}"}

    r1 = client.post(
        "/api/v1/send-message",
        headers=headers,
        json={
            "client_msg_id": client_msg_id,
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-b",
            "content": "sama",
        },
    )
    assert r1.status_code == 201
    body1 = r1.json()["data"]

    r2 = client.post(
        "/api/v1/send-message",
        headers=headers,
        json={
            "client_msg_id": client_msg_id,
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-b",
            "content": "sama",
        },
    )
    assert r2.status_code == 200
    body2 = r2.json()["data"]
    assert body2["duplicate"] is True
    assert body2["message_id"] == body1["message_id"]

    # Only one row in the database.
    from conftest import settings

    conn = sqlite3.connect(settings.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_content_length_and_uuid_validation(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    h = {"Authorization": f"Bearer {a['secret_key']}"}

    r = client.post(
        "/api/v1/send-message", headers=h,
        json={"client_msg_id": "bukan-uuid", "target_type": "direct",
              "target_id": b["agent_id"], "target_name": "hermes-b", "content": "x"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"

    r = client.post(
        "/api/v1/send-message", headers=h,
        json={"client_msg_id": str(uuid.uuid4()), "target_type": "direct",
              "target_id": b["agent_id"], "target_name": "hermes-b", "content": ""},
    )
    assert r.status_code == 422

    r = client.post(
        "/api/v1/send-message", headers=h,
        json={"client_msg_id": str(uuid.uuid4()), "target_type": "direct",
              "target_id": b["agent_id"], "target_name": "hermes-b",
              "content": "x" * 8001},
    )
    assert r.status_code == 422

    r = client.post(
        "/api/v1/send-message", headers=h,
        json={"client_msg_id": str(uuid.uuid4()), "target_type": "channel",
              "target_id": b["agent_id"], "target_name": "hermes-b", "content": "x"},
    )
    assert r.status_code == 422


def test_group_message_reaches_all_members_not_sender(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")
    d = register_agent(client, "hermes-d")

    group = create_group(client, a["secret_key"], "research-team", "Grup riset")
    assert group["group_id"].startswith("gr_")

    for agent in (b, c):
        r = client.post(
            "/api/v1/groups/research-team/join",
            headers={"Authorization": f"Bearer {agent['secret_key']}"},
        )
        assert r.status_code == 200

    send(
        client, a["secret_key"], "group", "research-team",
        "Update: dataset siap", target_id=group["group_id"],
    )

    for agent in (b, c):
        msgs = fetch(client, agent["secret_key"])["messages"]
        assert len(msgs) == 1
        assert msgs[0]["scope"] == "group"
        assert msgs[0]["group"] == "research-team"
        assert msgs[0]["group_id"] == group["group_id"]
        assert msgs[0]["content"] == "Update: dataset siap"

    # A non-member does not see group messages.
    assert fetch(client, d["secret_key"])["messages"] == []
    # The sender does not receive their own message.
    assert fetch(client, a["secret_key"])["messages"] == []


def test_non_member_cannot_send_to_group(client):
    a = register_agent(client, "hermes-a")
    outsider = register_agent(client, "hermes-x")
    group = create_group(client, a["secret_key"], "research-team")
    r = client.post(
        "/api/v1/send-message",
        headers={"Authorization": f"Bearer {outsider['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "group",
            "target_id": group["group_id"],
            "target_name": "research-team",
            "content": "intrusi",
        },
    )
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


def test_send_with_mismatched_id_and_name(client):
    """ID and name must match — otherwise 404 (not sent to just anyone)."""
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")
    h = {"Authorization": f"Bearer {a['secret_key']}"}

    # Agent B's ID but agent C's name.
    r = client.post(
        "/api/v1/send-message", headers=h,
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-c",
            "content": "salah alamat",
        },
    )
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"

    # Valid ID, unknown name.
    r = client.post(
        "/api/v1/send-message", headers=h,
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": b["agent_id"],
            "target_name": "hermes-asing",
            "content": "salah alamat",
        },
    )
    assert r.status_code == 404

    # ID with the wrong format.
    r = client.post(
        "/api/v1/send-message", headers=h,
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": "ag_salah",
            "target_name": "hermes-b",
            "content": "salah alamat",
        },
    )
    assert r.status_code == 422


def test_new_member_only_sees_messages_after_joining(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")

    group = create_group(client, a["secret_key"], "research-team")
    client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
    )
    send(
        client, a["secret_key"], "group", "research-team", "pesan-1",
        target_id=group["group_id"],
    )
    # C joins after pesan-1 was sent.
    client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {c['secret_key']}"},
    )
    send(
        client, a["secret_key"], "group", "research-team", "pesan-2",
        target_id=group["group_id"],
    )

    b_msgs = [m["content"] for m in fetch(client, b["secret_key"])["messages"]]
    c_msgs = [m["content"] for m in fetch(client, c["secret_key"])["messages"]]
    assert b_msgs == ["pesan-1", "pesan-2"]
    assert c_msgs == ["pesan-2"]


def test_join_group_idempotent(client):
    a = register_agent(client, "hermes-a")
    client.post(
        "/api/v1/groups", headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={"name": "research-team"},
    )
    r1 = client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    )
    r2 = client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    )
    assert r1.status_code == r2.status_code == 200


def test_duplicate_group_name(client):
    a = register_agent(client, "hermes-a")
    r = client.post(
        "/api/v1/groups", headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={"name": "research-team"},
    )
    assert r.status_code == 201
    # Valid names are lowercase only; an exact duplicate must be 409.
    r = client.post(
        "/api/v1/groups", headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={"name": "research-team"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"


def test_group_name_uppercase_rejected(client):
    a = register_agent(client, "hermes-a")
    r = client.post(
        "/api/v1/groups", headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={"name": "RESEARCH-TEAM"},
    )
    assert r.status_code == 422


def test_target_lookup_case_insensitive(client):
    """Agent/group names are looked up case-insensitively (COLLATE NOCASE)."""
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    data = send(client, a["secret_key"], "direct", "HERMES-B", "halo")
    assert data["target"] == "HERMES-B"
    msgs = fetch(client, b["secret_key"])["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] == "halo"


def test_join_unknown_group(client):
    a = register_agent(client, "hermes-a")
    r = client.post(
        "/api/v1/groups/nonexistent/join",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    )
    assert r.status_code == 404


def test_cursor_since_id_and_limit(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    for i in range(3):
        send(client, a["secret_key"], "direct", "hermes-b", f"pesan-{i}")

    d = fetch(client, b["secret_key"])
    assert len(d["messages"]) == 3
    assert d["next_since_id"] == 3
    assert d["has_more"] is False

    # since_id at the end: no new messages.
    d2 = fetch(client, b["secret_key"], since_id=3)
    assert d2["messages"] == []
    assert d2["next_since_id"] == 3

    # limit 2: has_more true.
    d3 = fetch(client, b["secret_key"], limit=2)
    assert len(d3["messages"]) == 2
    assert d3["has_more"] is True
    assert d3["next_since_id"] == 2


def test_reply_to(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    first = send(client, a["secret_key"], "direct", "hermes-b", "pertanyaan")

    r = client.post(
        "/api/v1/send-message",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": a["agent_id"],
            "target_name": "hermes-a",
            "content": "jawaban",
            "reply_to": first["message_id"],
        },
    )
    assert r.status_code == 201

    msgs = fetch(client, a["secret_key"])["messages"]
    assert msgs[-1]["reply_to"] == first["message_id"]

    # reply to a message that does not exist / is not visible.
    r = client.post(
        "/api/v1/send-message",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
        json={
            "client_msg_id": str(uuid.uuid4()),
            "target_type": "direct",
            "target_id": a["agent_id"],
            "target_name": "hermes-a",
            "content": "halo",
            "reply_to": 99999,
        },
    )
    assert r.status_code == 404


def test_fetch_limit_max_100(client):
    a = register_agent(client, "hermes-a")
    r = client.get(
        "/api/v1/fetch-messages?limit=101",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    )
    assert r.status_code == 422
