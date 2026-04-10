from llm_gateway.backends.base import BaseBackend
from llm_gateway.backends.deepseek import DeepSeekBackend
from llm_gateway.backends.ollama import OllamaBackend

BACKEND_REGISTRY: dict[str, type[BaseBackend]] = {
    "openai_compat": DeepSeekBackend,
    "ollama": OllamaBackend,
}

def create_backend(name: str, config: dict) -> BaseBackend:
    """Instantiate a backend by its `type` field in config."""
    backend_type = config.get("type", "openai_compat")
    cls = BACKEND_REGISTRY.get(backend_type)
    if cls is None:
        raise ValueError(
            f"Unknown backend type '{backend_type}' for '{name}'. "
            f"Available: {list(BACKEND_REGISTRY.keys())}"
        )
    return cls(name, config)

__all__ = [
    "BaseBackend",
    "DeepSeekBackend",
    "OllamaBackend",
    "BACKEND_REGISTRY",
    "create_backend",
]
