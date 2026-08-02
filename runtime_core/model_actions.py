"""
H0: Typed ModelAction + TokenUsage — no object/raw dict from LLM boundary.

LLMPort returns one of these sum types.  ToolCall params use FrozenJsonObject.
H0: Every ModelAction subclass now carries optional TokenUsage from the provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.json_values import FrozenJsonObject


# ── H0: TokenUsage ──────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Immutable token usage from LLM provider response.

    Extracted from the provider's `usage` object (Anthropic: usage.input_tokens
    / output_tokens; OpenAI: usage.prompt_tokens / completion_tokens).
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


# ── ModelAction sum types ───────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AssistantText:
    """Plain text response from the model."""
    text: str
    stop_reason: str = ""
    usage: TokenUsage | None = None  # H0: provider token usage


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single tool invocation request."""
    id: str
    name: str
    params: FrozenJsonObject  # G15: was dict
    usage: TokenUsage | None = None  # H0: provider token usage for this turn


@dataclass(frozen=True, slots=True)
class ToolCallBatch:
    """Multiple tool calls in a single model response."""
    calls: tuple[ToolCall, ...]
    usage: TokenUsage | None = None  # H0: provider token usage for this turn


@dataclass(frozen=True, slots=True)
class ModelStop:
    """Model indicates the run is complete."""
    stop_reason: str = "end_turn"
    text: str = ""
    usage: TokenUsage | None = None  # H0: provider token usage


@dataclass(frozen=True, slots=True)
class ModelRefusal:
    """Model refused to answer (safety/content policy)."""
    reason: str = ""
    usage: TokenUsage | None = None  # H0: provider token usage


@dataclass(frozen=True, slots=True)
class ModelFailure:
    """Model provider returned an error."""
    error: str
    retryable: bool = False
    usage: TokenUsage | None = None  # H0: provider token usage (partial, if any)


# ── Sum type ───────────────────────────────────────────────────────────────

ModelAction = AssistantText | ToolCall | ToolCallBatch | ModelStop | ModelRefusal | ModelFailure
