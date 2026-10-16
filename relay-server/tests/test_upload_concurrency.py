"""Upload concurrency & validation tests: staging + atomic commit.

All tests here use a real uvicorn server (the live_server fixture)
and many simultaneous threads, because race conditions only appear outside
the single-threaded TestClient.
"""
import hashlib
import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import pytest

REG_KEY = "test-registration-key"


@pytest.fixture(autouse=True)
def _relax_limits(monkeypatch):
    """The limiter singleton is shared across tests; relax it so that
    dozens of registrations do not hit 429 (register limit 5/minute)."""
    monkeypatch.setattr("app.config.settings.rate_limit_register", "10000/minute")
    monkeypatch.setattr("app.config.settings.rate_limit_default", "100000/minute")


def register(base_url: str, name: str) -> dict:
    r = requests.post(
        f"{base_url}/register",
        headers={"X-Registration-Key": REG_KEY},
        json={"name": name},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def upload(session: requests.Session, base_url: str, path: Path,
           target_id: str, target_name: str, filename: str | None = None,
           client_msg_id: str | None = None, content_type: str = "text/csv",
           caption: str | None = None):
    data = {
        "target_type": "direct",
        "target_id": target_id,
        "target_name": target_name,
        "client_msg_id": client_msg_id or str(uuid.uuid4()),
    }
    if caption is not None:
        data["caption"] = caption
    with open(path, "rb") as f:
        return session.post(
            f"{base_url}/upload-file",
            files={"file": (filename or path.name, f, content_type)},
            data=data,
        )


def download(session: requests.Session, base_url: str, file_id: str) -> bytes:
    r = session.get(f"{base_url}/download-file/{file_id}")
    assert r.status_code == 200, r.text
    return r.content


def count_rows(db_path: Path, sql: str, params=()) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def staging_files() -> list[Path]:
    from app.config import settings

    staging = settings.filemanager_dir / ".staging"
    return list(staging.glob("*.part")) if staging.is_dir() else []


# ---------------------------------------------------------------------------
# Same file name: no duplication and no corruption
# ---------------------------------------------------------------------------

def test_concurrent_same_filename_uploads(live_server, agents_dir, tmp_path):
    """10 simultaneous uploads with identical file names.

    Expectation: 10 distinct file_ids, 10 distinct physical files on disk, all
    contents intact (SHA-256 matches), 10 messages stored, staging clean.
    """
    base = live_server.base_url
    a = register(base, "race-a")
    b = register(base, "race-b")

    payload = b"id,nilai\n" + os.urandom(20_000)
    src = tmp_path / "laporan.csv"
    src.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    def worker(_):
        with requests.Session() as s:
            s.headers["Authorization"] = f"Bearer {a['secret_key']}"
            return upload(s, base, src, b["agent_id"], "race-b")

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(worker, range(10)))

    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    bodies = [r.json()["data"] for r in responses]
    file_ids = [d["file_id"] for d in bodies]
    assert len(set(file_ids)) == 10, "file_id harus unik per unggahan"

    from app.config import settings

    stored = [
        p.name
        for p in settings.filemanager_dir.iterdir()
        if p.is_file() and p.name != ".gitkeep"
    ]
    assert len(stored) == 10, f"harus 10 file fisik, dapat {len(stored)}"
    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM file_logs") == 10
    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM messages") == 10

    # All copies can be downloaded and their contents are intact.
    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {b['secret_key']}"
        for fid in file_ids:
            assert hashlib.sha256(download(s, base, fid)).hexdigest() == expected

    assert staging_files() == [], "staging harus bersih setelah sukses"


def test_concurrent_same_client_msg_id(live_server, agents_dir, tmp_path):
    """8 simultaneous uploads with an identical client_msg_id (double retry).

    Expectation: exactly 1 file_logs row + 1 message; one 201 response,
    the rest 200 duplicate; only 1 physical file.
    """
    base = live_server.base_url
    a = register(base, "idem-a")
    b = register(base, "idem-b")

    payload = b"isi idempotency" * 100
    src = tmp_path / "data.bin"
    src.write_bytes(payload)

    same_id = str(uuid.uuid4())

    def worker(_):
        with requests.Session() as s:
            s.headers["Authorization"] = f"Bearer {a['secret_key']}"
            return upload(s, base, src, b["agent_id"], "idem-b", client_msg_id=same_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(worker, range(8)))

    codes = [r.status_code for r in responses]
    assert all(c in (200, 201) for c in codes), [
        (r.status_code, r.text[:200]) for r in responses
    ]
    bodies = [r.json()["data"] for r in responses]
    fresh = [d for d in bodies if not d.get("duplicate")]
    assert len(fresh) == 1, "tepat satu unggahan yang benar-benar disimpan"
    assert len({d["file_id"] for d in bodies}) == 1, "semua balasan menunjuk file yang sama"

    from app.config import settings

    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM file_logs") == 1
    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM messages") == 1
    stored = [p for p in settings.filemanager_dir.iterdir() if p.is_file() and p.name != ".gitkeep"]
    assert len(stored) == 1
    assert staging_files() == []


