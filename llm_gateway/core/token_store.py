"""
In-memory token accounting with periodic flush to disk.

Aggregates usage by (caller, model, date). Thread-safe via asyncio.Lock.
Periodically appends snapshots to a JSON Lines file for persistence.
Retains the last 7 days in memory for fast /stats queries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("llm-gw.token_store")

class _CallerBucket:
    """Mutable counters for a single (caller, model, date) triple."""

    __slots__ = (
        "caller", "model", "date",
        "calls", "prompt_tokens", "completion_tokens",
        "cache_hit_tokens", "cache_miss_tokens",
    )

    def __init__(self, caller: str, model: str, date: str) -> None:
        self.caller = caller
        self.model = model
        self.date = date
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0

    def to_dict(self) -> dict:
        return {
            "caller": self.caller,
            "model": self.model,
            "date": self.date,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }

class TokenStore:
    """Central token accounting store."""

    def __init__(
        self,
        stats_file: str = "logs/token_stats.jsonl",
        flush_interval_s: int = 300,
        retention_days: int = 7,
        pricing: dict | None = None,
    ) -> None:
        self._stats_file = stats_file
        self._flush_interval_s = flush_interval_s
        self._retention_days = retention_days
        self._pricing = pricing or {}

        self._buckets: dict[str, _CallerBucket] = {}
        self._lock = asyncio.Lock()

        self._total_calls = 0
        self._hourly_calls: list[float] = []

        self._flush_task: asyncio.Task | None = None

    def start_flush_loop(self) -> None:
        """Start the background flush task. Call once at app startup."""
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def shutdown(self) -> None:
        """Flush remaining data and cancel the background task."""
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        await self._flush_to_disk()

    async def record(
        self,
        caller: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        """Record a single LLM call's token usage. Thread-safe."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{caller}::{model}::{today}"
        now = time.monotonic()

        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _CallerBucket(caller, model, today)
                self._buckets[key] = bucket

            bucket.calls += 1
            bucket.prompt_tokens += prompt_tokens
            bucket.completion_tokens += completion_tokens
            bucket.cache_hit_tokens += cache_hit_tokens
            bucket.cache_miss_tokens += cache_miss_tokens

            self._total_calls += 1
            self._hourly_calls.append(now)

    @property
    def total_calls(self) -> int:
        return self._total_calls

    @property
    def calls_last_1h(self) -> int:
        cutoff = time.monotonic() - 3600
        self._hourly_calls = [t for t in self._hourly_calls if t > cutoff]
        return len(self._hourly_calls)

    async def get_stats(
        self,
        since: datetime | None = None,
        group_by: str = "caller",
    ) -> dict:
        """Aggregate stats for the /stats endpoint."""
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)

        since_date = since.strftime("%Y-%m-%d")
        now_str = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"

        async with self._lock:
            filtered = [
                b for b in self._buckets.values()
                if b.date >= since_date
            ]

        total_calls = sum(b.calls for b in filtered)
        total_prompt = sum(b.prompt_tokens for b in filtered)
        total_completion = sum(b.completion_tokens for b in filtered)
        total_cache_hit = sum(b.cache_hit_tokens for b in filtered)
        total_tokens = total_prompt + total_completion

        estimated_cost = self._estimate_cost(total_prompt, total_completion)

        grouped: dict[str, dict] = defaultdict(lambda: {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cache_hit_tokens": 0,
        })
        for bucket in filtered:
            group_key = bucket.caller if group_by == "caller" else bucket.model
            entry = grouped[group_key]
            entry["calls"] += bucket.calls
            entry["prompt_tokens"] += bucket.prompt_tokens
            entry["completion_tokens"] += bucket.completion_tokens
            entry["total_tokens"] += bucket.prompt_tokens + bucket.completion_tokens
            entry["cache_hit_tokens"] += bucket.cache_hit_tokens

        by_group = [
            {"caller" if group_by == "caller" else "model": key, **vals}
            for key, vals in sorted(grouped.items(), key=lambda x: -x[1]["calls"])
        ]

        return {
            "period": f"{since.isoformat(timespec='seconds')}Z / {now_str}",
            "total": {
                "calls": total_calls,
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_tokens,
                "cache_hit_tokens": total_cache_hit,
                "estimated_cost_usd": round(estimated_cost, 4),
            },
            f"by_{group_by}": by_group,
        }

    async def get_recent_entries(self) -> list[dict]:
        """Return all current bucket data (for /logs fallback)."""
        async with self._lock:
            return [b.to_dict() for b in self._buckets.values()]

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        input_rate = self._pricing.get("deepseek_chat_input_per_1m", 0.27)
        output_rate = self._pricing.get("deepseek_chat_output_per_1m", 1.10)
        return (prompt_tokens / 1_000_000 * input_rate
                + completion_tokens / 1_000_000 * output_rate)

    async def _flush_loop(self) -> None:
        """Periodically flush stats to disk and prune old data."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval_s)
                await self._flush_to_disk()
                await self._prune_old_data()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("[LLM-GW] token_store flush error", exc_info=True)

    async def _flush_to_disk(self) -> None:
        """Append current snapshot to the stats JSONL file."""
        async with self._lock:
            if not self._buckets:
                return
            snapshot = [b.to_dict() for b in self._buckets.values()]

        try:
            os.makedirs(os.path.dirname(self._stats_file) or ".", exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
            record = {"flushed_at": ts, "buckets": snapshot}
            with open(self._stats_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.debug("[LLM-GW] token_store flushed %d buckets", len(snapshot))
        except Exception:
            logger.warning("[LLM-GW] token_store flush write error", exc_info=True)

    async def _prune_old_data(self) -> None:
        """Remove buckets older than retention_days."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        ).strftime("%Y-%m-%d")

        async with self._lock:
            stale_keys = [k for k, b in self._buckets.items() if b.date < cutoff]
            for key in stale_keys:
                del self._buckets[key]
            if stale_keys:
                logger.debug("[LLM-GW] token_store pruned %d stale buckets", len(stale_keys))
