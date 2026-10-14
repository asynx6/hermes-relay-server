"""Model Pydantic untuk request body dan response."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,31}$"
GROUP_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,31}$"
UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# ID publik: ag_<10 hex> untuk agent, gr_<10 hex> untuk grup.
PUBLIC_ID_PATTERN = r"^(ag|gr)_[0-9a-f]{10}$"


class RegisterRequest(BaseModel):
    name: str = Field(..., pattern=AGENT_NAME_PATTERN, max_length=32)
    description: str | None = Field(default=None, max_length=500)
    metadata: dict | None = None
    # Nonce idempotency registrasi: kalau respons 201 hilang (timeout),
    # retry dengan nonce yang sama mengembalikan secret yang sama alih-alih
    # 409 "nama sudah dipakai" yang membakar nama agent.
    registration_nonce: str | None = Field(default=None, min_length=8, max_length=64)


MSG_TYPES = ("text", "file", "task", "result", "event")
TASK_STATUSES = ("pending", "running", "done", "failed", "cancelled")


class SendMessageRequest(BaseModel):
    client_msg_id: str = Field(..., pattern=UUID4_PATTERN)
    target_type: Literal["direct", "group"]
    # Alamat selalu pasangan (ID, nama) — server menolak kalau tidak cocok.
    target_id: str = Field(..., pattern=PUBLIC_ID_PATTERN)
    target_name: str = Field(..., min_length=3, max_length=32)
    content: str = Field(..., min_length=1, max_length=8000)
    msg_type: Literal["text", "file", "task", "result", "event"] = "text"
    # Wajib untuk msg_type task/result: ID tugas yang menghubungkan
    # permintaan dan balasan (pola RPC ringan).
    task_id: str | None = Field(default=None, min_length=8, max_length=64)
    task_status: Literal["pending", "running", "done", "failed", "cancelled"] | None = None
    reply_to: int | None = None

    @model_validator(mode="after")
    def _check_task_fields(self):
        if self.msg_type in ("task", "result") and not self.task_id:
            raise ValueError(f"task_id wajib untuk msg_type '{self.msg_type}'")
        if self.msg_type not in ("task", "result") and self.task_id:
            raise ValueError("task_id hanya boleh untuk msg_type 'task' atau 'result'")
        return self


class AckRequest(BaseModel):
    """Konfirmasi penerimaan pesan oleh agent penerima."""

    message_ids: list[int] = Field(..., min_length=1, max_length=200)
    status: Literal["delivered", "read"] = "delivered"


class CreateGroupRequest(BaseModel):
    name: str = Field(..., pattern=GROUP_NAME_PATTERN, max_length=32)
    description: str | None = Field(default=None, max_length=500)


class MessageOut(BaseModel):
    id: int
    scope: Literal["direct", "group"]
    sender: str
    group: str | None = None
    msg_type: Literal["text", "file"]
    content: str | None = None
    file: dict | None = None
    reply_to: int | None = None
    created_at: str


class FetchMessagesResponse(BaseModel):
    messages: list[MessageOut]
    next_since_id: int
    has_more: bool
