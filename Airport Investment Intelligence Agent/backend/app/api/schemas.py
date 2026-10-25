"""Validated HTTP request and response contracts (mirrored in frontend/src/types.ts)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.analysis.models import AnalysisResult, SourceRef


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- errors ------------------------------------------------------------------------------------


class ErrorBody(Schema):
    code: str
    message: str
    request_id: str | None = None
    conversation_id: str | None = None


class ErrorEnvelope(Schema):
    error: ErrorBody


# -- auth --------------------------------------------------------------------------------------


class LoginRequest(Schema):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=1024)


class RegisterRequest(Schema):
    username: str = Field(min_length=3, max_length=150, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=10, max_length=1024)


class UserOut(Schema):
    user_id: str
    username: str


class AuthResponse(Schema):
    user: UserOut
    csrf_token: str


# -- chat --------------------------------------------------------------------------------------


class ChatRequest(Schema):
    request_id: UUID
    message: str = Field(max_length=20_000)
    conversation_id: str | None = Field(default=None, max_length=64)

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be empty")
        return value


class ChatResponse(Schema):
    """Final answer envelope (spec section 6): always all five fields; empty arrays as []."""

    conversation_id: str
    answer: str
    results: list[AnalysisResult] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


ChatStatus = Literal["processing", "completed", "failed", "interrupted"]


class ChatAccepted(Schema):
    request_id: str
    conversation_id: str
    status: ChatStatus
    status_url: str


class ChatRequestStatus(Schema):
    request_id: str
    conversation_id: str
    status: ChatStatus
    created_at: datetime
    updated_at: datetime
    # The final envelope exactly as it was saved (spec section 8: stored results are not recomputed
    # or rewritten), so older answers keep loading after the result models evolve.
    response: dict[str, Any] | None = None
    error: ErrorBody | None = None


# -- conversations -----------------------------------------------------------------------------


class ConversationSummary(Schema):
    conversation_id: str
    title: str
    updated_at: datetime


class ConversationList(Schema):
    conversations: list[ConversationSummary]


class MessageRequestInfo(Schema):
    request_id: str
    status: ChatStatus
    sent_conversation_id: str | None


class MessageOut(Schema):
    seq: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    request: MessageRequestInfo | None = None  # user messages
    response: dict[str, Any] | None = None  # assistant messages: the saved ChatResponse envelope


class ConversationDetail(Schema):
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]


# -- data refresh ------------------------------------------------------------------------------

RefreshStatus = Literal["queued", "running", "completed", "failed", "interrupted"]


class SourceRefreshResult(Schema):
    source_id: str
    status: Literal["pending", "unchanged", "updated", "failed", "skipped"]
    periods: list[str] = Field(default_factory=list)
    checked_at: datetime | None = None
    retrieved_at: datetime | None = None
    message: str | None = None


class RefreshJob(Schema):
    job_id: str
    status: RefreshStatus
    stage: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    sources: list[SourceRefreshResult]
    changed: bool | None
    active_version_id: str | None
    error: ErrorBody | None
    status_url: str
