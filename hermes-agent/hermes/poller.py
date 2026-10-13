"""Loop polling adaptif: flush outbox, ambil pesan, unduh file.

Interval:
- ada pesan baru  -> kembali ke POLL_MIN
- tidak ada pesan -> naik x1.5 sampai POLL_MAX
- server mati     -> naik x2 (backoff), coba lagi
"""
import threading
import uuid

from .api_client import AuthRejected, RelayUnavailable
from .handlers import on_message


class Poller:
    def __init__(self, api, memory, file_handler,
                 poll_min: float = 3.0, poll_max: float = 30.0,
                 on_message_fn=on_message):
        self.api = api
        self.memory = memory
        self.file_handler = file_handler
        self.poll_min = poll_min
        self.poll_max = poll_max
        self.on_message = on_message_fn
        self.stop_event = threading.Event()
        self.auth_rejected = False

    # ---- outbox ----

    def flush_outbox(self) -> None:
        """Kirim ulang pesan pending. Gagal sementara -> retry_count naik;
        lewat batas -> failed permanen."""
        for row in self.memory.pending_outbox():
            peer_id = row["peer_id"]
            if peer_id is None:
                # Baris lama (sebelum addressing ID+nama): resolve ulang lewat direktori.
                try:
                    if row["scope"] == "group":
                        match = next(
                            (g for g in self.api.list_groups()
                             if g["name"].lower() == row["peer"].lower()),
                            None,
                        )
                    else:
                        match = next(
                            (a for a in self.api.list_agents()
                             if a["name"].lower() == row["peer"].lower()),
                            None,
                        )
                except Exception:
                    self.memory.mark_failed(row["id"])
                    continue
                if match is None:
                    self.memory.mark_failed(row["id"])
                    continue
                peer_id = match.get("group_id") or match.get("agent_id")

            try:
                data = self.api.send_message(
                    target_type="group" if row["scope"] == "group" else "direct",
                    target_id=peer_id,
                    target_name=row["peer"],
                    content=row["content"] or "",
                    client_msg_id=row["client_msg_id"],
                )
                self.memory.mark_sent(row["id"], data["message_id"])
            except AuthRejected:
                raise
            except Exception:
                self.memory.mark_failed(row["id"])

    # ---- pesan masuk ----

    def _handle_incoming(self, messages: list[dict]) -> None:
        for msg in messages:
            if msg["msg_type"] == "file" and msg.get("file"):
                try:
                    dest = self.file_handler.download(self.api, msg, self.memory)
                    print(f"File tersimpan: {dest}", flush=True)
                except Exception as exc:
                    print(f"Gagal unduh file {msg['file']['file_id']}: {exc}", flush=True)
            self.on_message(msg)

    # ---- loop utama ----

    def poll_once(self) -> tuple[bool, bool]:
        """Satu siklus kerja: flush outbox lalu fetch.

        Mengembalikan (server_merespons, masih_ada_pesan).
        """
        try:
            self.flush_outbox()
            data = self.api.fetch_messages(
                since_id=self.memory.get_last_id(), limit=50
            )
        except AuthRejected as exc:
            print(str(exc), flush=True)
            self.auth_rejected = True
            return False, False
        except RelayUnavailable:
            return False, False

        messages = data["messages"]
        if messages:
            self.memory.save_incoming(messages, data["next_since_id"])
            self._handle_incoming(messages)
        return True, data["has_more"]

    def run(self) -> None:
        interval = self.poll_min
        while not self.stop_event.is_set() and not self.auth_rejected:
            reachable, has_more = self.poll_once()
            if reachable:
                interval = self.poll_min if has_more else min(self.poll_max, interval * 1.5)
            else:
                interval = min(self.poll_max, interval * 2)
            if not has_more:
                self.stop_event.wait(interval)

    def new_client_msg_id(self) -> str:
        return str(uuid.uuid4())
