"""Test upload dan download file (Fase 2)."""
import hashlib
import os
import uuid
from pathlib import Path

from conftest import create_group, fetch, register_agent, settings

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da63f8ffff3f0300050001ffff5d1cd4e90000000049454e44ae426082"
)


def upload(client, secret, path, target_type, target_name, target_id,
           client_msg_id=None, caption=None):
    data = {
        "target_type": target_type,
        "target_name": target_name,
        "target_id": target_id,
    }
    if client_msg_id:
        data["client_msg_id"] = client_msg_id
    else:
        data["client_msg_id"] = str(uuid.uuid4())
    if caption is not None:
        data["caption"] = caption
    with open(path, "rb") as f:
        return client.post(
            "/api/v1/upload-file",
            headers={"Authorization": f"Bearer {secret}"},
            files={"file": (os.path.basename(path), f, "application/octet-stream")},
            data=data,
        )


def test_upload_direct_and_download_roundtrip(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")

    src = tmp_path / "dataset.bin"
    payload = os.urandom(64 * 1024)
    src.write_bytes(payload)

    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"], caption="Dataset")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["file_id"].startswith("fl_")
    assert data["size_bytes"] == len(payload)
    assert data["sha256"] == hashlib.sha256(payload).hexdigest()
    assert data["original_name"] == "dataset.bin"

    # Penerima melihat pesan file.
    msgs = fetch(client, b["secret_key"])["messages"]
    assert len(msgs) == 1
    assert msgs[0]["msg_type"] == "file"
    assert msgs[0]["file"]["file_id"] == data["file_id"]
    assert msgs[0]["content"] == "Dataset"

    # Unduh dan verifikasi isi.
    r = client.get(
        f"/api/v1/download-file/{data['file_id']}",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
    )
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["x-file-sha256"] == data["sha256"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in r.headers["content-disposition"]

    # Uploader juga boleh mengunduh.
    r = client.get(
        f"/api/v1/download-file/{data['file_id']}",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    )
    assert r.status_code == 200


def test_non_recipient_cannot_download(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")

    src = tmp_path / "rahasia.txt"
    src.write_bytes(b"isi rahasia")
    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])
    file_id = r.json()["data"]["file_id"]

    r = client.get(
        f"/api/v1/download-file/{file_id}",
        headers={"Authorization": f"Bearer {c['secret_key']}"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


def test_download_without_auth(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "x.txt"
    src.write_bytes(b"x")
    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])
    r = client.get(f"/api/v1/download-file/{r.json()['data']['file_id']}")
    assert r.status_code == 401


def test_download_nonexistent(client):
    a = register_agent(client, "hermes-a")
    r = client.get(
        "/api/v1/download-file/fl_tidakada",
        headers={"Authorization": f"Bearer {a['secret_key']}"},
    )
    assert r.status_code == 404


def test_upload_to_unknown_agent(client, tmp_path):
    a = register_agent(client, "hermes-a")
    src = tmp_path / "x.txt"
    src.write_bytes(b"x")
    r = upload(client, a["secret_key"], src, "direct", "hermes-ghost", "ag_0000000000")
    assert r.status_code == 404


def test_blocked_extension(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "malware.exe"
    src.write_bytes(b"MZ")
    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])
    # Target valid tapi ekstensi diblokir: 415. Tidak ada file tersisa.
    assert r.status_code == 415
    assert r.json()["code"] == "UNSUPPORTED_TYPE"
    # Tidak ada file tersisa (direktori .staging boleh ada, harus kosong).
    assert not [p for p in settings.filemanager_dir.iterdir() if p.is_file()]
    assert not list((settings.filemanager_dir / ".staging").glob("*.part"))


def test_blocked_extension_with_valid_recipient(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "virus.bat"
    src.write_bytes(b"@echo off")
    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])
    assert r.status_code == 415


