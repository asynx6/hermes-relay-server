"""Klien HTTP ke relay server: satu Session, retry dengan backoff + jitter.

Retry hanya untuk kegagalan sementara (ConnectionError, Timeout, 429, 5xx).
Kesalahan 4xx lain bersifat permanen dan langsung dikembalikan ke pemanggil.
send-message dan upload-file aman di-retry karena client_msg_id idempotent.
"""
import hashlib
import json
import os
import random
import time
from urllib.parse import quote

import requests

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
AUTH_REJECTED_MSG = "Token ditolak server. Hentikan polling dan periksa config."


class RelayUnavailable(Exception):
    """Server tidak bisa dijangkau setelah semua percobaan."""


class AuthRejected(Exception):
    """Server menolak token (401). Polling harus berhenti, bukan retry."""


def parse_json(resp) -> dict:
    """Parse respons JSON dengan aman. Halaman HTML dari proxy/CDN tidak
    boleh menjadi JSONDecodeError mentah — jadi error yang informatif."""
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        preview = (resp.text or "")[:200].replace("\n", " ")
        raise RuntimeError(
            f"Respons bukan JSON (HTTP {resp.status_code}): {preview}"
        )


def request_with_retry(session, method, url, max_retries=5, base=1.0, cap=60.0, **kwargs):
    kwargs.setdefault("timeout", (5, 30))
    for attempt in range(max_retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code == 401:
                raise AuthRejected(AUTH_REJECTED_MSG)
            if resp.status_code not in RETRYABLE_STATUS:
                return resp
        except (requests.ConnectionError, requests.Timeout):
            pass
        if attempt == max_retries:
            break
        delay = min(cap, base * 2**attempt) * random.uniform(0.5, 1.5)
        time.sleep(delay)
    raise RelayUnavailable(f"{method} {url} gagal setelah {max_retries + 1} percobaan")


class RelayAPI:
    def __init__(self, config, retry_max: int = 5, retry_base: float = 1.0):
        self.config = config
        self.retry_max = retry_max
        self.retry_base = retry_base
        self.session = requests.Session()
        if config.secret_key:
            self.session.headers["Authorization"] = f"Bearer {config.secret_key}"

    @property
    def base(self) -> str:
        return self.config.server_url

    def _backoff(self, attempt: int) -> None:
        time.sleep(min(60.0, self.retry_base * 2**attempt) * random.uniform(0.5, 1.5))

    # ---- registrasi ----

    def register(self, registration_nonce: str | None = None) -> dict:
        """Daftarkan agent. Kalau nonce dikirim dan respons sebelumnya
        hilang, server memutar ulang secret yang sama alih-alih 409."""
        payload = {
            "name": self.config.agent_name,
            "description": "Hermes agent",
            "metadata": {"client_version": "1.0.0"},
        }
        if registration_nonce:
            payload["registration_nonce"] = registration_nonce
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/register",
            max_retries=self.retry_max,
            base=self.retry_base,
            headers={"X-Registration-Key": self.config.registration_key},
            json=payload,
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"Registrasi gagal: {resp.status_code} {parse_json(resp)}"
            )
        return parse_json(resp)["data"]

    # ---- pesan ----

    def send_message(self, target_type: str, target_id: str, target_name: str,
                     content: str, client_msg_id: str,
                     reply_to: int | None = None,
                     msg_type: str = "text",
                     task_id: str | None = None,
                     task_status: str | None = None) -> dict:
        """Kirim pesan. Target selalu pasangan (target_id, target_name).

        msg_type 'task'/'result' memakai task_id untuk pola RPC ringan.
        """
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/send-message",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={
                "client_msg_id": client_msg_id,
                "target_type": target_type,
                "target_id": target_id,
                "target_name": target_name,
                "content": content,
                "reply_to": reply_to,
                "msg_type": msg_type,
                "task_id": task_id,
                "task_status": task_status,
            },
        )
        body = parse_json(resp)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Kirim pesan gagal: {resp.status_code} {body}")
        return body["data"]

    def fetch_messages(self, since_id: int, limit: int = 50, wait: int = 0) -> dict:
        """Ambil pesan baru. `wait>0` mengaktifkan long polling: request
        ditahan server sampai ada pesan atau timeout `wait` detik."""
        timeout = (5, max(30, wait + 30))
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/fetch-messages",
            max_retries=self.retry_max,
            base=self.retry_base,
            params={"since_id": since_id, "limit": limit, "wait": wait},
            timeout=timeout,
        )
        return parse_json(resp)["data"]

    def stream_events(self, since_id: int | None = None, read_timeout: int = 120):
        """Generator SSE: `yield (event, data)` dari `GET /events`.

        Dipakai kalau `SSE_ENABLED=true` di server. Pemanggil harus siap
        menangani koneksi yang putus dan menyambung ulang.
        """
        params = {"since_id": since_id} if since_id is not None else {}
        resp = self.session.get(
            f"{self.base}/events",
            params=params,
            stream=True,
            timeout=(5, read_timeout),
            headers={"Accept": "text/event-stream"},
        )
        if resp.status_code == 401:
            resp.close()
            raise AuthRejected(AUTH_REJECTED_MSG)
        if resp.status_code != 200:
            text = resp.text[:200]
            resp.close()
            raise RuntimeError(f"SSE gagal: {resp.status_code} {text}")

        event_name: str | None = None
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                if raw.startswith(":"):
                    continue  # komentar/heartbeat
                if raw == "":
                    event_name = None
                    continue
                if raw.startswith("event:"):
                    event_name = raw[6:].strip()
                elif raw.startswith("data:"):
                    try:
                        payload = json.loads(raw[5:].strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    yield (event_name or "message"), payload
        finally:
            resp.close()

    # ---- file ----

    def list_files(self, limit: int = 50, offset: int = 0) -> dict:
        """Daftar file yang boleh diakses agent ini (diunggah sendiri,
        diterima langsung, atau di grup tempatnya anggota)."""
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/files",
            max_retries=self.retry_max,
            base=self.retry_base,
            params={"limit": limit, "offset": offset},
        )
        return parse_json(resp)["data"]

    def upload_file(self, path: str, target_type: str, target_id: str,
                    target_name: str, client_msg_id: str,
                    caption: str | None = None) -> dict:
        """Upload dengan pembukaan ulang file di tiap percobaan.
        Target selalu pasangan (target_id, target_name)."""
        size = os.path.getsize(path)
        if size > self.config.max_upload_mb * 1024 * 1024:
            raise ValueError(
                f"Ukuran file {size} byte melebihi batas {self.config.max_upload_mb} MB"
            )
        last_error = ""
        for attempt in range(self.retry_max + 1):
            try:
                with open(path, "rb") as f:
                    resp = self.session.post(
                        f"{self.base}/upload-file",
                        files={"file": (os.path.basename(path), f)},
                        data={
                            "target_type": target_type,
                            "target_id": target_id,
                            "target_name": target_name,
                            "client_msg_id": client_msg_id,
                            "caption": caption or "",
                        },
                        timeout=(5, 300),
                    )
                if resp.status_code == 401:
                    raise AuthRejected(AUTH_REJECTED_MSG)
                if resp.status_code not in RETRYABLE_STATUS:
                    body = parse_json(resp)
                    if resp.status_code not in (200, 201):
                        raise RuntimeError(f"Upload gagal: {resp.status_code} {body}")
                    return body["data"]
                last_error = f"HTTP {resp.status_code}"
            except (requests.ConnectionError, requests.Timeout):
                last_error = "koneksi gagal"
            if attempt < self.retry_max:
                self._backoff(attempt)
        raise RelayUnavailable(f"upload {path} gagal setelah {self.retry_max + 1} percobaan ({last_error})")

    def download_file(self, file_id: str):
        """Stream unduhan. Mengembalikan objek response (chunk via iter_content)."""
        last_error = ""
        for attempt in range(self.retry_max + 1):
            try:
                resp = self.session.get(
                    f"{self.base}/download-file/{file_id}",
                    stream=True,
                    timeout=(5, 300),
                )
                if resp.status_code == 401:
                    raise AuthRejected(AUTH_REJECTED_MSG)
                if resp.status_code not in RETRYABLE_STATUS:
                    if resp.status_code != 200:
                        resp.close()
                        raise RuntimeError(
                            f"Unduhan gagal: {resp.status_code} {resp.text}"
                        )
                    return resp
                resp.close()
                last_error = f"HTTP {resp.status_code}"
            except (requests.ConnectionError, requests.Timeout):
                last_error = "koneksi gagal"
            if attempt < self.retry_max:
                self._backoff(attempt)
        raise RelayUnavailable(
            f"download-file/{file_id} gagal setelah {self.retry_max + 1} percobaan ({last_error})"
        )

    # ---- direktori ----

    def list_agents(self) -> list[dict]:
        """Semua agent terdaftar (nama + ID) — untuk pilih tujuan chat."""
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/agents",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return parse_json(resp)["data"]["agents"]

    def list_groups(self) -> list[dict]:
        """Semua grup + status keanggotaan agent ini."""
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/groups",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return parse_json(resp)["data"]["groups"]

    # ---- ack & receipt ----

    def ack(self, message_ids: list[int], status: str = "delivered") -> dict:
        """Konfirmasi ke server bahwa pesan sudah diterima/dibaca."""
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/ack",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={"message_ids": message_ids, "status": status},
        )
        return parse_json(resp)["data"]

    def receipts(self, message_ids: list[int]) -> list[dict]:
        """Status penerimaan pesan yang kita kirim."""
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/receipts",
            max_retries=self.retry_max,
            base=self.retry_base,
            params={"message_ids": ",".join(str(i) for i in message_ids)},
        )
        return parse_json(resp)["data"]["receipts"]

    def get_task(self, task_id: str) -> dict:
        """Riwayat satu tugas (task + result)."""
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/tasks/{quote(task_id)}",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return parse_json(resp)["data"]

    # ---- grup ----

    def create_group(self, name: str, description: str | None = None) -> dict:
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/groups",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={"name": name, "description": description},
        )
        body = parse_json(resp)
        if resp.status_code != 201:
            raise RuntimeError(f"Buat grup gagal: {resp.status_code} {body}")
        return body["data"]

    def join_group(self, name: str, join_code: str | None = None) -> dict:
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/groups/{quote(name)}/join",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={"join_code": join_code} if join_code else None,
        )
        body = parse_json(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"Join grup gagal: {resp.status_code} {body}")
        return body["data"]

    def leave_group(self, name: str) -> dict:
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/groups/{quote(name)}/leave",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return parse_json(resp)["data"]

    def list_members(self, name: str) -> dict:
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/groups/{quote(name)}/members",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return parse_json(resp)["data"]

    def kick_member(self, name: str, agent_name: str) -> dict:
        resp = request_with_retry(
            self.session,
            "DELETE",
            f"{self.base}/groups/{quote(name)}/members/{quote(agent_name)}",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        body = parse_json(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"Kick gagal: {resp.status_code} {body}")
        return body["data"]

    def set_role(self, name: str, agent_name: str, role: str) -> dict:
        resp = request_with_retry(
            self.session,
            "PUT",
            f"{self.base}/groups/{quote(name)}/members/{quote(agent_name)}/role",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={"role": role},
        )
        body = parse_json(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"Ubah role gagal: {resp.status_code} {body}")
        return body["data"]

    def set_join_code(self, name: str, join_code: str | None = None) -> dict:
        """Atur join_code (None = dibuatkan otomatis oleh server)."""
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/groups/{quote(name)}/join-code",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={"join_code": join_code},
        )
        body = parse_json(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"Set join_code gagal: {resp.status_code} {body}")
        return body["data"]

    def clear_join_code(self, name: str) -> dict:
        resp = request_with_retry(
            self.session,
            "DELETE",
            f"{self.base}/groups/{quote(name)}/join-code",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        body = parse_json(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"Hapus join_code gagal: {resp.status_code} {body}")
        return body["data"]

    # ---- token ----

    def create_token(self, scopes: list[str], label: str | None = None,
                     expires_in_days: int | None = None) -> dict:
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/tokens",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={"scopes": scopes, "label": label, "expires_in_days": expires_in_days},
        )
        body = parse_json(resp)
        if resp.status_code != 201:
            raise RuntimeError(f"Buat token gagal: {resp.status_code} {body}")
        return body["data"]

    def list_tokens(self) -> list[dict]:
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/tokens",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return parse_json(resp)["data"]["tokens"]

    def revoke_token(self, token_id: str) -> dict:
        resp = request_with_retry(
            self.session,
            "DELETE",
            f"{self.base}/tokens/{quote(token_id)}",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        body = parse_json(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"Cabut token gagal: {resp.status_code} {body}")
        return body["data"]

    def rotate_token(self) -> dict:
        """Putar kunci utama. Kunci lama tetap valid selama grace di server."""
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/rotate-token",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        body = parse_json(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"Rotasi token gagal: {resp.status_code} {body}")
        return body["data"]

    # ---- unggahan berpotong (chunked + resumable) ----

    def _sha256_of(self, path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while block := f.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    def upload_status(self, upload_id: str) -> dict:
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/upload/{quote(upload_id)}",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return parse_json(resp)["data"]

    def abort_upload(self, upload_id: str) -> dict:
        resp = request_with_retry(
            self.session,
            "DELETE",
            f"{self.base}/upload/{quote(upload_id)}",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return parse_json(resp)["data"]

    def chunked_upload(
        self,
        path: str,
        target_type: str,
        target_id: str,
        target_name: str,
        client_msg_id: str,
        caption: str | None = None,
        chunk_mb: int = 5,
        on_progress=None,
    ) -> dict:
        """Unggah file besar secara berpotong dan resumable.

        Kalau server sudah punya file dengan SHA-256 + ukuran yang sama
        (dan boleh dilihat agent ini), tidak ada byte yang dikirim —
        `/init` menandai `deduplicated` dan kita langsung `/complete`.
        """
        size = os.path.getsize(path)
        sha = self._sha256_of(path)
        name = os.path.basename(path)

        init_resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/upload/init",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={
                "original_name": name,
                "total_size": size,
                "sha256": sha,
                "chunk_size": chunk_mb * 1024 * 1024,
                "target_type": target_type,
                "target_id": target_id,
                "target_name": target_name,
                "client_msg_id": client_msg_id,
                "caption": caption,
            },
        )
        body = parse_json(init_resp)
        if init_resp.status_code != 201:
            raise RuntimeError(f"Init unggah gagal: {init_resp.status_code} {body}")
        session = body["data"]
        upload_id = session["upload_id"]

        if not session.get("deduplicated"):
            chunk_size = session["chunk_size"]
            total = session["total_chunks"]
            done = set(session.get("received") or [])

            for index in range(total):
                if index in done:
                    continue
                offset = index * chunk_size
                with open(path, "rb") as f:
                    f.seek(offset)
                    payload = f.read(chunk_size)
                ok = False
                for attempt in range(self.retry_max + 1):
                    try:
                        resp = self.session.put(
                            f"{self.base}/upload/{quote(upload_id)}/chunk",
                            params={"index": index},
                            data=payload,
                            headers={"Content-Type": "application/octet-stream"},
                            timeout=(5, 120),
                        )
                        if resp.status_code == 401:
                            raise AuthRejected(AUTH_REJECTED_MSG)
                        if resp.status_code == 200:
                            ok = True
                            break
                        if resp.status_code not in RETRYABLE_STATUS:
                            raise RuntimeError(
                                f"Chunk {index} ditolak: {resp.status_code} {parse_json(resp)}"
                            )
                    except (requests.ConnectionError, requests.Timeout):
                        pass
                    if attempt < self.retry_max:
                        self._backoff(attempt)
                if not ok:
                    # Koneksi bermasalah: tanyakan status, lanjut dari yang kurang.
                    state = self.upload_status(upload_id)
                    done = set(state["received"])
                    if index in done:
                        continue
                    raise RelayUnavailable(f"chunk {index} gagal setelah beberapa percobaan")
                done.add(index)
                if on_progress:
                    on_progress(len(done), total)

        done_resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/upload/{quote(upload_id)}/complete",
            max_retries=self.retry_max,
            base=self.retry_base,
            timeout=(5, 300),
        )
        done_body = parse_json(done_resp)
        if done_resp.status_code not in (200, 201):
            raise RuntimeError(f"Selesai unggah gagal: {done_resp.status_code} {done_body}")
        return done_body["data"]
