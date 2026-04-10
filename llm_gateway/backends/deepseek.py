"""DeepSeek backend — OpenAI-compatible via openai SDK (AsyncOpenAI)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from llm_gateway.backends.base import BaseBackend
from llm_gateway.models.request import ResponseFormat, ToolCall, ToolDefinition
from llm_gateway.models.response import BackendResult, StreamEvent
from llm_gateway.models.stats import UsageInfo

logger = logging.getLogger("llm-gw.backends.deepseek")

class DeepSeekBackend(BaseBackend):
    """Uses the openai Python SDK with a custom base_url."""

    def __init__(self, name: str, config: dict) -> None:
        super().__init__(name, config)
        api_key_env = config.get("api_key_env", "DEEPSEEK_API_KEY")
        api_key = os.environ.get(api_key_env, "")
        base_url = config.get("base_url", "https://api.deepseek.com")

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=self.timeout,
        )

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
        prepared_messages = _prepare_messages_for_response_format(messages, response_format)
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": prepared_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = _to_openai_tools(tools)
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if response_format and response_format.type != "text":
            kwargs["response_format"] = _to_openai_response_format(response_format)

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        content = choice.message.content
        parsed_tool_calls: list[ToolCall] | None = None
        stop_reason = _map_finish_reason(choice.finish_reason)

        if choice.message.tool_calls:
            parsed_tool_calls = []
            for tc in choice.message.tool_calls:
                arguments = tc.function.arguments
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                parsed_tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
                )

        usage = _parse_openai_usage(response.usage)

        return BackendResult(
            content=content,
            tool_calls=parsed_tool_calls,
            stop_reason=stop_reason,
            usage=usage,
        )

    async def health_check(self) -> bool:
        try:
            await self._client.models.list()
            return True
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
        if response_format is None or response_format.type in {"text", "json_object"}:
            return None
        return "response_format 'json_schema' is not supported by openai_compat backends yet"

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
        prepared_messages = _prepare_messages_for_response_format(messages, response_format)
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": prepared_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            kwargs["tools"] = _to_openai_tools(tools)
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if response_format and response_format.type != "text":
            kwargs["response_format"] = _to_openai_response_format(response_format)

        stream = await self._client.chat.completions.create(**kwargs)
        yielded_start = False
        tool_seen: dict[int, dict[str, str]] = {}
        stop_reason = "stop"

        async for chunk in stream:
            if not yielded_start:
                yielded_start = True
                yield StreamEvent(type="message_start")

            choice = chunk.choices[0] if chunk.choices else None
            if choice is not None:
                if choice.delta and choice.delta.content:
                    yield StreamEvent(type="text_delta", delta=choice.delta.content)

                if choice.delta and choice.delta.tool_calls:
                    for tc in choice.delta.tool_calls:
                        if tc.index is None:
                            continue
                        state = tool_seen.setdefault(tc.index, {})
                        tool_call_id = getattr(tc, "id", None) or state.get("id") or f"call_{tc.index}"
                        if tc.function and tc.function.name and not state.get("name"):
                            state["name"] = tc.function.name
                            state["id"] = tool_call_id
                            yield StreamEvent(
                                type="tool_call_start",
                                tool_call_id=tool_call_id,
                                tool_name=tc.function.name,
                                tool_index=tc.index,
                            )
                        if tc.function and tc.function.arguments:
                            yield StreamEvent(
                                type="tool_args_delta",
                                tool_call_id=tool_call_id,
                                tool_name=state.get("name"),
                                tool_index=tc.index,
                                arguments_delta=tc.function.arguments,
                            )

                if choice.finish_reason:
                    stop_reason = _map_finish_reason(choice.finish_reason)

            if chunk.usage is not None:
                yield StreamEvent(type="usage", usage=_parse_openai_usage(chunk.usage))

        yield StreamEvent(type="done", stop_reason=stop_reason)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_openai_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Convert gateway ToolDefinition list to OpenAI tools format."""
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


def _to_openai_response_format(response_format: ResponseFormat) -> dict:
    """Convert gateway structured-output settings to Chat Completions format."""
    if response_format.type == "json_object":
        return {"type": "json_object"}
    if response_format.type == "json_schema" and response_format.json_schema:
        schema = response_format.json_schema
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema.name,
                "schema": schema.schema_,
                "strict": schema.strict,
            },
        }
    raise ValueError(f"Unsupported response_format type: {response_format.type}")


def _prepare_messages_for_response_format(
    messages: list[dict],
    response_format: ResponseFormat | None,
) -> list[dict]:
    """Inject a minimal compatibility hint for providers that require JSON wording."""
    if response_format is None or response_format.type == "text":
        return messages

    note = "Return valid JSON only."
    if response_format.type == "json_schema":
        note = "Return valid JSON only, matching the requested schema."

    prepared = [dict(message) for message in messages]
    prepared.insert(0, {"role": "system", "content": note})
    return prepared

def _map_finish_reason(reason: str | None) -> str:
    """Map OpenAI finish_reason to gateway stop_reason."""
    mapping = {
        "stop": "stop",
        "tool_calls": "tool_use",
        "length": "length",
        "content_filter": "stop",
    }
    return mapping.get(reason or "stop", "stop")

def _parse_openai_usage(usage: Any) -> UsageInfo:
    """Extract usage info, including DeepSeek-specific cache fields."""
    if usage is None:
        return UsageInfo()

    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0

    cache_hit = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details:
        cache_hit = getattr(details, "cached_tokens", 0) or 0
    cache_miss = max(0, prompt_tokens - cache_hit)

    return UsageInfo(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
    )
