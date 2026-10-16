"""Request models for the dashboard endpoints."""
from typing import Literal

from pydantic import BaseModel, Field

from ..schemas import GROUP_NAME_PATTERN, PUBLIC_ID_PATTERN


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


class SendMessageRequest(BaseModel):
    """Send a message from the dashboard.

    `as_agent` determines the sender identity. The dashboard is an admin
    surface, so the session holder may send as any agent (equivalent to
    access to the agent machine). The inter-agent API still uses per-agent
    tokens. The target is always an (ID, name) pair, as in the agent API.
    """

    as_agent: str = Field(..., min_length=3, max_length=32)
    target_type: Literal["direct", "group"]
    target_id: str = Field(..., pattern=PUBLIC_ID_PATTERN)
    target_name: str = Field(..., min_length=3, max_length=32)
    content: str = Field(..., min_length=1, max_length=8000)


class CreateGroupRequest(BaseModel):
    as_agent: str = Field(..., min_length=3, max_length=32)
    name: str = Field(..., pattern=GROUP_NAME_PATTERN, max_length=32)
    description: str | None = Field(default=None, max_length=500)


class JoinGroupRequest(BaseModel):
    as_agent: str = Field(..., min_length=3, max_length=32)
