"""
Model router — resolves (caller, model) into a routing policy.

Each routing rule maps a caller pattern to a policy that includes:
- backend & model selection
- priority level
- cache-friendliness hint
- fallback permission
- max context token budget

Matching is done in list order; first match wins.
Supports `*` wildcard in caller_pattern (e.g. "meeting/*", "*").
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("llm-gw.router")

@dataclass
class RoutePolicy:
    """Resolved routing decision for a single request."""
    backend: str = ""
    model: str = ""
    priority: str = "normal"
    cacheable: bool = True
    fallback_allowed: bool = True
    max_context_tokens: int = 32000

@dataclass
class RoutingRule:
    """A single rule from config.yaml routing_rules."""
    caller_pattern: str = "*"
    backend: str = ""
    model: str = ""
    priority: str = "normal"
    cacheable: bool = True
    fallback_allowed: bool = True
    max_context_tokens: int = 32000

    def matches(self, caller: str) -> bool:
        """Check if this rule matches the given caller string."""
        return fnmatch.fnmatch(caller, self.caller_pattern)

    def to_policy(self) -> RoutePolicy:
        return RoutePolicy(
            backend=self.backend,
            model=self.model,
            priority=self.priority,
            cacheable=self.cacheable,
            fallback_allowed=self.fallback_allowed,
            max_context_tokens=self.max_context_tokens,
        )

@dataclass
class Router:
    """Resolves caller + optional model override into a RoutePolicy."""

    rules: list[RoutingRule] = field(default_factory=list)
    default_backend: str = "deepseek"
    default_model: str = "deepseek-chat"

    @classmethod
    def from_config(cls, config: dict) -> Router:
        """Build a Router from the top-level config dict."""
        rules = []
        for raw in config.get("routing_rules", []):
            rules.append(RoutingRule(
                caller_pattern=raw.get("caller_pattern", "*"),
                backend=raw.get("backend", config.get("default_backend", "deepseek")),
                model=raw.get("model", config.get("default_model", "deepseek-chat")),
                priority=raw.get("priority", "normal"),
                cacheable=raw.get("cacheable", True),
                fallback_allowed=raw.get("fallback_allowed", True),
                max_context_tokens=raw.get("max_context_tokens", 32000),
            ))

        return cls(
            rules=rules,
            default_backend=config.get("default_backend", "deepseek"),
            default_model=config.get("default_model", "deepseek-chat"),
        )

    def resolve(self, caller: str, model_override: str | None = None) -> RoutePolicy:
        for rule in self.rules:
            if rule.matches(caller):
                policy = rule.to_policy()
                if model_override:
                    policy.model = model_override
                logger.debug(
                    "[LLM-GW] route caller=%s → backend=%s model=%s (pattern=%s)",
                    caller, policy.backend, policy.model, rule.caller_pattern,
                )
                return policy

        policy = RoutePolicy(
            backend=self.default_backend,
            model=model_override or self.default_model,
        )
        logger.debug(
            "[LLM-GW] route caller=%s → default backend=%s model=%s",
            caller, policy.backend, policy.model,
        )
        return policy

    @property
    def rule_count(self) -> int:
        return len(self.rules)
