"""
llm/router.py

按配置选择并实例化正确的 LLMBackend。

支持的 provider — 在 _BACKENDS 注册表中加一行即可新增：
    anthropic   → AnthropicBackend
    openai      → OpenAICompatBackend
    deepseek    → OpenAICompatBackend
    groq        → OpenAICompatBackend
    ollama      → OpenAICompatBackend
"""

from __future__ import annotations

import os
from typing import Callable

from llm.base import LLMBackend


# ---------------------------------------------------------------------------
# Backend 工厂函数（惰性 import，只有用到时才加载 SDK）
# ---------------------------------------------------------------------------

def _make_anthropic(
    model: str, api_key: str, base_url: str | None, max_tokens: int,
) -> LLMBackend:
    from llm.anthropic_backend import AnthropicBackend
    return AnthropicBackend(model=model, api_key=api_key, max_tokens=max_tokens)


def _make_openai_compat(
    model: str, api_key: str, base_url: str | None, max_tokens: int,
) -> LLMBackend:
    from llm.openai_compat import OpenAICompatBackend
    return OpenAICompatBackend(
        model=model, api_key=api_key, base_url=base_url, max_tokens=max_tokens,
    )


# provider → (factory_fn, default_base_url_or_none)
_BackendFactory = Callable[[str, str, str | None, int], LLMBackend]

_BACKENDS: dict[str, tuple[_BackendFactory, str | None]] = {
    "anthropic": (_make_anthropic,     None),
    "openai":    (_make_openai_compat,  None),
    "deepseek":  (_make_openai_compat,  "https://api.deepseek.com"),
    "groq":      (_make_openai_compat,  "https://api.groq.com/openai/v1"),
    "ollama":    (_make_openai_compat,  "http://localhost:11434/v1"),
}


# provider → 环境变量名（api_key 未显式配置时的 fallback）
_ENV_KEY_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "deepseek":  "DEEPSEEK_API_KEY",
    "groq":      "GROQ_API_KEY",
    "ollama":    "OLLAMA_API_KEY",
}


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def create_backend(
    provider: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int = 4096,
) -> LLMBackend:
    """
    工厂函数，根据 provider 创建对应的 LLMBackend。

    Args:
        provider:   "anthropic" | "openai" | "deepseek" | "groq" | "ollama"
        model:      模型名
        api_key:    API key，None 时从环境变量读取
        base_url:   覆盖默认 base_url
        max_tokens: 最大输出 token 数

    Returns:
        对应的 LLMBackend 实例

    Raises:
        ValueError: provider 不支持，或 api_key 缺失
    """
    provider = provider.lower().strip()

    entry = _BACKENDS.get(provider)
    if entry is None:
        supported = ", ".join(sorted(_BACKENDS))
        raise ValueError(
            f"Unsupported provider '{provider}'. Supported: {supported}"
        )

    # 解析 api_key
    resolved_key = api_key or os.environ.get(_ENV_KEY_MAP.get(provider, ""), "")
    if not resolved_key and provider != "ollama":
        env_var = _ENV_KEY_MAP.get(provider, "")
        raise ValueError(
            f"API key for '{provider}' not provided. "
            f"Set it via config or environment variable {env_var!r}."
        )
    if not resolved_key:
        resolved_key = "ollama"

    factory, default_base_url = entry
    return factory(model, resolved_key, base_url or default_base_url, max_tokens)


def create_backend_from_config(config: dict) -> LLMBackend:
    """从 config YAML 的 llm 节创建 backend。"""
    return create_backend(
        provider=config.get("provider", "anthropic"),
        model=config.get("model", "claude-sonnet-4-5"),
        api_key=config.get("api_key") or None,
        base_url=config.get("base_url") or None,
        max_tokens=int(config.get("max_tokens", 4096)),
    )