def test_same_name_sequential_different_content(live_server, agents_dir, tmp_path):
    """Same name (client declaration), different content: two versions stored."""
    base = live_server.base_url
    a = register(base, "ver-a")
    b = register(base, "ver-b")

    first = b"versi pertama" * 50
    second = b"versi kedua berbeda total" * 50
    src1 = tmp_path / "v1.bin"
    src2 = tmp_path / "v2.bin"
    src1.write_bytes(first)
    src2.write_bytes(second)

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r1 = upload(s, base, src1, b["agent_id"], "ver-b", filename="laporan.csv")
        r2 = upload(s, base, src2, b["agent_id"], "ver-b", filename="laporan.csv")

    assert r1.status_code == r2.status_code == 201
    id1, id2 = r1.json()["data"]["file_id"], r2.json()["data"]["file_id"]
    assert id1 != id2
    assert r1.json()["data"]["sha256"] == hashlib.sha256(first).hexdigest()
    assert r2.json()["data"]["sha256"] == hashlib.sha256(second).hexdigest()

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {b['secret_key']}"
        assert download(s, base, id1) == first
        assert download(s, base, id2) == second

    from app.config import settings

    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM file_logs") == 2


def test_same_client_msg_id_is_idempotent_sequential(live_server, agents_dir, tmp_path):
    """Retry with the same client_msg_id: does not create a duplicate."""
    base = live_server.base_url
    a = register(base, "seq-a")
    b = register(base, "seq-b")
    src = tmp_path / "doc.bin"
    src.write_bytes(b"isi tetap")
    same_id = str(uuid.uuid4())

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r1 = upload(s, base, src, b["agent_id"], "seq-b", client_msg_id=same_id)
        r2 = upload(s, base, src, b["agent_id"], "seq-b", client_msg_id=same_id)

    assert r1.status_code == 201 and r1.json()["data"]["duplicate"] is False
    assert r2.status_code == 200 and r2.json()["data"]["duplicate"] is True
    assert r1.json()["data"]["file_id"] == r2.json()["data"]["file_id"]

    from app.config import settings

    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM file_logs") == 1


# ---------------------------------------------------------------------------
# Content validation: extension, magic bytes, size, quota
# ---------------------------------------------------------------------------

def test_executable_content_rejected_despite_safe_extension(live_server, agents_dir, tmp_path):
    """A .png file whose content is a PE executable (MZ) must be rejected with 415."""
    base = live_server.base_url
    a = register(base, "exe-a")
    b = register(base, "exe-b")

    src = tmp_path / "foto.png"
    src.write_bytes(b"MZ\x90\x00" + os.urandom(512))

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r = upload(s, base, src, b["agent_id"], "exe-b")

    assert r.status_code == 415
    assert r.json()["code"] == "EXECUTABLE_CONTENT"
    assert staging_files() == []

    from app.config import settings

    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM file_logs") == 0
    assert list(settings.filemanager_dir.glob("*")) == [] or not any(
        p.is_file() and p.name != ".gitkeep" for p in settings.filemanager_dir.iterdir()
    )


def test_shebang_script_rejected(live_server, agents_dir, tmp_path):
    base = live_server.base_url
    a = register(base, "sh-a")
    b = register(base, "sh-b")
    src = tmp_path / "catatan.txt"
    src.write_bytes(b"#!/bin/sh\nrm -rf /\n")

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r = upload(s, base, src, b["agent_id"], "sh-b")

    assert r.status_code == 415
    assert r.json()["code"] == "EXECUTABLE_CONTENT"
    assert staging_files() == []


def test_empty_file_rejected(live_server, agents_dir, tmp_path):
    base = live_server.base_url
    a = register(base, "emp-a")
    b = register(base, "emp-b")
    src = tmp_path / "kosong.txt"
    src.write_bytes(b"")

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r = upload(s, base, src, b["agent_id"], "emp-b")

    assert r.status_code == 400
    assert r.json()["code"] == "EMPTY_FILE"
    assert staging_files() == []


def test_oversize_leaves_no_staging(live_server, agents_dir, tmp_path, monkeypatch):
    from app.config import Settings, settings

    base = live_server.base_url
    a = register(base, "big-a")
    b = register(base, "big-b")
    # max_upload_bytes is a property: patch it on the class, not the instance.
    monkeypatch.setattr(Settings, "max_upload_bytes", 4096)

    src = tmp_path / "besar.bin"
    src.write_bytes(os.urandom(16 * 1024))

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r = upload(s, base, src, b["agent_id"], "big-b")

    assert r.status_code == 413
    assert r.json()["code"] == "FILE_TOO_LARGE"
    assert staging_files() == [], "file staging harus dibuang saat ditolak"

    from app.config import settings as s2

    assert count_rows(s2.db_path, "SELECT COUNT(*) FROM file_logs") == 0


