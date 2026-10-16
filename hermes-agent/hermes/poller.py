"""Message-fetching loop: adaptive polling, long polling, or SSE.

Modes (pick one):
- **polling** (default) — adaptive interval: message present -> POLL_MIN, empty ->
  increase x1.5 up to POLL_MAX, server down -> backoff x2.
- **long polling** (`wait_seconds>0`) — the server holds the request until a
  message arrives or a timeout, so latency is nearly instant without increasing
  the number of requests.
- **SSE** (`use_sse=True`) — one open connection; the server pushes messages.
  Most efficient; reconnects automatically with backoff.

Resilience (applies to all modes):
- The `on_message` handler runs per message inside try/except; one message
  that makes the handler error must not kill the loop.
- A transient failure while flushing the outbox stops the flush cycle (not
  endless retries) and the message stays `pending` for the next cycle.
- File downloads that fail due to the network are marked `pending` and retried
  automatically every cycle.
- After a message is processed, the agent sends an ack to the server (/ack endpoint).
"""
import threading
import uuid

from .api_client import AuthRejected, RelayUnavailable
from .file_handler import RETRYABLE
from .handlers import on_message


class Poller:
    def __init__(self, api, memory, file_handler,
                 poll_min: float = 3.0, poll_max: float = 30.0,
                 wait_seconds: int = 0, use_sse: bool = False,
                 on_message_fn=on_message):
        self.api = api
        self.memory = memory
        self.file_handler = file_handler
        self.poll_min = poll_min
        self.poll_max = poll_max
        self.wait_seconds = wait_seconds
        self.use_sse = use_sse
        self.on_message = on_message_fn
        self.stop_event = threading.Event()
        self.auth_rejected = False

    # ---- outbox ----

    def flush_outbox(self) -> None:
        """Resend pending messages.

        Server down -> stop this cycle (break), do NOT retry each message
        for tens of seconds and then mark them failed. Permanent errors
        (e.g. target missing) are still marked failed so they do not repeat
        forever.
        """
        for row in self.memory.pending_outbox():
            peer_id = row["peer_id"]
            if peer_id is None:
                # Old row (before ID+name addressing): resolve again.
                peer_id = self._resolve_peer(row["scope"], row["peer"])
                if peer_id is None:
                    self.memory.mark_failed(row["id"])
                    continue

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
            except RelayUnavailable:
                # Server unreachable: leave the rest for the next
                # cycle, do not burn retry_count.
                break
            except RETRYABLE:
                break
            except Exception:
                self.memory.mark_failed(row["id"])

    def _resolve_peer(self, scope: str, name: str) -> str | None:
        try:
            if scope == "group":
                match = next(
                    (g for g in self.api.list_groups()
                     if g["name"].lower() == name.lower()),
                    None,
                )
            else:
                match = next(
                    (a for a in self.api.list_agents()
                     if a["name"].lower() == name.lower()),
                    None,
                )
        except Exception:
            return None
        if match is None:
            return None
        return match.get("group_id") or match.get("agent_id")

    # ---- incoming messages ----

    def _handle_incoming(self, messages: list[dict]) -> list[int]:
        """Process new messages; one handler that errors does not stop the
        others. Return the list of processed message ids (for ack)."""
        processed: list[int] = []
        for msg in messages:
            try:
                if msg["msg_type"] == "file" and msg.get("file"):
                    try:
                        dest = self.file_handler.download(self.api, msg, self.memory)
                        print(f"File tersimpan: {dest}", flush=True)
                    except RETRYABLE:
                        print(
                            f"Unduhan file {msg['file']['file_id']} tertunda, "
                            "dicoba lagi nanti.",
                            flush=True,
                        )
                self.on_message(msg)
                processed.append(msg["id"])
            except Exception as exc:
                # The loop must stay alive; log and continue to the next message.
                print(f"Handler error untuk pesan #{msg.get('id')}: {exc!r}", flush=True)
        return processed

    def _ack(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        try:
            self.api.ack(message_ids)
        except Exception:
            pass  # ack is a status optimization; a failure is not fatal

    # ---- main loop ----

    def poll_once(self) -> tuple[bool, bool]:
        """One work cycle: flush outbox, retry downloads, fetch, process.

        Returns (server_responds, more_messages_pending).
        """
        try:
            self.flush_outbox()
        except AuthRejected as exc:
            print(str(exc), flush=True)
            self.auth_rejected = True
            return False, False
        except Exception as exc:
            # Do not die because a single flush cycle errored.
            print(f"flush_outbox error: {exc!r}", flush=True)

        try:
            self.file_handler.retry_pending(self.api, self.memory)
        except Exception:
            pass

        try:
            data = self.api.fetch_messages(
                since_id=self.memory.get_last_id(), limit=50, wait=self.wait_seconds
            )
        except AuthRejected as exc:
            print(str(exc), flush=True)
            self.auth_rejected = True
            return False, False
        except RelayUnavailable:
            return False, False
        except Exception as exc:
            # Includes JSONDecodeError from a non-JSON response (e.g. a proxy
            # error page). The loop stays alive.
            print(f"fetch_messages error: {exc!r}", flush=True)
            return False, False

        # The cursor is no longer valid if the server DB was reset.
        epoch = data.get("server_epoch")
        if epoch and not self.memory.server_epoch_matches(epoch):
            print(
                "PERINGATAN: server_epoch berubah - database server tampaknya "
                "di-reset. Cursor pesan lama mungkin tidak valid lagi.",
                flush=True,
            )
        if epoch:
            self.memory.server_epoch = epoch
            self.memory.remember_server_epoch(epoch)

        messages = data["messages"]
        if messages:
            try:
                new_messages = self.memory.save_incoming(messages, data["next_since_id"])
            except Exception as exc:
                print(f"save_incoming error: {exc!r}", flush=True)
                return True, False
            processed = self._handle_incoming(new_messages)
            self._ack(processed)
        return True, data["has_more"]

    def run(self) -> None:
        if self.use_sse:
            self.run_sse()
            return
        interval = self.poll_min
        while not self.stop_event.is_set() and not self.auth_rejected:
            reachable, has_more = self.poll_once()
            if not reachable:
                interval = min(self.poll_max, interval * 2)
                self.stop_event.wait(interval)
                continue
            if self.wait_seconds > 0:
                # The server already held the request until a message/timeout; no
                # long pause is needed. A small pause only when there is leftover.
                if not has_more:
                    self.stop_event.wait(0.2)
                continue
            interval = self.poll_min if has_more else min(self.poll_max, interval * 1.5)
            if not has_more:
                self.stop_event.wait(interval)

    # ---- SSE mode ----

    def _housekeeping(self) -> bool:
        """Flush outbox + retry pending downloads. False = token rejected."""
        try:
            self.flush_outbox()
        except AuthRejected as exc:
            print(str(exc), flush=True)
            self.auth_rejected = True
            return False
        except Exception as exc:
            print(f"flush_outbox error: {exc!r}", flush=True)
        try:
            self.file_handler.retry_pending(self.api, self.memory)
        except Exception:
            pass
        return True

    def _consume_sse(self, event: str, payload: dict) -> None:
        if event == "epoch":
            epoch = payload.get("server_epoch")
            if epoch and not self.memory.server_epoch_matches(epoch):
                print(
                    "PERINGATAN: server_epoch berubah - database server "
                    "tampaknya di-reset.",
                    flush=True,
                )
            if epoch:
                self.memory.server_epoch = epoch
                self.memory.remember_server_epoch(epoch)
            return
        if event != "messages":
            return
        messages = payload.get("messages") or []
        if not messages:
            return
        try:
            new_messages = self.memory.save_incoming(messages, payload["next_since_id"])
        except Exception as exc:
            print(f"save_incoming error: {exc!r}", flush=True)
            return
        self._ack(self._handle_incoming(new_messages))

    def run_sse(self) -> None:
        """SSE mode: a single connection, reconnect automatically with backoff."""
        backoff = self.poll_min
        while not self.stop_event.is_set() and not self.auth_rejected:
            if not self._housekeeping():
                return
            try:
                for event, payload in self.api.stream_events(
                    since_id=self.memory.get_last_id()
                ):
                    if self.stop_event.is_set():
                        break
                    backoff = self.poll_min
                    self._consume_sse(event, payload)
            except AuthRejected as exc:
                print(str(exc), flush=True)
                self.auth_rejected = True
                return
            except Exception as exc:
                print(f"SSE terputus ({exc!r}); menyambung ulang...", flush=True)
            if self.stop_event.is_set():
                break
            self.stop_event.wait(backoff)
            backoff = min(self.poll_max, backoff * 2)

    def new_client_msg_id(self) -> str:
        return str(uuid.uuid4())
