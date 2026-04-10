
"""Statistics, status and log models for llm-gateway."""

from __future__ import annotations

from pydantic import BaseModel, Field

class UsageInfo(BaseModel):
    """Token usage breakdown, including cache metrics."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

class BackendStatus(BaseModel):
    """Per-backend health info for GET /status."""
    status: str = "ok"
    model: str = ""
    circuit_state: str = "closed"           # closed | open | half_open
    consecutive_failures: int = 0
    max_concurrent: int = 0
    active_calls: int = 0

class ServiceStatus(BaseModel):
    """GET /status response."""
    status: str = "ok"
    uptime_s: int = 0
    backends: dict[str, BackendStatus] = Field(default_factory=dict)
    routing_rules: int = 0
    calls_total: int = 0
    calls_last_1h: int = 0

class CallerStats(BaseModel):
    """Per-caller aggregated stats."""
    caller: str = ""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    estimated_cost_usd: float = 0.0

class StatsResponse(BaseModel):
    """GET /stats response."""
    period: str = ""
    total: dict = Field(default_factory=dict)
    by_caller: list[CallerStats] = Field(default_factory=list)

class LogEntry(BaseModel):
    """Single call log entry for GET /logs."""
    ts: str = ""
    caller: str = ""
    session_id: str | None = None
    role_card: str | None = None
    model: str = ""
    backend: str = ""
    stream: bool = False
    response_format_type: str = "text"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    latency_ms: int = 0
    queue_wait_ms: int = 0
    tool_call_count: int = 0
    status: str = "ok"

class LogsResponse(BaseModel):
    """GET /logs response."""
    logs: list[LogEntry] = Field(default_factory=list)
