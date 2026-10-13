"""Klien HTTP ke relay server: satu Session, retry dengan backoff + jitter.

Retry hanya untuk kegagalan sementara (ConnectionError, Timeout, 429, 5xx).
Kesalahan 4xx lain bersifat permanen dan langsung dikembalikan ke pemanggil.
send-message dan upload-file aman di-retry karena client_msg_id idempotent.
"""
import os
import random
import time

import requests

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
AUTH_REJECTED_MSG = "Token ditolak server. Hentikan polling dan periksa config."


class RelayUnavailable(Exception):
    """Server tidak bisa dijangkau setelah semua percobaan."""


class AuthRejected(Exception):
    """Server menolak token (401). Polling harus berhenti, bukan retry."""


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

    def register(self) -> dict:
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/register",
            max_retries=self.retry_max,
            base=self.retry_base,
            headers={"X-Registration-Key": self.config.registration_key},
            json={
                "name": self.config.agent_name,
                "description": "Hermes agent",
                "metadata": {"client_version": "1.0.0"},
            },
        )
        if resp.status_code != 201:
            raise RuntimeError(f"Registrasi gagal: {resp.status_code} {resp.text}")
        return resp.json()["data"]

    # ---- pesan ----

    def send_message(self, target_type: str, target: str, content: str,
                     client_msg_id: str, reply_to: int | None = None) -> dict:
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/send-message",
            max_retries=self.retry_max,
            base=self.retry_base,
            json={
                "client_msg_id": client_msg_id,
                "target_type": target_type,
                "target": target,
                "content": content,
                "reply_to": reply_to,
            },
        )
        body = resp.json()
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Kirim pesan gagal: {resp.status_code} {body}")
        return body["data"]

    def fetch_messages(self, since_id: int, limit: int = 50) -> dict:
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/fetch-messages",
            max_retries=self.retry_max,
            base=self.retry_base,
            params={"since_id": since_id, "limit": limit},
        )
        return resp.json()["data"]

    # ---- file ----

    def upload_file(self, path: str, target_type: str, target: str,
                    client_msg_id: str, caption: str | None = None) -> dict:
        """Upload dengan pembukaan ulang file di tiap percobaan."""
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
                            "target": target,
                            "client_msg_id": client_msg_id,
                            "caption": caption or "",
                        },
                        timeout=(5, 300),
                    )
                if resp.status_code == 401:
                    raise AuthRejected(AUTH_REJECTED_MSG)
                if resp.status_code not in RETRYABLE_STATUS:
                    body = resp.json()
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
        return resp.json()["data"]["agents"]

    def list_groups(self) -> list[dict]:
        """Semua grup + status keanggotaan agent ini."""
        resp = request_with_retry(
            self.session,
            "GET",
            f"{self.base}/groups",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        return resp.json()["data"]["groups"]

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
        body = resp.json()
        if resp.status_code != 201:
            raise RuntimeError(f"Buat grup gagal: {resp.status_code} {body}")
        return body["data"]

    def join_group(self, name: str) -> dict:
        resp = request_with_retry(
            self.session,
            "POST",
            f"{self.base}/groups/{name}/join",
            max_retries=self.retry_max,
            base=self.retry_base,
        )
        body = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"Join grup gagal: {resp.status_code} {body}")
        return body["data"]
