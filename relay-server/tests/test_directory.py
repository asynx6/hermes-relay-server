"""Test direktori: agent bisa melihat siapa saja yang terdaftar dan grup apa yang ada."""
from conftest import register_agent


def test_list_agents_requires_auth(client):
    r = client.get("/api/v1/agents")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_list_agents_returns_all_registered(client):
    a = register_agent(client, "hermes-a")
    register_agent(client, "hermes-b")
    register_agent(client, "hermes-c")

    r = client.get(
        "/api/v1/agents", headers={"Authorization": f"Bearer {a['secret_key']}"}
    )
    assert r.status_code == 200
    agents = r.json()["data"]["agents"]
    assert [x["name"] for x in agents] == ["hermes-a", "hermes-b", "hermes-c"]
    for x in agents:
        assert x["agent_id"].startswith("ag_")
        assert x["status"] == "active"
        assert "last_seen_at" in x and "created_at" in x


def test_list_agents_shows_revoked_status(client):
    from app.database import connect

    a = register_agent(client, "hermes-a")
    register_agent(client, "hermes-b")
    conn = connect()
    conn.execute("UPDATE agents SET status = 'revoked' WHERE name = 'hermes-b'")
    conn.commit()
    conn.close()

    r = client.get(
        "/api/v1/agents", headers={"Authorization": f"Bearer {a['secret_key']}"}
    )
    by_name = {x["name"]: x for x in r.json()["data"]["agents"]}
    assert by_name["hermes-b"]["status"] == "revoked"


def test_list_groups_requires_auth(client):
    r = client.get("/api/v1/groups")
    assert r.status_code == 401


def test_list_groups_membership_flag(client):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")

    client.post(
        "/api/v1/groups",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
        json={"name": "research-team"},
    )
    client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
    )

    def groups_for(secret):
        r = client.get("/api/v1/groups", headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 200
        return r.json()["data"]["groups"]

    # Pembuat dan yang join: anggota. C: bukan anggota.
    for agent in (a, b):
        groups = groups_for(agent["secret_key"])
        assert len(groups) == 1
        assert groups[0]["name"] == "research-team"
        assert groups[0]["is_member"] is True
        assert groups[0]["member_count"] == 2
        assert groups[0]["joined_at"]

    groups = groups_for(c["secret_key"])
    assert groups[0]["is_member"] is False
    assert groups[0]["joined_at"] is None
