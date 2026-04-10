"""Abstract base class for LLM backends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from llm_gateway.models.request import ResponseFormat, ToolDefinition
from llm_gateway.models.response import BackendResult, StreamEvent

class BaseBackend:
    """Common interface every backend must implement."""

    name: str = ""
    default_model: str = ""
    timeout: float = 120.0
    max_concurrent: int = 10

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self.default_model = config.get("default_model", "")
        self.timeout = config.get("timeout", 120)
        self.max_concurrent = config.get("max_concurrent", 10)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._active_calls = 0

    @property
    def active_calls(self) -> int:
        return self._active_calls

    async def call(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
        response_format: ResponseFormat | None = None,
    ) -> BackendResult:
        raise NotImplementedError

    async def stream_call(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
        response_format: ResponseFormat | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    async def health_check(self) -> bool:
        """Quick connectivity check. Override per backend."""
        return True

    def validate_response_format(
        self,
        response_format: ResponseFormat | None,
    ) -> str | None:
        """Return an error message when the backend cannot satisfy the request."""
        if response_format is None or response_format.type == "text":
            return None
        return f"response_format '{response_format.type}' is not supported"
