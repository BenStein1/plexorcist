from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

import pydantic
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field


if int(pydantic.VERSION.split(".", 1)[0]) >= 2:
    class BaseModel(PydanticBaseModel):
        pass
else:
    class BaseModel(PydanticBaseModel):
        @classmethod
        def model_validate(cls, value: Any) -> Any:
            return cls.parse_obj(value)

        def model_copy(self, *, deep: bool = False) -> Any:
            return self.copy(deep=deep)

        def model_dump(self, *, mode: str = "python", **kwargs: Any) -> dict[str, Any]:
            if mode == "json":
                return __import__("json").loads(self.json(**kwargs))
            return self.dict(**kwargs)


class Intent(str, Enum):
    UNKNOWN = "unknown"
    REQUEST_MOVIE = "request_movie"
    REQUEST_SHOW = "request_show"
    REQUEST_EPISODE = "request_episode"
    SUPPORT_MISSING = "support_missing"
    SUPPORT_MISSING_MOVIE = "support_missing_movie"
    RECOMMEND = "recommend"


class MediaType(str, Enum):
    MOVIE = "movie"
    SHOW = "show"
    EPISODE = "episode"


class UserContext(BaseModel):
    user_id: str
    username: str
    display_name: str
    is_admin: bool = False
    auth_source: str = "dev-header"


class PlexAuthSession(BaseModel):
    session_id: str
    user_id: str
    username: str
    display_name: str
    is_admin: bool = False
    plex_token: str | None = None
    auth_source: str = "plex-oauth"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ChatMessage(BaseModel):
    role: str
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ConversationState(BaseModel):
    conversation_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    intent: Intent = Intent.UNKNOWN
    pending_confirmation: str | None = None
    candidate_media: list[dict[str, Any]] = Field(default_factory=list)
    last_issue_key: str | None = None
    support_context: dict[str, Any] = Field(default_factory=dict)
    last_tool_actions: list[dict[str, Any]] = Field(default_factory=list)
    escalation_history: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class DevImpersonationRequest(BaseModel):
    user_id: str
    username: str
    display_name: str
    is_admin: bool = False


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    private_note: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    continue_to_ombi_url: str
    state: ConversationState
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
