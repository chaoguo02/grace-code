"""
G15: Runtime ports — fully typed, non-Optional, no UI concerns.

Every port is an explicit Protocol.  RuntimePorts has ALL required ports.
Tests must provide explicit fakes — no Optional shortcuts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Protocol

from core.eventing.identifiers import SessionId, RunId, TaskId
from core.json_values import FrozenJsonObject
from runtime_core.model_actions import ModelAction


# ── Tool outcome ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ToolSuccess:
    tool_name: str = ""
    result: FrozenJsonObject | None = None
    output: str = ""
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolFailure:
    tool_name: str = ""
    error: str = ""
    error_type: str = ""
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolDenied:
    tool_name: str = ""
    reason: str = ""


ToolOutcome = ToolSuccess | ToolFailure | ToolDenied


# ── Hook gate ───────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class HookGateResult:
    allowed: bool
    reason: str = ""
    updated_input: FrozenJsonObject | None = None
    decision: object | None = None
    additional_context: str = ""  # G17: PostToolUse context


class HookGatePort(Protocol):
    """Synchronous lifecycle decision gate."""
    def check(self, event_type: str, hook_input: object,
              tool_name: str = "") -> HookGateResult: ...


# ── Ports ───────────────────────────────────────────────────────────────────

class LLMPort(Protocol):
    """Call the language model.  Returns typed ModelAction."""
    def invoke(self, messages: FrozenJsonObject,
               tools: FrozenJsonObject | None = None) -> ModelAction: ...

    def stream(self, messages: FrozenJsonObject,
               tools: FrozenJsonObject | None = None) -> Awaitable[ModelAction]: ...


class ToolPort(Protocol):
    """Execute a tool call.  Returns typed ToolOutcome."""
    def execute(self, tool_name: str, params: FrozenJsonObject,
                invocation_id: str = "") -> ToolOutcome: ...


class LiveEventPort(Protocol):
    """Publish non-authoritative live events (scoped)."""
    def publish(self, event_type: str, payload: FrozenJsonObject) -> None: ...


class ClockPort(Protocol):
    """Monotonic clock for timeouts and duration measurement."""
    def now(self) -> float: ...
    def deadline(self, timeout_s: float) -> float: ...


class TokenUsagePort(Protocol):
    """Record token usage for cost tracking."""
    def record(self, run_id: RunId, input_tokens: int,
               output_tokens: int) -> None: ...


class CancellationPort(Protocol):
    """Check if the current run has been cancelled."""
    @property
    def cancelled(self) -> bool: ...


# ── RuntimePorts ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RuntimePorts:
    """All ports required by AgentRuntime.  None are Optional in production.

    G15: No web_mode or UI concerns.  Every port is explicit and typed.
    """
    llm: LLMPort
    tools: ToolPort
    hooks: HookGatePort
    live_events: LiveEventPort
    clock: ClockPort
    token_usage: TokenUsagePort
    cancellation: CancellationPort
