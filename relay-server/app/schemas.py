"""Pydantic models for request bodies and responses."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .security import KNOWN_SCOPES

AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,31}$"
GROUP_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,31}$"
UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# Public IDs: ag_<10 hex> for agents, gr_<10 hex> for groups.
PUBLIC_ID_PATTERN = r"^(ag|gr)_[0-9a-f]{10}$"


class RegisterRequest(BaseModel):
    name: str = Field(..., pattern=AGENT_NAME_PATTERN, max_length=32)
    description: str | None = Field(default=None, max_length=500)
    metadata: dict | None = None
    # Registration idempotency nonce: if the 201 response is lost (timeout),
    # retrying with the same nonce returns the same secret instead of a
    # 409 "name already taken" that burns the agent name.
    registration_nonce: str | None = Field(default=None, min_length=8, max_length=64)


MSG_TYPES = ("text", "file", "task", "result", "event")
TASK_STATUSES = ("pending", "running", "done", "failed", "cancelled")


class SendMessageRequest(BaseModel):
    client_msg_id: str = Field(..., pattern=UUID4_PATTERN)
    target_type: Literal["direct", "group"]
    # Address is always an (ID, name) pair — the server rejects a mismatch.
    target_id: str = Field(..., pattern=PUBLIC_ID_PATTERN)
    target_name: str = Field(..., min_length=3, max_length=32)
    content: str = Field(..., min_length=1, max_length=8000)
    msg_type: Literal["text", "file", "task", "result", "event"] = "text"
    # Required for msg_type task/result: the task ID linking the
    # request and the reply (lightweight RPC pattern).
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
    """Confirmation of message receipt by the receiving agent."""

    message_ids: list[int] = Field(..., min_length=1, max_length=200)
    status: Literal["delivered", "read"] = "delivered"


class CreateGroupRequest(BaseModel):
    name: str = Field(..., pattern=GROUP_NAME_PATTERN, max_length=32)
    description: str | None = Field(default=None, max_length=500)


# --- token security ---------------------------------------------------------


class CreateTokenRequest(BaseModel):
    """Secondary token with limited scope."""

    scopes: list[str] = Field(..., min_length=1, max_length=16)
    label: str | None = Field(default=None, max_length=64)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def _check_scopes(self):
        unknown = [s for s in self.scopes if s not in KNOWN_SCOPES]
        if unknown:
            raise ValueError(f"scope tidak dikenal: {', '.join(unknown)}")
        # Dedupe while preserving order.
        self.scopes = list(dict.fromkeys(self.scopes))
        return self


# --- advanced group management ---------------------------------------------------


class JoinGroupRequest(BaseModel):
    """Join using a join_code (optional; if empty, the group must be open)."""

    join_code: str | None = Field(default=None, min_length=4, max_length=64)


class SetRoleRequest(BaseModel):
    role: Literal["admin", "member"]


class SetJoinCodeRequest(BaseModel):
    """Set/change the group's join_code.

    `join_code=None` -> the server generates a random code. To clear it
    (the group becomes open again), use `DELETE /groups/{name}/join-code`.
    """

    join_code: str | None = Field(default=None, min_length=4, max_length=64)


# --- chunked uploads ------------------------------------------


class ChunkedUploadInitRequest(BaseModel):
    """Open a chunked upload session. The client has already computed the total SHA-256."""

    original_name: str = Field(..., min_length=1, max_length=255)
    total_size: int = Field(..., ge=1)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    chunk_size: int = Field(..., ge=1)
    target_type: Literal["direct", "group"]
    target_id: str = Field(..., pattern=PUBLIC_ID_PATTERN)
    target_name: str = Field(..., min_length=3, max_length=32)
    client_msg_id: str = Field(..., pattern=UUID4_PATTERN)
    caption: str | None = Field(default=None, max_length=500)


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
