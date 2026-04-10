"""Ollama backend — direct HTTP via httpx.AsyncClient."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from llm_gateway.backends.base import BaseBackend
from llm_gateway.models.request import ResponseFormat, ToolCall, ToolDefinition
from llm_gateway.models.response import BackendResult, StreamEvent
from llm_gateway.models.stats import UsageInfo

logger = logging.getLogger("llm-gw.backends.ollama")

class OllamaBackend(BaseBackend):
    """Direct HTTP calls to the Ollama REST API."""

    def __init__(self, name: str, config: dict) -> None:
        super().__init__(name, config)
        self._base_url = config.get("base_url", "http://ollama:11434").rstrip("/")
        self._keep_alive = config.get("keep_alive", "5m")
        self._client = httpx.AsyncClient(timeout=self.timeout)

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
        async with self._semaphore:
            self._active_calls += 1
            try:
                return await self._do_call(
                    messages,
                    model,
                    temperature,
                    max_tokens,
                    tools,
                    tool_choice,
                    response_format,
                )
            finally:
                self._active_calls -= 1

    async def _do_call(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: list[ToolDefinition] | None,
        tool_choice: str | None,
        response_format: ResponseFormat | None,
    ) -> BackendResult:
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": False,
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        if tools:
            payload["tools"] = _to_ollama_tools(tools)
        if response_format and response_format.type != "text":
            payload["format"] = _to_ollama_response_format(response_format)

        response = await self._client.post(
            f"{self._base_url}/api/chat",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        content = data.get("message", {}).get("content")
        parsed_tool_calls = _parse_ollama_tool_calls(data)
        stop_reason = "tool_use" if parsed_tool_calls else "stop"

        usage = UsageInfo(
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
        )

        return BackendResult(
            content=content,
            tool_calls=parsed_tool_calls,
            stop_reason=stop_reason,
            usage=usage,
        )

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base_url}/api/tags")
            return resp.status_code == 200
        except Exception:
            return False

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
        async with self._semaphore:
            self._active_calls += 1
            try:
                async for event in self._do_stream_call(
                    messages,
                    model,
                    temperature,
                    max_tokens,
                    tools,
                    tool_choice,
                    response_format,
                ):
                    yield event
            finally:
                self._active_calls -= 1

    def validate_response_format(
        self,
        response_format: ResponseFormat | None,
    ) -> str | None:
        if response_format is None or response_format.type in {"text", "json_object", "json_schema"}:
            return None
        return f"response_format '{response_format.type}' is not supported by ollama"

    async def _do_stream_call(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: list[ToolDefinition] | None,
        tool_choice: str | None,
        response_format: ResponseFormat | None,
    ) -> AsyncIterator[StreamEvent]:
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": True,
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        if tools:
            payload["tools"] = _to_ollama_tools(tools)
        if response_format and response_format.type != "text":
            payload["format"] = _to_ollama_response_format(response_format)

        yielded_start = False

        async with self._client.stream(
            "POST",
            f"{self._base_url}/api/chat",
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue

                data = json.loads(line)

                if not yielded_start:
                    yielded_start = True
                    yield StreamEvent(type="message_start")

                message = data.get("message", {})
                if message.get("content"):
                    yield StreamEvent(type="text_delta", delta=message["content"])

                raw_calls = message.get("tool_calls") or []
                for idx, tc in enumerate(raw_calls):
                    func = tc.get("function", {})
                    tool_call_id = f"ollama_call_{idx}"
                    name = func.get("name", "")
                    yield StreamEvent(
                        type="tool_call_start",
                        tool_call_id=tool_call_id,
                        tool_name=name,
                        tool_index=idx,
                    )

                    arguments = func.get("arguments", {})
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    if arguments:
                        yield StreamEvent(
                            type="tool_args_delta",
                            tool_call_id=tool_call_id,
                            tool_name=name,
                            tool_index=idx,
                            arguments_delta=str(arguments),
                        )

                if data.get("done"):
                    usage = UsageInfo(
                        prompt_tokens=data.get("prompt_eval_count", 0),
                        completion_tokens=data.get("eval_count", 0),
                        total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
                    )
                    yield StreamEvent(type="usage", usage=usage)
                    yield StreamEvent(
                        type="done",
                        stop_reason=str(data.get("done_reason") or "stop"),
                    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_ollama_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Convert gateway ToolDefinition list to Ollama tools format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]

def _parse_ollama_tool_calls(data: dict) -> list[ToolCall] | None:
    """Extract tool calls from Ollama response if present."""
    message = data.get("message", {})
    raw_calls = message.get("tool_calls")
    if not raw_calls:
        return None

    result = []
    for idx, tc in enumerate(raw_calls):
        func = tc.get("function", {})
        arguments = func.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw": arguments}
        result.append(
            ToolCall(
                id=f"ollama_call_{idx}",
                name=func.get("name", ""),
                arguments=arguments,
            )
        )
    return result if result else None


def _to_ollama_response_format(response_format: ResponseFormat) -> str | dict:
    """Convert gateway structured-output settings to Ollama's `format` field."""
    if response_format.type == "json_object":
        return "json"
    if response_format.type == "json_schema" and response_format.json_schema:
        return response_format.json_schema.schema_
    raise ValueError(f"Unsupported response_format type: {response_format.type}")
