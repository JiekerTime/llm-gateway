
"""Request models for llm-gateway."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ThinkingMode = Literal["enabled", "disabled"]
ReasoningEffort = Literal["high", "max", "low", "medium", "xhigh"]

class ToolDefinition(BaseModel):
    """Caller-provided tool schema, forwarded to the LLM backend."""
    name: str
    description: str
    input_schema: dict = Field(default_factory=dict)

class ToolCall(BaseModel):
    """A single tool invocation returned by the LLM."""
    id: str
    name: str
    arguments: dict = Field(default_factory=dict)

class ToolResult(BaseModel):
    """Result of a tool execution (sent back by the caller, not the gateway)."""
    tool_call_id: str
    content: str
    is_error: bool = False


class JsonSchemaDefinition(BaseModel):
    """Schema definition for structured-output constrained decoding."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = "structured_response"
    schema_: dict = Field(default_factory=dict, alias="schema")
    description: str | None = None
    strict: bool = False


class ResponseFormat(BaseModel):
    """Unified structured-output request across providers."""

    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: JsonSchemaDefinition | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ResponseFormat:
        if self.type == "json_schema" and self.json_schema is None:
            raise ValueError("response_format.json_schema is required when type='json_schema'")
        if self.type != "json_schema" and self.json_schema is not None:
            raise ValueError("response_format.json_schema is only allowed when type='json_schema'")
        return self

class ChatRequest(BaseModel):
    """POST /chat request body."""
    messages: list[dict]
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 800
    caller: str = ""
    stream: bool = False
    thinking: ThinkingMode | None = None
    reasoning_effort: ReasoningEffort | None = None

    # Function Calling (optional)
    tools: list[ToolDefinition] | None = None
    tool_choice: str | None = None          # "auto" | "none" | "required" | specific name
    response_format: ResponseFormat | None = None

    # Session / role card (optional)
    session_id: str | None = None
    role_card: str | None = None
    dimension_overrides: dict[str, str] = Field(default_factory=dict)
    append_history: bool = True

    mode: str = "single_turn"               # reserved for future multi-turn

    @field_validator("thinking", mode="before")
    @classmethod
    def normalize_thinking(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        if isinstance(value, dict):
            return value.get("type")
        return value
