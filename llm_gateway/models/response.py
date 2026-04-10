
"""Response models for llm-gateway."""

from __future__ import annotations

from typing import Literal
from typing import Any

from pydantic import BaseModel, Field

from llm_gateway.models.request import ToolCall
from llm_gateway.models.stats import UsageInfo

class ChatResponse(BaseModel):
    """POST /chat success response."""
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    stop_reason: str = "stop"               # "stop" | "tool_use" | "length"
    model: str = ""
    backend: str = ""
    usage: UsageInfo = Field(default_factory=UsageInfo)
    latency_ms: int = 0
    queue_wait_ms: int = 0
    response_format_type: str = "text"
    session_id: str | None = None
    role_card: str | None = None
    caller: str = ""

class ErrorResponse(BaseModel):
    """Error response (typically HTTP 502 / 504)."""
    error: str
    detail: str = ""
    caller: str = ""

class BackendResult(BaseModel):
    """Unified result returned by any backend's call() method."""
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    stop_reason: str = "stop"
    usage: UsageInfo = Field(default_factory=UsageInfo)


class StreamEvent(BaseModel):
    """Normalized event emitted by the gateway streaming API."""

    type: Literal[
        "message_start",
        "text_delta",
        "tool_call_start",
        "tool_args_delta",
        "structured_partial",
        "usage",
        "done",
        "error",
    ]
    delta: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_index: int | None = None
    arguments_delta: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: UsageInfo | None = None
    structured_target: str | None = None
    structured_value: Any | None = None
    stop_reason: str | None = None
    backend: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    queue_wait_ms: int | None = None
    message: str | None = None
