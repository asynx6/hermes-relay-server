"""Model Pydantic untuk request body dan response."""
from typing import Literal

from pydantic import BaseModel, Field

AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,31}$"
GROUP_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{2,31}$"
UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class RegisterRequest(BaseModel):
    name: str = Field(..., pattern=AGENT_NAME_PATTERN, max_length=32)
    description: str | None = Field(default=None, max_length=500)
    metadata: dict | None = None


class SendMessageRequest(BaseModel):
    client_msg_id: str = Field(..., pattern=UUID4_PATTERN)
    target_type: Literal["direct", "group"]
    target: str = Field(..., min_length=3, max_length=32)
    content: str = Field(..., min_length=1, max_length=8000)
    reply_to: int | None = None


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
