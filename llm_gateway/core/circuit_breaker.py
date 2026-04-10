"""
Per-backend circuit breaker.

State machine: CLOSED → OPEN → HALF_OPEN → CLOSED

- CLOSED:    all requests pass through
- OPEN:      consecutive failures ≥ threshold → reject immediately, trigger fallback
- HALF_OPEN: after recovery_timeout, allow up to `half_open_limit` probe requests;
             success → CLOSED, failure → reset OPEN timer
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum

logger = logging.getLogger("llm-gw.circuit_breaker")

class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    """Independent circuit breaker instance — one per backend."""

    def __init__(
        self,
        backend_name: str,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
        half_open_limit: int = 1,
    ) -> None:
        self.backend_name = backend_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.half_open_limit = half_open_limit

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0
        self._half_open_in_flight = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Return the *effective* state (may transition OPEN → HALF_OPEN lazily)."""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout_s:
                return CircuitState.HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    async def allow_request(self) -> bool:
        """Check whether a request should be allowed through."""
        async with self._lock:
            effective = self.state

            if effective == CircuitState.CLOSED:
                return True

            if effective == CircuitState.HALF_OPEN:
                if self._half_open_in_flight < self.half_open_limit:
                    self._half_open_in_flight += 1
                    self._state = CircuitState.HALF_OPEN
                    return True
                return False

            return False

    async def record_success(self) -> None:
        """Call after a successful backend response."""
        async with self._lock:
            previous = self._state
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._half_open_in_flight = 0
            if previous != CircuitState.CLOSED:
                logger.warning(
                    "[LLM-GW] circuit_breaker backend=%s state=%s→CLOSED",
                    self.backend_name,
                    previous.value,
                )

    async def record_failure(self) -> None:
        """Call after a failed backend response."""
        async with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._half_open_in_flight = 0
                logger.warning(
                    "[LLM-GW] circuit_breaker backend=%s HALF_OPEN→OPEN "
                    "(probe failed, failures=%d)",
                    self.backend_name,
                    self._consecutive_failures,
                )
            elif (
                self._state == CircuitState.CLOSED
                and self._consecutive_failures >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                logger.warning(
                    "[LLM-GW] circuit_breaker backend=%s CLOSED→OPEN "
                    "(failures=%d ≥ threshold=%d)",
                    self.backend_name,
                    self._consecutive_failures,
                    self.failure_threshold,
                )

    async def reset(self) -> None:
        """Force-reset to CLOSED (e.g. for admin / testing)."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._half_open_in_flight = 0
            logger.info(
                "[LLM-GW] circuit_breaker backend=%s force-reset to CLOSED",
                self.backend_name,
            )
