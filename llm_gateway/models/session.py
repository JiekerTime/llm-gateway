"""Session models for session-backed chat state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Session(BaseModel):
    """Persisted session state."""

    session_id: str
    role_card: str = ""
    caller: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    ttl_hours: int = 24
    dimension_overrides: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    total_tokens_used: int = 0


class SessionCreateRequest(BaseModel):
    """POST /sessions request body."""

    session_id: str | None = None
    role_card: str
    caller: str = ""
    ttl_hours: int | None = None
    dimension_overrides: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
