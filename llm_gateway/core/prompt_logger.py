"""
Prompt logger — append-only JSON Lines, one file per day.

Writes each LLM call as a single JSON line to `logs/llm_calls/YYYY-MM-DD.jsonl`.
Write failures are silently logged as warnings and never propagate to the caller.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
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
