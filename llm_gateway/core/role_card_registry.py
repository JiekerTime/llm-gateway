"""Role card registry loading cards from config and disk."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from llm_gateway.models.role_card import RoleCard, RoleCardDimension


class RoleCardRegistry:
    """Load and resolve role cards from config and standalone files."""

    def __init__(self, config: dict | None = None) -> None:
        self._cards: dict[str, RoleCard] = {}
        if config:
            self.load_from_config(config)

    def load_from_config(self, cards_config: dict[str, Any] | None) -> None:
        if not cards_config:
            return
        for name, raw in cards_config.items():
            self._cards[name] = self._parse_card(name, raw)

    def load_from_directory(self, card_dir: str | None) -> None:
        if not card_dir:
            return

        base_path = Path(card_dir)
        if not base_path.exists():
            return

        for path in sorted(base_path.iterdir()):
            if path.suffix.lower() not in {".yaml", ".yml", ".json"} or not path.is_file():
                continue
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            name = str(raw.get("name") or path.stem)
            self._cards[name] = self._parse_card(name, raw)

    def get(self, name: str) -> RoleCard | None:
        return self._cards.get(name)

    def list_all(self) -> list[RoleCard]:
        return [self._cards[name] for name in sorted(self._cards)]

    def _parse_card(self, name: str, raw: dict[str, Any]) -> RoleCard:
        normalized = dict(raw)
        normalized["name"] = name

        if "personality" not in normalized and raw.get("description"):
            normalized["personality"] = raw["description"]
        if "scenario" not in normalized and raw.get("context"):
            normalized["scenario"] = raw["context"]
        if "knowledge" not in normalized and raw.get("background"):
            normalized["knowledge"] = raw["background"]
        if "constraints" not in normalized and raw.get("rules"):
            normalized["constraints"] = raw["rules"]
        if "style" not in normalized and raw.get("speaking_style"):
            normalized["style"] = raw["speaking_style"]
        if "examples" not in normalized and raw.get("mes_example"):
            normalized["examples"] = raw["mes_example"]
        if not normalized.get("system_prompt") and raw.get("main_prompt"):
            normalized["system_prompt"] = raw["main_prompt"]

        for field_name in (
            "personality",
            "scenario",
            "knowledge",
            "constraints",
            "style",
            "examples",
        ):
            if field_name in normalized and normalized[field_name] is not None:
                normalized[field_name] = _to_dimension(normalized[field_name])

        extra_dimensions = normalized.get("extra_dimensions") or {}
        normalized["extra_dimensions"] = {
            key: _to_dimension(value)
            for key, value in extra_dimensions.items()
        }

        return RoleCard.model_validate(normalized)


def _to_dimension(value: Any) -> RoleCardDimension:
    if isinstance(value, RoleCardDimension):
        return value
    if isinstance(value, str):
        return RoleCardDimension(content=value)
    if isinstance(value, dict):
        return RoleCardDimension.model_validate(value)
    raise TypeError(f"Unsupported role card dimension value: {type(value)!r}")