def test_quota_exceeded(live_server, agents_dir, tmp_path, monkeypatch):
    from app.config import settings

    base = live_server.base_url
    a = register(base, "quota-a")
    b = register(base, "quota-b")
    monkeypatch.setattr(settings, "max_storage_mb_per_agent", 1)  # 1 MB quota

    src = tmp_path / "gede.bin"
    src.write_bytes(os.urandom(2 * 1024 * 1024))

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r = upload(s, base, src, b["agent_id"], "quota-b")

    assert r.status_code == 413
    assert r.json()["code"] == "QUOTA_EXCEEDED"
    assert staging_files() == []
    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM file_logs") == 0


def test_mime_detected_from_content(live_server, agents_dir, tmp_path):
    """A real PNG with a fake content_type: the stored mime is the detection result."""
    base = live_server.base_url
    a = register(base, "mime-a")
    b = register(base, "mime-b")

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da63f8ffff3f0300050001ffff5d1cd4e90000000049454e44ae426082"
    )
    src = tmp_path / "pixel.png"
    src.write_bytes(png)

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r = upload(
            s, base, src, b["agent_id"], "mime-b",
            content_type="application/octet-stream",
        )

    assert r.status_code == 201
    data = r.json()["data"]
    assert data["mime_type"] == "image/png"  # detection result, not the client claim
    assert data["declared_mime_type"] == "application/octet-stream"


def test_csv_keeps_extension_mime(live_server, agents_dir, tmp_path):
    """Plain text cannot be distinguished by magic bytes; the extension map wins."""
    base = live_server.base_url
    a = register(base, "csv-a")
    b = register(base, "csv-b")
    src = tmp_path / "data.csv"
    src.write_bytes(b"id,nilai\n1,10\n")

    with requests.Session() as s:
        s.headers["Authorization"] = f"Bearer {a['secret_key']}"
        r = upload(s, base, src, b["agent_id"], "csv-b", content_type="text/csv")

    assert r.json()["data"]["mime_type"] == "text/csv"


def test_strict_mime_match_rejects_mismatch(live_server, agents_dir, tmp_path, monkeypatch):
    from app.config import settings

    base = live_server.base_url
    a = register(base, "strict-a")
    b = register(base, "strict-b")
    monkeypatch.setattr(settings, "strict_mime_match", True)

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da63f8ffff3f0300050001ffff5d1cd4e90000000049454e44ae426082"
    )
    src = tmp_path / "foto.png"
    src.write_bytes(png)
    with open(src, "rb") as f:
        r = requests.post(
            f"{base}/upload-file",
            headers={"Authorization": f"Bearer {a['secret_key']}"},
            files={"file": ("laporan.pdf", f, "application/pdf")},
            data={
                "target_type": "direct",
                "target_id": b["agent_id"],
                "target_name": "strict-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )

    assert r.status_code == 415
    assert r.json()["code"] == "MIME_MISMATCH"
    assert staging_files() == []


def test_authorization_still_enforced(live_server, agents_dir, tmp_path):
    """Concurrency must not weaken authorization."""
    base = live_server.base_url
    a = register(base, "auth-a")
    b = register(base, "auth-b")
    c = register(base, "auth-c")
    src = tmp_path / "x.bin"
    src.write_bytes(b"rahasia")

    # C tries to send a direct file to B as if from A: without A's token.
    with open(src, "rb") as f:
        r = requests.post(
            f"{base}/upload-file",
            headers={"Authorization": f"Bearer {c['secret_key']}"},
            files={"file": ("x.bin", f, "application/octet-stream")},
            data={
                "target_type": "direct",
                "target_id": b["agent_id"],
                "target_name": "auth-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    # This is valid (C -> B); what must not be allowed: without a token, or to oneself.
    assert r.status_code == 201

    with open(src, "rb") as f:
        r = requests.post(
            f"{base}/upload-file",
            files={"file": ("x.bin", f, "application/octet-stream")},
            data={
                "target_type": "direct",
                "target_id": b["agent_id"],
                "target_name": "auth-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    assert r.status_code == 401

    with open(src, "rb") as f:
        r = requests.post(
            f"{base}/upload-file",
            headers={"Authorization": f"Bearer {b['secret_key']}"},
            files={"file": ("x.bin", f, "application/octet-stream")},
            data={
                "target_type": "direct",
                "target_id": b["agent_id"],
                "target_name": "auth-b",
                "client_msg_id": str(uuid.uuid4()),
            },
        )
    assert r.status_code == 400  # to oneself

    from app.config import settings

    assert count_rows(settings.db_path, "SELECT COUNT(*) FROM file_logs") == 1
    assert staging_files() == []