def test_dangerous_filename_sanitized(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")

    for dangerous in ("../../etc/passwd", "a b;rm.sh", "..\\..\\win.ini", "  "):
        src = tmp_path / "payload.bin"
        src.write_bytes(b"isi aman")
        with open(src, "rb") as f:
            r = client.post(
                "/api/v1/upload-file",
                headers={"Authorization": f"Bearer {a['secret_key']}"},
                files={"file": (dangerous, f, "application/octet-stream")},
                data={
                    "target_type": "direct",
                    "target_id": b["agent_id"],
                    "target_name": "hermes-b",
                    "client_msg_id": str(uuid.uuid4()),
                },
            )
        assert r.status_code == 201, (dangerous, r.text)
        data = r.json()["data"]
        assert "/" not in data["original_name"] and "\\" not in data["original_name"]
        stored = settings.filemanager_dir / (
            # cek nama acak uuid di disk
            [p.name for p in settings.filemanager_dir.iterdir() if p.is_file()][-1]
        )
        assert stored.parent == settings.filemanager_dir.resolve()

    # Semua file tersimpan di filemanager/, jumlahnya sesuai.
    files = [p for p in settings.filemanager_dir.iterdir() if p.is_file()]
    assert len(files) == 4
    for p in files:
        assert len(p.stem) == 32  # uuid4 hex


def test_upload_over_limit_rejected_no_leftover(client, tmp_path, monkeypatch):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    # max_upload_bytes adalah property; patch di class agar instance melihat nilai baru.
    from app.config import Settings

    monkeypatch.setattr(Settings, "max_upload_bytes", 1024)

    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(4096))
    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])
    assert r.status_code == 413
    assert r.json()["code"] == "FILE_TOO_LARGE"
    assert not [p for p in settings.filemanager_dir.iterdir() if p.is_file()]
    assert not list((settings.filemanager_dir / ".staging").glob("*.part"))

    # Tidak ada pesan yang tersimpan.
    assert fetch(client, b["secret_key"])["messages"] == []


def test_group_file_all_members_download(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")
    outsider = register_agent(client, "hermes-x")

    group = create_group(client, a["secret_key"], "research-team")
    for agent in (b, c):
        client.post(
            "/api/v1/groups/research-team/join",
            headers={"Authorization": f"Bearer {agent['secret_key']}"},
        )

    src = tmp_path / "dataset.csv"
    payload = b"id,nilai\n1,10\n2,20\n" * 100
    src.write_bytes(payload)

    r = upload(
        client, a["secret_key"], src, "group", "research-team",
        group["group_id"], caption="Dataset scraping",
    )
    assert r.status_code == 201
    file_id = r.json()["data"]["file_id"]

    for agent in (b, c):
        msgs = fetch(client, agent["secret_key"])["messages"]
        assert len(msgs) == 1
        assert msgs[0]["msg_type"] == "file"
        r = client.get(
            f"/api/v1/download-file/{file_id}",
            headers={"Authorization": f"Bearer {agent['secret_key']}"},
        )
        assert r.status_code == 200
        assert r.content == payload

    # Non-member grup tidak bisa mengunduh.
    r = client.get(
        f"/api/v1/download-file/{file_id}",
        headers={"Authorization": f"Bearer {outsider['secret_key']}"},
    )
    assert r.status_code == 403


def test_duplicate_upload_client_msg_id(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "doc.txt"
    src.write_bytes(b"isi")
    client_msg_id = str(uuid.uuid4())

    r1 = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"], client_msg_id=client_msg_id)
    assert r1.status_code == 201
    file_id = r1.json()["data"]["file_id"]

    r2 = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"], client_msg_id=client_msg_id)
    assert r2.status_code == 200
    assert r2.json()["data"]["duplicate"] is True
    assert r2.json()["data"]["file_id"] == file_id

    # Hanya satu file di disk dan satu pesan.
    files = [p for p in settings.filemanager_dir.iterdir() if p.is_file()]
    assert len(files) == 1
    assert len(fetch(client, b["secret_key"])["messages"]) == 1


def test_deleted_file_returns_410(client, tmp_path):
    from app.database import connect

    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "doc.txt"
    src.write_bytes(b"isi")
    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])
    file_id = r.json()["data"]["file_id"]

    conn = connect()
    conn.execute(
        "UPDATE file_logs SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE file_id = ?",
        (file_id,),
    )
    conn.commit()
    conn.close()

    r = client.get(
        f"/api/v1/download-file/{file_id}",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
    )
    assert r.status_code == 410
    assert r.json()["code"] == "GONE"


