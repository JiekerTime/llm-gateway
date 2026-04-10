"""Role card models for session-backed persona reuse."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RoleCardDimension(BaseModel):
    """One ordered slice of a role card."""

    content: str
    priority: int = 0


class RoleCard(BaseModel):
    """Composable role card with stable prompt-prefix semantics."""

    name: str
    display_name: str = ""
    system_prompt: str = ""

    personality: RoleCardDimension | None = None
    scenario: RoleCardDimension | None = None
    knowledge: RoleCardDimension | None = None
    constraints: RoleCardDimension | None = None
    style: RoleCardDimension | None = None
    examples: RoleCardDimension | None = None

    extra_dimensions: dict[str, RoleCardDimension] = Field(default_factory=dict)

    max_history_turns: int = 50
    max_history_tokens: int = 8000
    temperature: float | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def build_system_prompt(
        self,
        dimension_overrides: dict[str, str] | None = None,
    ) -> str:
        """Build a deterministic system prompt with sorted sections."""
        overrides = dimension_overrides or {}
        sections: list[tuple[int, str, str]] = []

        if self.display_name:
            sections.append((-1000, "Identity", f"You are {self.display_name}."))
        if self.system_prompt.strip():
            sections.append((-900, "Core Instructions", self.system_prompt.strip()))

        for key, title, dimension in self._iter_dimensions():
            override_content = overrides.get(key)
            if override_content:
                priority = dimension.priority if dimension else 0
                sections.append((priority, title, override_content.strip()))
                continue

            if dimension and dimension.content.strip():
                sections.append((dimension.priority, title, dimension.content.strip()))

        sections.sort(key=lambda item: (item[0], item[1]))
        return "\n\n".join(f"## {title}\n{content}" for _, title, content in sections if content)

    def _iter_dimensions(self) -> list[tuple[str, str, RoleCardDimension | None]]:
        base_dimensions: list[tuple[str, str, RoleCardDimension | None]] = [
            ("personality", "Personality", self.personality),
            ("scenario", "Scenario", self.scenario),
            ("knowledge", "Knowledge", self.knowledge),
            ("constraints", "Constraints", self.constraints),
            ("style", "Style", self.style),
            ("examples", "Examples", self.examples),
        ]
        extra_dimensions = [
            (name, name.replace("_", " ").title(), dimension)
            for name, dimension in sorted(self.extra_dimensions.items())
        ]
        return base_dimensions + extra_dimensions
