
"""Configuration loader for llm-gateway."""

from __future__ import annotations

import os

import yaml

def load_config(path: str = "config.yaml") -> dict:
    """Read and parse the YAML configuration file."""
    config_path = os.environ.get("LLM_GATEWAY_CONFIG", path)
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)
