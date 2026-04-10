"""
llm-gateway — FastAPI application entry point.

Responsibilities:
- Load config.yaml, initialise all backends / router / circuit breakers /
  token store / prompt logger
- Global + per-backend concurrency control
- API-key authentication (all endpoints except /health)
- Fallback chain: primary backend → next in chain → … → HTTP 502
- Retry with exponential backoff
- Endpoints: /chat, /health, /status, /stats, /logs
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from llm_gateway.backends import BaseBackend, create_backend
from llm_gateway.config import load_config
from llm_gateway.core.circuit_breaker import CircuitBreaker
from llm_gateway.core.incremental_json import IncrementalJSONParser
from llm_gateway.core.prompt_logger import PromptLogger
from llm_gateway.core.role_card_registry import RoleCardRegistry
from llm_gateway.core.router import Router
from llm_gateway.core.session_manager import SessionManager
from llm_gateway.core.token_store import TokenStore
from llm_gateway.models import (
    BackendStatus,
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    LogEntry,
    LogsResponse,
    ServiceStatus,
    SessionCreateRequest,
    StreamEvent,
    ToolCall,
    UsageInfo,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("llm-gw.server")

# ---------------------------------------------------------------------------
# Global state (initialised in lifespan)
# ---------------------------------------------------------------------------

_config: dict = {}
_backends: dict[str, BaseBackend] = {}
_breakers: dict[str, CircuitBreaker] = {}
_router: Router = Router()
_token_store: TokenStore = TokenStore()
_prompt_logger: PromptLogger = PromptLogger()
_role_card_registry: RoleCardRegistry = RoleCardRegistry()
_session_manager: SessionManager | None = None
_global_semaphore: asyncio.Semaphore = asyncio.Semaphore(20)
_start_time: float = 0.0


@dataclass
class _PreparedChat:
    caller: str
    messages: list[dict]
    model_override: str | None
    temperature: float
    session_id: str | None = None
    role_card_name: str | None = None

# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    global _config, _backends, _breakers, _router
    global _token_store, _prompt_logger, _global_semaphore, _start_time
    global _role_card_registry, _session_manager

    _start_time = time.monotonic()
    _config = load_config()

    # --- backends ---
    for backend_name, backend_cfg in _config.get("backends", {}).items():
        _backends[backend_name] = create_backend(backend_name, backend_cfg)
        logger.info("[LLM-GW] backend=%s type=%s loaded", backend_name, backend_cfg.get("type"))

    # --- circuit breakers (per-backend) ---
    cb_cfg = _config.get("circuit_breaker", {})
    for backend_name in _backends:
        _breakers[backend_name] = CircuitBreaker(
            backend_name=backend_name,
            failure_threshold=cb_cfg.get("failure_threshold", 5),
            recovery_timeout_s=cb_cfg.get("recovery_timeout_s", 60),
            half_open_limit=cb_cfg.get("half_open_limit", 1),
        )

    # --- router ---
    _router = Router.from_config(_config)

    # --- role cards ---
    rc_cfg = _config.get("role_cards", {})
    _role_card_registry = RoleCardRegistry()
    _role_card_registry.load_from_config(rc_cfg.get("cards", {}))
    _role_card_registry.load_from_directory(rc_cfg.get("card_dir", ""))

    # --- token store ---
    ta_cfg = _config.get("token_accounting", {})
    pricing_cfg = _config.get("pricing", {})
    _token_store = TokenStore(
        stats_file=ta_cfg.get("stats_file", "logs/token_stats.jsonl"),
        flush_interval_s=ta_cfg.get("flush_interval_s", 300),
        pricing=pricing_cfg,
    )
    _token_store.start_flush_loop()

    # --- prompt logger ---
    pl_cfg = _config.get("prompt_logging", {})
    _prompt_logger = PromptLogger(
        log_dir=pl_cfg.get("log_dir", "logs/llm_calls"),
        enabled=pl_cfg.get("enabled", True),
        include_content=pl_cfg.get("include_content", True),
        max_content_chars=pl_cfg.get("max_content_chars", 4000),
    )

    # --- global semaphore ---
    max_concurrent = _config.get("concurrency", {}).get("max_concurrent", 20)
    _global_semaphore = asyncio.Semaphore(max_concurrent)

    # --- session manager ---
    sessions_cfg = _config.get("sessions", {})
    if sessions_cfg.get("enabled", False):
        _session_manager = SessionManager(
            db_path=sessions_cfg.get("db_path", "data_runtime/sessions.db"),
            default_ttl_hours=sessions_cfg.get("default_ttl_hours", 24),
            cleanup_interval_s=sessions_cfg.get("cleanup_interval_s", 3600),
        )
        _session_manager.start_cleanup_loop()
    else:
        _session_manager = None

    logger.info(
        "[LLM-GW] started  port=%s  backends=%s  rules=%d  max_concurrent=%d",
        _config.get("port", 8525),
        list(_backends.keys()),
        _router.rule_count,
        max_concurrent,
    )

    yield  # --- app is running ---

    if _session_manager is not None:
        await _session_manager.shutdown()
    await _token_store.shutdown()
    logger.info("[LLM-GW] shutdown complete")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="llm-gateway",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _check_api_key(x_api_key: str = Header(default="")) -> str:
    expected = _config.get("api_key", "lg_llm_gateway_2026")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return x_api_key

# ---------------------------------------------------------------------------
# POST /chat
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(request: ChatRequest, x_api_key: str = Header(default="")):
    _check_api_key(x_api_key)
    if request.stream:
        return await _chat_stream(request)
    return await _chat_sync(request)


async def _chat_sync(request: ChatRequest):
    prepared = await _prepare_chat_request(request)
    caller = prepared.caller
    policy = _router.resolve(caller, prepared.model_override)
    response_format_type = request.response_format.type if request.response_format else "text"

    # Build the ordered backend list: primary → fallback chain
    fallback_chain: list[str] = _config.get("fallback_chain", list(_backends.keys()))
    backend_order = _build_backend_order(policy.backend, fallback_chain, policy.fallback_allowed)

    errors: list[str] = []
    format_supported = False
    retry_cfg = _config.get("retry", {})
    max_attempts = retry_cfg.get("max_attempts", 3)
    backoff_s = retry_cfg.get("backoff_seconds", 2.0)
    backoff_mult = retry_cfg.get("backoff_multiplier", 1.5)

    for backend_name in backend_order:
        backend = _backends.get(backend_name)
        breaker = _breakers.get(backend_name)
        if backend is None or breaker is None:
            continue

        response_format_error = backend.validate_response_format(request.response_format)
        if response_format_error:
            errors.append(f"{backend_name}: {response_format_error}")
            continue
        format_supported = True

        # circuit breaker check
        if not await breaker.allow_request():
            errors.append(f"{backend_name}: circuit open")
            continue

        # retry loop for this backend
        for attempt in range(1, max_attempts + 1):
            queue_start = time.monotonic()
            call_start = queue_start
            try:
                async with _global_semaphore:
                    queue_wait_ms = int((time.monotonic() - queue_start) * 1000)
                    call_start = time.monotonic()

                    result = await backend.call(
                        messages=prepared.messages,
                        model=policy.model,
                        temperature=prepared.temperature,
                        max_tokens=request.max_tokens,
                        tools=request.tools,
                        tool_choice=request.tool_choice,
                        response_format=request.response_format,
                    )

                    latency_ms = int((time.monotonic() - call_start) * 1000)

                await breaker.record_success()
                await _append_session_history_if_needed(
                    request=request,
                    prepared=prepared,
                    assistant_content=result.content,
                    assistant_tool_calls=result.tool_calls,
                    usage_tokens=result.usage.total_tokens,
                )

                # token accounting
                await _token_store.record(
                    caller=caller,
                    model=policy.model,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    cache_hit_tokens=result.usage.cache_hit_tokens,
                    cache_miss_tokens=result.usage.cache_miss_tokens,
                )

                # prompt logging (never raises)
                tool_call_count = len(result.tool_calls) if result.tool_calls else 0
                _prompt_logger.log_call(
                    caller=caller,
                    session_id=prepared.session_id,
                    role_card=prepared.role_card_name,
                    backend=backend_name,
                    model=policy.model,
                    stream=False,
                    response_format_type=response_format_type,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    cache_hit_tokens=result.usage.cache_hit_tokens,
                    latency_ms=latency_ms,
                    queue_wait_ms=queue_wait_ms,
                    tool_call_count=tool_call_count,
                    status="ok",
                    messages=prepared.messages,
                    response_content=result.content,
                )

                logger.info(
                    "[LLM-GW] caller=%s backend=%s model=%s latency=%dms "
                    "tokens=%d queue=%dms tools=%d",
                    caller, backend_name, policy.model, latency_ms,
                    result.usage.total_tokens, queue_wait_ms, tool_call_count,
                )

                return ChatResponse(
                    content=result.content,
                    tool_calls=result.tool_calls,
                    stop_reason=result.stop_reason,
                    model=policy.model,
                    backend=backend_name,
                    usage=result.usage,
                    latency_ms=latency_ms,
                    queue_wait_ms=queue_wait_ms,
                    response_format_type=response_format_type,
                    session_id=prepared.session_id,
                    role_card=prepared.role_card_name,
                    caller=caller,
                )

            except Exception as exc:
                await breaker.record_failure()
                error_msg = f"{backend_name}: {type(exc).__name__}: {exc}"
                errors.append(error_msg)
                logger.warning(
                    "[LLM-GW] caller=%s backend=%s attempt=%d/%d error=%s",
                    caller, backend_name, attempt, max_attempts, error_msg,
                )

                if attempt < max_attempts:
                    sleep_time = backoff_s * (backoff_mult ** (attempt - 1))
                    await asyncio.sleep(sleep_time)
                    continue
                break  # exhausted retries for this backend

    if not format_supported:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="unsupported_response_format",
                detail="; ".join(errors),
                caller=caller,
            ).model_dump(),
        )

    # all backends exhausted
    logger.error("[LLM-GW] caller=%s all backends exhausted errors=%s", caller, errors)
    return JSONResponse(
        status_code=502,
        content=ErrorResponse(
            error="all backends exhausted",
            detail="; ".join(errors),
            caller=caller,
        ).model_dump(),
    )


async def _chat_stream(request: ChatRequest):
    prepared = await _prepare_chat_request(request)
    caller = prepared.caller
    policy = _router.resolve(caller, prepared.model_override)
    response_format_type = request.response_format.type if request.response_format else "text"
    fallback_chain: list[str] = _config.get("fallback_chain", list(_backends.keys()))
    backend_order = _build_backend_order(policy.backend, fallback_chain, policy.fallback_allowed)

    unsupported_errors: list[str] = []
    format_supported = False
    for backend_name in backend_order:
        backend = _backends.get(backend_name)
        if backend is None:
            continue
        response_format_error = backend.validate_response_format(request.response_format)
        if response_format_error:
            unsupported_errors.append(f"{backend_name}: {response_format_error}")
            continue
        format_supported = True
        break

    if not format_supported:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="unsupported_response_format",
                detail="; ".join(unsupported_errors),
                caller=caller,
            ).model_dump(),
        )

    async def event_source() -> AsyncIterator[str]:
        errors: list[str] = []
        retry_cfg = _config.get("retry", {})
        max_attempts = retry_cfg.get("max_attempts", 3)
        backoff_s = retry_cfg.get("backoff_seconds", 2.0)
        backoff_mult = retry_cfg.get("backoff_multiplier", 1.5)

        for backend_name in backend_order:
            backend = _backends.get(backend_name)
            breaker = _breakers.get(backend_name)
            if backend is None or breaker is None:
                continue

            response_format_error = backend.validate_response_format(request.response_format)
            if response_format_error:
                errors.append(f"{backend_name}: {response_format_error}")
                continue

            if not await breaker.allow_request():
                errors.append(f"{backend_name}: circuit open")
                continue

            for attempt in range(1, max_attempts + 1):
                queue_start = time.monotonic()
                content_parts: list[str] = []
                tool_states: dict[int, dict[str, str]] = {}
                tool_parsers: dict[int, IncrementalJSONParser] = {}
                text_parser = IncrementalJSONParser()
                usage = UsageInfo()
                stop_reason = "stop"
                stream_started = False

                try:
                    async with _global_semaphore:
                        queue_wait_ms = int((time.monotonic() - queue_start) * 1000)
                        call_start = time.monotonic()

                        async for event in backend.stream_call(
                            messages=prepared.messages,
                            model=policy.model,
                            temperature=prepared.temperature,
                            max_tokens=request.max_tokens,
                            tools=request.tools,
                            tool_choice=request.tool_choice,
                            response_format=request.response_format,
                        ):
                            if event.type == "message_start":
                                stream_started = True
                                yield _encode_sse(event)
                                continue

                            if event.type == "text_delta" and event.delta:
                                content_parts.append(event.delta)
                                text_parser.append(event.delta)
                                stream_started = True
                                yield _encode_sse(event)
                                if response_format_type in {"json_object", "json_schema"}:
                                    snapshot = text_parser.snapshot()
                                    if snapshot is not None:
                                        yield _encode_sse(
                                            StreamEvent(
                                                type="structured_partial",
                                                structured_target="message",
                                                structured_value=snapshot,
                                            )
                                        )
                                continue

                            if event.type == "tool_call_start" and event.tool_index is not None:
                                stream_started = True
                                tool_states[event.tool_index] = {
                                    "id": event.tool_call_id or f"call_{event.tool_index}",
                                    "name": event.tool_name or "",
                                    "arguments": "",
                                }
                                tool_parsers[event.tool_index] = IncrementalJSONParser()
                                yield _encode_sse(event)
                                continue

                            if event.type == "tool_args_delta" and event.tool_index is not None:
                                state = tool_states.setdefault(
                                    event.tool_index,
                                    {
                                        "id": event.tool_call_id or f"call_{event.tool_index}",
                                        "name": event.tool_name or "",
                                        "arguments": "",
                                    },
                                )
                                state["arguments"] += event.arguments_delta or ""
                                parser = tool_parsers.setdefault(
                                    event.tool_index,
                                    IncrementalJSONParser(),
                                )
                                parser.append(event.arguments_delta)
                                stream_started = True
                                yield _encode_sse(event)
                                snapshot = parser.snapshot()
                                if snapshot is not None:
                                    yield _encode_sse(
                                        StreamEvent(
                                            type="structured_partial",
                                            structured_target="tool_arguments",
                                            structured_value=snapshot,
                                            tool_call_id=state["id"],
                                            tool_name=state["name"],
                                            tool_index=event.tool_index,
                                        )
                                    )
                                continue

                            if event.type == "usage" and event.usage is not None:
                                usage = event.usage
                                yield _encode_sse(event)
                                continue

                            if event.type == "done":
                                stop_reason = event.stop_reason or "stop"

                        latency_ms = int((time.monotonic() - call_start) * 1000)

                    await breaker.record_success()

                    tool_calls = _build_tool_calls(tool_states, tool_parsers)
                    await _append_session_history_if_needed(
                        request=request,
                        prepared=prepared,
                        assistant_content="".join(content_parts),
                        assistant_tool_calls=tool_calls or None,
                        usage_tokens=usage.total_tokens,
                    )
                    await _token_store.record(
                        caller=caller,
                        model=policy.model,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        cache_hit_tokens=usage.cache_hit_tokens,
                        cache_miss_tokens=usage.cache_miss_tokens,
                    )

                    _prompt_logger.log_call(
                        caller=caller,
                        session_id=prepared.session_id,
                        role_card=prepared.role_card_name,
                        backend=backend_name,
                        model=policy.model,
                        stream=True,
                        response_format_type=response_format_type,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        cache_hit_tokens=usage.cache_hit_tokens,
                        latency_ms=latency_ms,
                        queue_wait_ms=queue_wait_ms,
                        tool_call_count=len(tool_calls),
                        status="ok",
                        messages=prepared.messages,
                        response_content="".join(content_parts),
                    )

                    if response_format_type in {"json_object", "json_schema"}:
                        final_snapshot = text_parser.final()
                        if final_snapshot is not None:
                            yield _encode_sse(
                                StreamEvent(
                                    type="structured_partial",
                                    structured_target="message",
                                    structured_value=final_snapshot,
                                )
                            )

                    yield _encode_sse(
                        StreamEvent(
                            type="done",
                            stop_reason=stop_reason,
                            usage=usage,
                            backend=backend_name,
                            model=policy.model,
                            latency_ms=latency_ms,
                            queue_wait_ms=queue_wait_ms,
                            message=None,
                            tool_calls=tool_calls or None,
                        )
                    )
                    return
                except Exception as exc:
                    await breaker.record_failure()
                    error_msg = f"{backend_name}: {type(exc).__name__}: {exc}"
                    errors.append(error_msg)
                    logger.warning(
                        "[LLM-GW] caller=%s backend=%s attempt=%d/%d stream_error=%s",
                        caller, backend_name, attempt, max_attempts, error_msg,
                    )

                    if stream_started:
                        yield _encode_sse(
                            StreamEvent(
                                type="error",
                                message=error_msg,
                                backend=backend_name,
                                model=policy.model,
                            )
                        )
                        return

                    if attempt < max_attempts:
                        sleep_time = backoff_s * (backoff_mult ** (attempt - 1))
                        await asyncio.sleep(sleep_time)
                        continue
                    break

        yield _encode_sse(
            StreamEvent(
                type="error",
                message="; ".join(errors) or "all_backends_exhausted",
                backend=policy.backend,
                model=policy.model,
            )
        )

    return StreamingResponse(event_source(), media_type="text/event-stream")

# ---------------------------------------------------------------------------
# Session / role card endpoints
# ---------------------------------------------------------------------------

@app.post("/sessions")
async def create_session(
    body: SessionCreateRequest,
    x_api_key: str = Header(default=""),
):
    _check_api_key(x_api_key)
    manager = _require_session_manager()

    if _role_card_registry.get(body.role_card) is None:
        raise HTTPException(status_code=404, detail=f"Unknown role_card '{body.role_card}'")

    session_id = body.session_id or _generate_session_id()
    try:
        session = await manager.get_or_create(
            session_id=session_id,
            role_card=body.role_card,
            caller=body.caller or "unknown",
            dimension_overrides=body.dimension_overrides,
            ttl_hours=body.ttl_hours,
            metadata=body.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session


@app.get("/sessions")
async def list_sessions(
    x_api_key: str = Header(default=""),
    caller: str = Query(default=""),
):
    _check_api_key(x_api_key)
    manager = _require_session_manager()
    sessions = await manager.list_sessions(caller=caller)
    return {"sessions": sessions}


@app.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    x_api_key: str = Header(default=""),
):
    _check_api_key(x_api_key)
    manager = _require_session_manager()
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    x_api_key: str = Header(default=""),
):
    _check_api_key(x_api_key)
    manager = _require_session_manager()
    deleted = await manager.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": True, "session_id": session_id}


@app.get("/role-cards")
async def list_role_cards(
    x_api_key: str = Header(default=""),
):
    _check_api_key(x_api_key)
    cards = _role_card_registry.list_all()
    return {"role_cards": cards}


@app.get("/role-cards/{name}")
async def get_role_card(
    name: str,
    x_api_key: str = Header(default=""),
):
    _check_api_key(x_api_key)
    card = _role_card_registry.get(name)
    if card is None:
        raise HTTPException(status_code=404, detail="Role card not found")
    return {
        "role_card": card,
        "system_prompt_preview": card.build_system_prompt(),
    }

# ---------------------------------------------------------------------------
# GET /health (no auth)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------

@app.get("/status")
async def status(x_api_key: str = Header(default="")):
    _check_api_key(x_api_key)

    uptime_s = int(time.monotonic() - _start_time)
    backend_statuses: dict[str, BackendStatus] = {}

    for name, backend in _backends.items():
        breaker = _breakers.get(name)
        backend_statuses[name] = BackendStatus(
            status="ok",
            model=backend.default_model,
            circuit_state=breaker.state.value if breaker else "unknown",
            consecutive_failures=breaker.consecutive_failures if breaker else 0,
            max_concurrent=backend.max_concurrent,
            active_calls=backend.active_calls,
        )

    return ServiceStatus(
        status="ok",
        uptime_s=uptime_s,
        backends=backend_statuses,
        routing_rules=_router.rule_count,
        calls_total=_token_store.total_calls,
        calls_last_1h=_token_store.calls_last_1h,
    )

# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------

@app.get("/stats")
async def stats(
    x_api_key: str = Header(default=""),
    since: str = Query(default="", description="ISO 8601 timestamp or duration like '7d', '24h'"),
    group_by: str = Query(default="caller", description="'caller' or 'model'"),
):
    _check_api_key(x_api_key)

    since_dt = _parse_since(since)
    data = await _token_store.get_stats(since=since_dt, group_by=group_by)
    return data

# ---------------------------------------------------------------------------
# GET /logs
# ---------------------------------------------------------------------------

@app.get("/logs")
async def logs(
    x_api_key: str = Header(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    caller: str = Query(default=""),
    since: str = Query(default=""),
):
    _check_api_key(x_api_key)

    since_dt = _parse_since(since) if since else None
    entries = _prompt_logger.get_recent(
        limit=limit,
        caller=caller or None,
        since=since_dt,
    )
    return LogsResponse(
        logs=[LogEntry(**e) for e in entries],
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_backend_order(
    primary: str,
    fallback_chain: list[str],
    fallback_allowed: bool,
) -> list[str]:
    """
    Build the ordered list of backends to try.
    Primary first, then fallback chain (if allowed), deduped.
    """
    if not fallback_allowed:
        return [primary]

    order = [primary]
    for name in fallback_chain:
        if name not in order:
            order.append(name)
    return order


async def _prepare_chat_request(request: ChatRequest) -> _PreparedChat:
    caller = request.caller or "unknown"
    model_override = request.model
    temperature = request.temperature

    if request.session_id is None:
        if request.role_card:
            role_card = _role_card_registry.get(request.role_card)
            if role_card is None:
                raise HTTPException(status_code=404, detail=f"Unknown role_card '{request.role_card}'")

            request_system_messages = [
                dict(message)
                for message in request.messages
                if message.get("role") == "system"
            ]
            request_non_system_messages = [
                dict(message)
                for message in request.messages
                if message.get("role") != "system"
            ]

            messages: list[dict] = [
                {
                    "role": "system",
                    "content": role_card.build_system_prompt(request.dimension_overrides),
                }
            ]
            messages.extend(request_system_messages)
            messages.extend(request_non_system_messages)

            if model_override is None and role_card.model:
                model_override = role_card.model
            if "temperature" not in request.model_fields_set and role_card.temperature is not None:
                temperature = role_card.temperature

            return _PreparedChat(
                caller=caller,
                messages=messages,
                model_override=model_override,
                temperature=temperature,
                role_card_name=request.role_card,
            )

        return _PreparedChat(
            caller=caller,
            messages=request.messages,
            model_override=model_override,
            temperature=temperature,
        )

    manager = _require_session_manager()
    try:
        session = await manager.get_or_create(
            session_id=request.session_id,
            role_card=request.role_card or "",
            caller=caller,
            dimension_overrides=request.dimension_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    role_card_name = request.role_card or session.role_card
    role_card = _role_card_registry.get(role_card_name)
    if role_card is None:
        raise HTTPException(status_code=404, detail=f"Unknown role_card '{role_card_name}'")

    full_messages = await manager.build_full_messages(
        session=session,
        new_messages=request.messages,
        role_card=role_card,
        dimension_overrides=request.dimension_overrides,
    )

    if model_override is None and role_card.model:
        model_override = role_card.model
    if "temperature" not in request.model_fields_set and role_card.temperature is not None:
        temperature = role_card.temperature

    return _PreparedChat(
        caller=caller,
        messages=full_messages,
        model_override=model_override,
        temperature=temperature,
        session_id=session.session_id,
        role_card_name=role_card_name,
    )


async def _append_session_history_if_needed(
    request: ChatRequest,
    prepared: _PreparedChat,
    assistant_content: str | None,
    assistant_tool_calls: list[ToolCall] | None,
    usage_tokens: int,
) -> None:
    if prepared.session_id is None or not request.append_history or _session_manager is None:
        return

    history_messages = [
        dict(message)
        for message in request.messages
        if message.get("role") != "system"
    ]

    assistant_message: dict[str, object] = {
        "role": "assistant",
        "content": assistant_content,
    }
    if assistant_tool_calls:
        assistant_message["tool_calls"] = [tool_call.model_dump() for tool_call in assistant_tool_calls]
    history_messages.append(assistant_message)

    await _session_manager.append_messages(
        session_id=prepared.session_id,
        messages=history_messages,
        usage_tokens=usage_tokens,
    )


def _require_session_manager() -> SessionManager:
    if _session_manager is None:
        raise HTTPException(status_code=400, detail="Session support is disabled")
    return _session_manager


def _generate_session_id() -> str:
    return f"sess_{uuid4().hex}"


def _encode_sse(event: StreamEvent) -> str:
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n"


def _build_tool_calls(
    tool_states: dict[int, dict[str, str]],
    tool_parsers: dict[int, IncrementalJSONParser] | None = None,
) -> list[ToolCall]:
    result: list[ToolCall] = []
    for index in sorted(tool_states):
        state = tool_states[index]
        raw_arguments = state.get("arguments", "")
        arguments: dict = {}
        if raw_arguments:
            try:
                arguments = json.loads(raw_arguments)
            except JSONDecodeError:
                parser = tool_parsers.get(index) if tool_parsers else None
                parsed = parser.final() if parser else None
                arguments = parsed if isinstance(parsed, dict) else {"raw": raw_arguments}
        result.append(
            ToolCall(
                id=state.get("id", f"call_{index}"),
                name=state.get("name", ""),
                arguments=arguments,
            )
        )
    return result


def _parse_since(since_str: str) -> datetime:
    """
    Parse a 'since' parameter.
    Supports: ISO 8601 timestamps, or shorthand like '7d', '24h', '1h'.
    Defaults to 24h ago.
    """
    if not since_str:
        return datetime.now(timezone.utc) - timedelta(hours=24)

    since_str = since_str.strip()

    # shorthand durations
    if since_str.endswith("d"):
        try:
            days = int(since_str[:-1])
            return datetime.now(timezone.utc) - timedelta(days=days)
        except ValueError:
            pass
    if since_str.endswith("h"):
        try:
            hours = int(since_str[:-1])
            return datetime.now(timezone.utc) - timedelta(hours=hours)
        except ValueError:
            pass

    # ISO 8601
    try:
        return datetime.fromisoformat(since_str.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc) - timedelta(hours=24)
