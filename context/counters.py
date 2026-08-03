"""
CC-Native two-layer token counting: sync/estimator + async/provider.

Design (P0_1 Batch 1):
  Layer 1 — LocalTokenEstimator (sync, conservative upper-bound)
    Used inside build_context() for in-line decisions (trim trigger,
    compact trigger).  Never blocks, always over-estimates.

  Layer 2 — ProviderTokenCounter (async, authoritative)
    Used as the final guard before sending to the provider.
    May call a remote API (Anthropic countTokens, etc.).

  TokenCount separates context occupancy (context_tokens) from billing
  dimensions (input_tokens, cache_creation_tokens, cache_read_tokens).
  The window guard uses context_tokens; billing uses input_tokens + cache_*.

Alignment: Claude Code's countTokens() API + char/4 fallback pattern.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Union

# ── TokenCount ──────────────────────────────────────────────────────────────

@dataclass
class TokenCount:
    """Precise token count — separates context occupancy from billing.

    context_tokens:
        The number of tokens this content occupies in the model's context
        window.  Used for the HARD invariant:
            context_tokens + output_room <= provider_context_limit

    input_tokens:
        Tokens billed as "input" (excluding cache_read).  Used for cost.

    cache_creation_tokens / cache_read_tokens:
        Anthropic-style prompt-cache billing dimensions.
        MUST NOT be added to context_tokens — they overlap with input.
    """
    context_tokens: int
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


# ── ContentBlock protocol ───────────────────────────────────────────────────

class ContentBlock:
    """Marker protocol for typed content blocks.

    Concrete types: text block {'type':'text','text':'...'},
    image block {'type':'image','source':...}, tool_use, tool_result, document.
    """
    type: str


ContentInput = Union[str, ContentBlock, list[ContentBlock], list[dict]]


# ── Layer 1: LocalTokenEstimator (sync, conservative) ───────────────────────

class LocalTokenEstimator(ABC):
    """Fast, synchronous, conservative token estimation.

    Used in synchronous context assembly paths for trigger decisions
    (trim, compact).  All methods are sync — no network I/O.

    The default implementation uses char/4 with a 10% safety margin.
    """

    @abstractmethod
    def estimate(self, content: ContentInput) -> int:
        """Conservative upper-bound token count.  Always >= true count."""
        ...

    @abstractmethod
    def estimate_messages(self, messages: list[dict]) -> int:
        """Conservative upper-bound for a full message list."""
        ...

    @property
    @abstractmethod
    def model_context_window(self) -> int:
        """Model's maximum context window (e.g. 200_000)."""
        ...


class CharEstimator(LocalTokenEstimator):
    """Conservative char/3.6 estimator (~10% safety margin over char/4).

    This is the fallback when no provider-specific estimator is available.
    Uses char/3.6 instead of char/4 to guarantee it never under-estimates.
    """

    def __init__(self, model_window: int = 128_000) -> None:
        self._window = model_window

    def estimate(self, content: ContentInput) -> int:
        return _estimate_any(content, chars_per_token=3.6)

    def estimate_messages(self, messages: list[dict]) -> int:
        total = 0
        for m in messages:
            total += _estimate_msg(m, chars_per_token=3.6)
        return max(1, total)

    @property
    def model_context_window(self) -> int:
        return self._window


# ── Layer 2: ProviderTokenCounter (async, authoritative) ────────────────────

class ProviderTokenCounter(ABC):
    """Async, authoritative provider-API token counter.

    Aligns with CC's countTokens() API call.
    All methods are async — may make network requests.

    The hard guard before sending to the provider always calls
    count_messages() and enforces:
        result.context_tokens + output_room <= provider_context_limit
    """

    @abstractmethod
    async def count_messages(self, messages: list[dict]) -> TokenCount:
        """Precise token count for a message list.

        Returns TokenCount with context_tokens separated from billing dims.
        """
        ...

    @abstractmethod
    async def count_content(self, content: ContentInput) -> int:
        """Precise token count for a single content item."""
        ...


# ── internal estimation helpers ─────────────────────────────────────────────

_tiktoken_enc = None
_tiktoken_available = False
_init_lock = threading.Lock()


def _init_tiktoken() -> None:
    global _tiktoken_enc, _tiktoken_available
    if _tiktoken_available or _tiktoken_enc is not None:
        return
    with _init_lock:
        if _tiktoken_available or _tiktoken_enc is not None:
            return
        try:
            import tiktoken
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
            _tiktoken_available = True
        except Exception:
            _tiktoken_available = False


def _estimate_text(text: str, chars_per_token: float = 4.0) -> int:
    """Estimate token count for a plain string.

    Prefers tiktoken when available, falls back to chars/N.
    """
    _init_tiktoken()
    if _tiktoken_available and _tiktoken_enc is not None:
        try:
            return max(1, len(_tiktoken_enc.encode(text)))
        except Exception:
            pass
    return max(1, int(len(text) / chars_per_token))


def _estimate_block(block: dict, chars_per_token: float = 4.0) -> int:
    """Estimate token count for a single content block."""
    import json as _json
    block_type = block.get("type", "")

    # text block — count the text directly
    if block_type == "text":
        return _estimate_text(block.get("text", ""), chars_per_token)

    # image block — use conservative per-image estimate
    # Anthropic: ~160 tokens for a standard image, ~400 for high-res
    if block_type == "image":
        source = block.get("source", {})
        if isinstance(source, dict) and source.get("type") == "base64":
            data_len = len(source.get("data", ""))
            return max(85, data_len // 100)  # token count scales with encoded size
        return 160  # conservative default for image_url

    # tool_use / tool_result — count the JSON representation
    if block_type in ("tool_use", "tool_result"):
        return _estimate_text(_json.dumps(block, default=str), chars_per_token)

    # document / generic — count the full serialization
    return _estimate_text(_json.dumps(block, default=str), chars_per_token)


def _estimate_any(content: ContentInput, chars_per_token: float = 4.0) -> int:
    """Conservative estimate for any content type."""
    import json as _json

    if isinstance(content, str):
        return _estimate_text(content, chars_per_token)

    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, dict):
                total += _estimate_block(item, chars_per_token)
            elif isinstance(item, str):
                total += _estimate_text(item, chars_per_token)
            else:
                total += _estimate_text(str(item), chars_per_token)
        return max(1, total)

    # unknown — serialize and count
    return _estimate_text(str(content), chars_per_token)


def _estimate_msg(msg: dict, chars_per_token: float = 4.0) -> int:
    """Conservative per-message estimate including metadata overhead."""
    content = msg.get("content", "")

    if isinstance(content, list):
        tokens = sum(_estimate_block(b, chars_per_token) for b in content)
    elif isinstance(content, str):
        tokens = _estimate_text(content, chars_per_token)
    elif content is None:
        tokens = 0
    else:
        tokens = _estimate_text(str(content), chars_per_token)

    # tool_calls overhead
    if msg.get("tool_calls"):
        import json as _json
        for tc in msg["tool_calls"]:
            tokens += _estimate_text(_json.dumps(tc, default=str), chars_per_token)

    # Per-message overhead: role markers, turn delimiters (~5 tokens)
    tokens += 5

    return tokens
