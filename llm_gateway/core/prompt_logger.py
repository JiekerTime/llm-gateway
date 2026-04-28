"""
Prompt logger — append-only JSON Lines, one file per day.

Writes each LLM call as a single JSON line to `logs/llm_calls/YYYY-MM-DD.jsonl`.
Write failures are silently logged as warnings and never propagate to the caller.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("llm-gw.prompt_logger")

class PromptLogger:
    """Append-only prompt/response logger with daily file rotation."""

    def __init__(
        self,
        log_dir: str = "logs/llm_calls",
        enabled: bool = True,
        include_content: bool = True,
        max_content_chars: int = 4000,
    ) -> None:
        self._log_dir = log_dir
        self._enabled = enabled
        self._include_content = include_content
        self._max_content_chars = max_content_chars

        self._recent: list[dict] = []
        self._max_recent = 500

    def log_call(
        self,
        *,
        caller: str,
        session_id: str | None = None,
        role_card: str | None = None,
        backend: str,
        model: str,
        stream: bool = False,
        response_format_type: str = "text",
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int = 0,
        latency_ms: int,
        queue_wait_ms: int = 0,
        tool_call_count: int = 0,
        status: str = "ok",
        messages: list[dict] | None = None,
        response_content: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        entry = self._build_entry(
            ts=now,
            caller=caller,
            session_id=session_id,
            role_card=role_card,
            backend=backend,
            model=model,
            stream=stream,
            response_format_type=response_format_type,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_hit_tokens=cache_hit_tokens,
            latency_ms=latency_ms,
            queue_wait_ms=queue_wait_ms,
            tool_call_count=tool_call_count,
            status=status,
            messages=messages,
            response_content=response_content,
        )

        self._recent.append(entry)
        if len(self._recent) > self._max_recent:
            self._recent = self._recent[-self._max_recent:]

        if not self._enabled:
            return

        try:
            self._write_to_disk(entry, now)
        except Exception:
            logger.warning(
                "[LLM-GW] prompt_logger write failed caller=%s", caller, exc_info=True,
            )

    def get_recent(
        self,
        limit: int = 20,
        caller: str | None = None,
        since: datetime | None = None,
    ) -> list[dict]:
        """Return recent log entries, optionally filtered."""
        entries = self._recent

        if caller:
            entries = [e for e in entries if e.get("caller", "").startswith(caller)]

        if since:
            since_str = since.isoformat(timespec="seconds")
            entries = [e for e in entries if e.get("ts", "") >= since_str]

        return list(reversed(entries[-limit:]))

    def get_usage_analysis(
        self,
        *,
        since: datetime | None = None,
        caller: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Analyze persisted per-call logs by source and caller."""
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
        since = _ensure_aware(since)

        entries = self._load_entries(since=since, caller=caller)
        now_str = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"

        return {
            "period": f"{since.isoformat(timespec='seconds')}Z / {now_str}",
            "total": _summarize_entries(entries),
            "by_service": _aggregate_entries(entries, "service", limit=limit, include_top_callers=True),
            "by_source": _aggregate_entries(entries, "source", limit=limit, include_top_callers=True),
            "by_caller": _aggregate_entries(entries, "caller", limit=limit),
            "by_model": _aggregate_entries(entries, "model", limit=limit),
            "by_backend": _aggregate_entries(entries, "backend", limit=limit),
            "by_role_card": _aggregate_entries(entries, "role_card", limit=limit),
            "by_session": _aggregate_entries(entries, "session_id", limit=limit),
            "recent_heavy_calls": _recent_heavy_calls(entries, limit=limit),
        }

    def _build_entry(
        self,
        *,
        ts: datetime,
        caller: str,
        session_id: str | None,
        role_card: str | None,
        backend: str,
        model: str,
        stream: bool,
        response_format_type: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int,
        latency_ms: int,
        queue_wait_ms: int,
        tool_call_count: int,
        status: str,
        messages: list[dict] | None,
        response_content: str | None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": ts.isoformat(timespec="seconds") + "Z",
            "caller": caller,
            "session_id": session_id,
            "role_card": role_card,
            "backend": backend,
            "model": model,
            "stream": stream,
            "response_format_type": response_format_type,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_hit_tokens": cache_hit_tokens,
            "latency_ms": latency_ms,
            "queue_wait_ms": queue_wait_ms,
            "tool_call_count": tool_call_count,
            "status": status,
        }

        if self._include_content:
            if messages:
                preview = json.dumps(messages, ensure_ascii=False)
                entry["messages_preview"] = preview[:self._max_content_chars]
            if response_content:
                entry["response_preview"] = response_content[:self._max_content_chars]

        return entry

    def _write_to_disk(self, entry: dict, ts: datetime) -> None:
        """Append one JSON line to the daily log file."""
        os.makedirs(self._log_dir, exist_ok=True)
        filename = ts.strftime("%Y-%m-%d") + ".jsonl"
        filepath = os.path.join(self._log_dir, filename)

        with open(filepath, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load_entries(
        self,
        *,
        since: datetime,
        caller: str | None,
    ) -> list[dict[str, Any]]:
        entries = self._load_entries_from_disk(since)

        if not entries and not self._enabled:
            entries = list(self._recent)

        filtered: list[dict[str, Any]] = []
        for entry in entries:
            ts = _parse_log_ts(entry.get("ts", ""))
            if ts is None or ts < since:
                continue
            entry_caller = str(entry.get("caller") or "unknown")
            if caller and not entry_caller.startswith(caller):
                continue
            filtered.append(entry)

        return sorted(filtered, key=lambda item: item.get("ts", ""))

    def _load_entries_from_disk(self, since: datetime) -> list[dict[str, Any]]:
        if not os.path.isdir(self._log_dir):
            return []

        entries: list[dict[str, Any]] = []
        since_day = since.date().isoformat()
        for filename in sorted(os.listdir(self._log_dir)):
            if not filename.endswith(".jsonl"):
                continue
            day = filename[:-6]
            if day < since_day:
                continue
            filepath = os.path.join(self._log_dir, filename)
            try:
                with open(filepath, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(entry, dict):
                            entries.append(entry)
            except OSError:
                logger.warning("[LLM-GW] prompt_logger usage read failed file=%s", filepath, exc_info=True)

        return entries


def _summarize_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_tokens = sum(_int(entry.get("prompt_tokens")) for entry in entries)
    completion_tokens = sum(_int(entry.get("completion_tokens")) for entry in entries)
    cache_hit_tokens = sum(_int(entry.get("cache_hit_tokens")) for entry in entries)
    return {
        "calls": len(entries),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cache_hit_tokens": cache_hit_tokens,
    }


def _aggregate_entries(
    entries: list[dict[str, Any]],
    field: str,
    *,
    limit: int,
    include_top_callers: bool = False,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(_new_usage_group)

    for entry in entries:
        key = _group_key(entry, field)
        group = grouped[key]
        group[field if field != "session_id" else "session"] = key
        _add_entry_to_group(group, entry)
        if include_top_callers:
            caller = str(entry.get("caller") or "unknown")
            group["callers"][caller] += _entry_total_tokens(entry)

    result = []
    for group in grouped.values():
        calls = group["calls"]
        latency_total = group.pop("_latency_total_ms")
        group["avg_latency_ms"] = round(latency_total / calls, 1) if calls else 0
        callers = group.pop("callers")
        if include_top_callers:
            group["top_callers"] = [
                {"caller": caller, "total_tokens": total_tokens}
                for caller, total_tokens in sorted(callers.items(), key=lambda item: -item[1])[:5]
            ]
        result.append(group)

    return sorted(result, key=lambda item: (-item["total_tokens"], -item["calls"]))[:limit]


def _recent_heavy_calls(entries: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    calls = []
    for entry in entries:
        calls.append(
            {
                "ts": entry.get("ts", ""),
                "caller": entry.get("caller") or "unknown",
                "service": _caller_service(entry.get("caller")),
                "source": _caller_source(entry.get("caller")),
                "backend": entry.get("backend") or "unknown",
                "model": entry.get("model") or "unknown",
                "session_id": entry.get("session_id"),
                "role_card": entry.get("role_card"),
                "stream": bool(entry.get("stream")),
                "response_format_type": entry.get("response_format_type") or "text",
                "prompt_tokens": _int(entry.get("prompt_tokens")),
                "completion_tokens": _int(entry.get("completion_tokens")),
                "total_tokens": _entry_total_tokens(entry),
                "latency_ms": _int(entry.get("latency_ms")),
            }
        )
    return sorted(calls, key=lambda item: (-item["total_tokens"], item["ts"]))[:limit]


def _new_usage_group() -> dict[str, Any]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "_latency_total_ms": 0,
        "first_seen": "",
        "last_seen": "",
        "callers": defaultdict(int),
    }


def _add_entry_to_group(group: dict[str, Any], entry: dict[str, Any]) -> None:
    prompt_tokens = _int(entry.get("prompt_tokens"))
    completion_tokens = _int(entry.get("completion_tokens"))
    total_tokens = prompt_tokens + completion_tokens
    ts = str(entry.get("ts") or "")

    group["calls"] += 1
    group["prompt_tokens"] += prompt_tokens
    group["completion_tokens"] += completion_tokens
    group["total_tokens"] += total_tokens
    group["cache_hit_tokens"] += _int(entry.get("cache_hit_tokens"))
    group["_latency_total_ms"] += _int(entry.get("latency_ms"))
    if ts and (not group["first_seen"] or ts < group["first_seen"]):
        group["first_seen"] = ts
    if ts and (not group["last_seen"] or ts > group["last_seen"]):
        group["last_seen"] = ts


def _group_key(entry: dict[str, Any], field: str) -> str:
    if field == "service":
        return _caller_service(entry.get("caller"))
    if field == "source":
        return _caller_source(entry.get("caller"))
    value = entry.get(field)
    if value is None or value == "":
        return "none" if field in {"role_card", "session_id"} else "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _caller_source(caller: Any) -> str:
    caller_str = str(caller or "").strip()
    if not caller_str:
        return "unknown"
    return caller_str.split("/", 1)[0] or "unknown"


def _caller_service(caller: Any) -> str:
    caller_str = str(caller or "").strip()
    source = _caller_source(caller_str)

    if caller_str == "hs-media-autoheal":
        return "homeserver-cli/media-autoheal"

    group_portrait_sources = {
        "batch",
        "clash",
        "dispatcher",
        "editor_evolution",
        "eval",
        "executor",
        "feed",
        "gap",
        "gp",
        "hunt",
        "interactive",
        "ingest",
        "knowledge_access",
        "meeting_service",
        "meeting",
        "merger",
        "query",
        "reflection",
        "research_protocol",
        "routing",
        "runtime",
        "search_decompose",
        "server",
        "steering",
        "writing",
    }
    if source in group_portrait_sources:
        return "group-portrait"

    if source in {"media", "subtitle"}:
        return "homeserver-media"
    if source == "codex":
        return "operator/codex"

    return source or "unknown"


def _entry_total_tokens(entry: dict[str, Any]) -> int:
    return _int(entry.get("prompt_tokens")) + _int(entry.get("completion_tokens"))


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_log_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value[:-1] if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