def test_png_upload_content_type(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "pixel.png"
    src.write_bytes(PNG_BYTES)
    with open(src, "rb") as f:
        r = client.post(
            "/api/v1/upload-file",
            headers={"Authorization": f"Bearer {a['secret_key']}"},
            files={"file": ("pixel.png", f, "image/png")},
            data={
                "target_type": "direct",
                "target_id": b["agent_id"],
                "target_name": "hermes-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    assert r.status_code == 201
    assert r.json()["data"]["mime_type"] == "image/png"
    assert r.json()["data"]["size_bytes"] == len(PNG_BYTES)


# ---------------------------------------------------------------------------
# GET /files — daftar file yang bisa diakses agent
# ---------------------------------------------------------------------------

def test_list_files_requires_auth(client):
    r = client.get("/api/v1/files")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


def test_list_files_shows_name_id_size(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")

    payload = os.urandom(3000)
    src = tmp_path / "laporan.pdf"
    src.write_bytes(payload)
    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])
    assert r.status_code == 201
    file_id = r.json()["data"]["file_id"]

    # Penerima melihat file dengan ketiga atribut: nama, ID, ukuran.
    data = client.get(
        "/api/v1/files", headers={"Authorization": f"Bearer {b['secret_key']}"}
    ).json()["data"]
    assert data["total"] == 1
    f = data["files"][0]
    assert f["file_id"] == file_id
    assert f["original_name"] == "laporan.pdf"
    assert f["size_bytes"] == 3000
    assert f["uploader"] == "hermes-a"
    assert f["scope"] == "direct"
    assert f["target"] == "hermes-b"
    assert f["uploaded_at"]
    assert data["has_more"] is False

    # Pengunggah juga melihat file-nya sendiri.
    data = client.get(
        "/api/v1/files", headers={"Authorization": f"Bearer {a['secret_key']}"}
    ).json()["data"]
    assert data["total"] == 1
    assert data["files"][0]["file_id"] == file_id


def test_list_files_hides_others_direct_files(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    c = register_agent(client, "hermes-c")

    src = tmp_path / "rahasia.bin"
    src.write_bytes(b"x" * 100)
    upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])

    # C tidak melihat file direct A->B.
    data = client.get(
        "/api/v1/files", headers={"Authorization": f"Bearer {c['secret_key']}"}
    ).json()["data"]
    assert data["total"] == 0
    assert data["files"] == []


def test_list_files_group_membership(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    outsider = register_agent(client, "hermes-x")
    group = create_group(client, a["secret_key"], "research-team")
    client.post(
        "/api/v1/groups/research-team/join",
        headers={"Authorization": f"Bearer {b['secret_key']}"},
    )

    src = tmp_path / "dataset.csv"
    src.write_bytes(b"id\n1\n2\n")
    r = upload(
        client, a["secret_key"], src, "group", "research-team",
        group["group_id"],
    )
    file_id = r.json()["data"]["file_id"]

    for agent in (a, b):
        data = client.get(
            "/api/v1/files", headers={"Authorization": f"Bearer {agent['secret_key']}"}
        ).json()["data"]
        assert data["total"] == 1
        assert data["files"][0]["scope"] == "group"
        assert data["files"][0]["target"] == "research-team"

    data = client.get(
        "/api/v1/files", headers={"Authorization": f"Bearer {outsider['secret_key']}"}
    ).json()["data"]
    assert data["total"] == 0


def test_list_files_excludes_deleted(client, tmp_path):
    from app.database import connect

    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")
    src = tmp_path / "doc.txt"
    src.write_bytes(b"isi")
    r = upload(client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"])
    file_id = r.json()["data"]["file_id"]

    conn = connect()
    conn.execute(
        "UPDATE file_logs SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE file_id = ?",
        (file_id,),
    )
    conn.commit()
    conn.close()

    data = client.get(
        "/api/v1/files", headers={"Authorization": f"Bearer {b['secret_key']}"}
    ).json()["data"]
    assert data["total"] == 0


def test_list_files_pagination(client, tmp_path):
    a = register_agent(client, "hermes-a")
    b = register_agent(client, "hermes-b")

    src = tmp_path / "batch.bin"
    src.write_bytes(b"data")
    for i in range(3):
        upload(
            client, a["secret_key"], src, "direct", "hermes-b", b["agent_id"],
            client_msg_id=str(uuid.uuid4()),
        )

    h = {"Authorization": f"Bearer {b['secret_key']}"}
    page1 = client.get("/api/v1/files?limit=2&offset=0", headers=h).json()["data"]
    assert len(page1["files"]) == 2
    assert page1["total"] == 3
    assert page1["has_more"] is True

    page2 = client.get("/api/v1/files?limit=2&offset=2", headers=h).json()["data"]
    assert len(page2["files"]) == 1
    assert page2["has_more"] is False

    # Tidak ada tumpang tindih antar halaman.
    ids1 = {f["file_id"] for f in page1["files"]}
    ids2 = {f["file_id"] for f in page2["files"]}
    assert ids1.isdisjoint(ids2)


def test_list_files_limit_bounds(client):
    a = register_agent(client, "hermes-a")
    r = client.get(
        "/api/v1/files?limit=0", headers={"Authorization": f"Bearer {a['secret_key']}"}
    )
    assert r.status_code == 422
    r = client.get(
        "/api/v1/files?limit=201", headers={"Authorization": f"Bearer {a['secret_key']}"}
    )
    assert r.status_code == 422
