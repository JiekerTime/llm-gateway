"""
Best-effort incremental JSON parsing for streaming deltas.

This parser never raises to the caller during normal use. It is designed as an
optional enhancement over raw streaming deltas, not as a hard dependency. When
the partial buffer cannot yet be stabilized into valid JSON, callers simply get
`None` and should continue relying on the raw stream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IncrementalJSONParser:
    """Accumulate text chunks and emit best-effort structured snapshots."""

    _chunks: list[str] = field(default_factory=list)
    _last_snapshot_json: str | None = None

    def append(self, delta: str | None) -> None:
        if delta:
            self._chunks.append(delta)

    @property
    def raw(self) -> str:
        return "".join(self._chunks)

    def snapshot(self) -> Any | None:
        """Return a parsed partial snapshot when the buffer is stabilizable."""
        raw = self.raw.strip()
        if not raw:
            return None

        candidate = _complete_partial_json(raw)
        if not candidate:
            return None

        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return None

        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if encoded == self._last_snapshot_json:
            return None

        self._last_snapshot_json = encoded
        return value

    def final(self) -> Any | None:
        """Parse the final buffer strictly first, then fall back to heuristics."""
        raw = self.raw.strip()
        if not raw:
            return None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        candidate = _complete_partial_json(raw)
        if not candidate:
            return None
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None


def _complete_partial_json(raw: str) -> str | None:
    """Heuristically close an incomplete top-level JSON object or array."""
    if not raw or raw[0] not in "{[":
        return None

    chars = list(raw)
    stack: list[str] = []
    in_string = False
    escape = False

    for ch in raw:
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == "\"":
                in_string = False
            continue

        if ch == "\"":
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()

    if in_string:
        chars.append("\"")

    stabilized = "".join(chars).rstrip()
    stabilized = _patch_trailing_token(stabilized)
    if stabilized is None:
        return None

    closing = []
    for token in reversed(stack):
        closing.append("}" if token == "{" else "]")
    return stabilized + "".join(closing)


def _patch_trailing_token(candidate: str) -> str | None:
    """Patch common trailing incomplete states in streamed JSON."""
    text = candidate.rstrip()
    if not text:
        return None

    while text and text[-1] in {",", ":"}:
        if text[-1] == ",":
            text = text[:-1].rstrip()
            continue
        if text[-1] == ":":
            text = text + " null"
            break

    for literal, replacement in (
        (" tru", " true"),
        (" fal", " false"),
        (" nul", " null"),
    ):
        if text.endswith(literal):
            return text[: -len(literal)] + replacement

    return text
