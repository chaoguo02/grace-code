"""Backend factory for chat model switches.

Extracted from ChatSession.switch_model().
"""

from __future__ import annotations

import os
from typing import Any


def rebuild_backend_for_model(
    model: str,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    current_provider: str = "",
) -> tuple[Any, str, str]:
    """Create a new LLM backend for a model switch.

    Returns (backend, model_name, provider_name).
    Does NOT mutate any ChatSession state — caller is responsible for that.
    """
    from llm.router import create_backend

    provider = provider or current_provider or "openai"
    resolved_key = api_key or os.environ.get(
        {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
         "deepseek": "DEEPSEEK_API_KEY", "groq": "GROQ_API_KEY",
         "ollama": "OLLAMA_API_KEY", "openai-compat": "OPENAI_API_KEY"}.get(provider, ""), "",
    )
    backend = create_backend(
        provider=provider, model=model,
        api_key=resolved_key or None,
        base_url=base_url,
    )
    return backend, model, provider
