"""
G15: Typed ModelAction — no object/raw dict from LLM boundary.

LLMPort returns one of these sum types.  ToolCall params use FrozenJsonObject.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.json_values import FrozenJsonObject


@dataclass(frozen=True, slots=True)
class AssistantText:
    """Plain text response from the model."""
    text: str
    stop_reason: str = ""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single tool invocation request."""
    id: str
    name: str
    params: FrozenJsonObject  # G15: was dict


@dataclass(frozen=True, slots=True)
class ToolCallBatch:
    """Multiple tool calls in a single model response."""
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True, slots=True)
class ModelStop:
    """Model indicates the run is complete."""
    stop_reason: str = "end_turn"
    text: str = ""


@dataclass(frozen=True, slots=True)
class ModelRefusal:
    """Model refused to answer (safety/content policy)."""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ModelFailure:
    """Model provider returned an error."""
    error: str
    retryable: bool = False


# ── Sum type ───────────────────────────────────────────────────────────────

ModelAction = AssistantText | ToolCall | ToolCallBatch | ModelStop | ModelRefusal | ModelFailure
