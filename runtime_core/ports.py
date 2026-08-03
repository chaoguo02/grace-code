"""
G15: Runtime ports — fully typed, non-Optional, no UI concerns.

Every port is an explicit Protocol.  RuntimePorts has ALL required ports.
Tests must provide explicit fakes — no Optional shortcuts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Protocol
if TYPE_CHECKING:
    from runtime_core.tool_scheduler import ToolMetadata

from core.eventing.identifiers import SessionId, RunId, TaskId
from core.json_values import FrozenJsonObject
from runtime_core.model_actions import ModelAction


# ── T0: Tool error classification ───────────────────────────────────────

from enum import StrEnum

class ToolErrorType(StrEnum):
    """CC-aligned tool error types.  Each maps to a retry strategy."""
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    NETWORK_ERROR = "network_error"
    VALIDATION_ERROR = "validation_error"
    TOOL_NOT_FOUND = "tool_not_found"
    EXECUTION_ERROR = "execution_error"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    CANCELLED = "cancelled"

# T0: Retry strategy per error type — CC behavior
# AUTOMATIC = retry without asking; APPROVAL = ask user; NEVER = don't retry
ERROR_RETRY_MAP: dict[ToolErrorType, str] = {
    ToolErrorType.TIMEOUT: "automatic",
    ToolErrorType.NETWORK_ERROR: "automatic",
    ToolErrorType.RESOURCE_EXHAUSTED: "automatic",
    ToolErrorType.VALIDATION_ERROR: "approval",
    ToolErrorType.EXECUTION_ERROR: "approval",
    ToolErrorType.PERMISSION_DENIED: "never",
    ToolErrorType.TOOL_NOT_FOUND: "never",
    ToolErrorType.CANCELLED: "never",
}


# ── Tool outcome ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ToolSuccess:
    tool_name: str = ""
    result: FrozenJsonObject | None = None
    output: str = ""
    duration_ms: float = 0.0
    tool_use_id: str = ""  # T1: CC tool_use_id for traceability

    def to_chat_block(self) -> dict:
        """T9: Convert to CC-compatible tool_result content block."""
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": self.output,
        }


@dataclass(frozen=True, slots=True)
class ToolFailure:
    tool_name: str = ""
    error: str = ""
    error_type: ToolErrorType = ToolErrorType.EXECUTION_ERROR  # T0: was str
    duration_ms: float = 0.0

    @property
    def retryable(self) -> bool:
        """T0: derived from ERROR_RETRY_MAP."""
        return ERROR_RETRY_MAP.get(self.error_type, "never") != "never"

    def to_chat_block(self) -> dict:
        """T9: CC-compatible error block with is_error flag."""
        return {
            "type": "tool_result",
            "tool_use_id": "",
            "content": self.error,
            "is_error": True,
        }


@dataclass(frozen=True, slots=True)
class ToolDenied:
    tool_name: str = ""
    reason: str = ""

    def to_chat_block(self) -> dict:
        """T9: Denied tool → error block with permission denial."""
        return {
            "type": "tool_result",
            "tool_use_id": "",
            "content": f"Tool denied: {self.reason}",
            "is_error": True,
        }


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
    """Call the language model.  Returns typed ModelAction.

    H0: Returned ModelAction carries optional TokenUsage in its .usage field.
    T5: tool_choice controls tool-calling behavior (CC-aligned).
      - {"type": "auto"} — model decides (default)
      - {"type": "any"} — must call at least one tool
      - {"type": "tool", "name": "..."} — force specific tool
      - {"type": "none"} — text-only response
    """
    def invoke(self, messages: "list[dict]",
               tools: "list[dict] | None" = None,
               tool_choice: dict | None = None) -> ModelAction: ...

    def stream(self, messages: "list[dict]",
               tools: "list[dict] | None" = None,
               tool_choice: dict | None = None) -> Awaitable[ModelAction]: ...


class ToolPort(Protocol):
    """Execute a tool call.  Returns typed ToolOutcome."""
    def execute(self, tool_name: str, params: FrozenJsonObject,
                invocation_id: str = "") -> ToolOutcome: ...


class ToolRegistryPort(Protocol):
    """T4: Tool registry — discovery, lifecycle, metadata."""
    def register(self, tool) -> None: ...
    def unregister(self, name: str) -> None: ...
    def resolve(self, name: str) -> object | None: ...
    def list_names(self) -> list[str]: ...
    def metadata_for(self, name: str) -> "ToolMetadata | None": ...


@dataclass(frozen=True, slots=True)
class PermissionResult:
    """T11: Result of a permission check."""
    allowed: bool = True
    reason: str = ""
    mode: str = ""  # auto, default, bypass


class PermissionPipelinePort(Protocol):
    """T11: Permission check — CC permission modes."""
    def check(self, tool_name: str, params: FrozenJsonObject,
              session_id: str = "") -> PermissionResult: ...
    def set_mode(self, mode: str) -> None: ...


class LiveEventPort(Protocol):
    """Publish non-authoritative live events (scoped).

    R1: scope parameter enables exact-scope routing via ScopedEventBus.
    When scope is None, the implementation may fall back to best-effort logging.
    """
    def publish(self, event_type: str, payload: FrozenJsonObject,
                scope: "ScopeToken | None" = None) -> None: ...


class ClockPort(Protocol):
    """Monotonic clock for timeouts and duration measurement."""
    def now(self) -> float: ...
    def deadline(self, timeout_s: float) -> float: ...


class TokenUsagePort(Protocol):
    """Record token usage for cost tracking."""
    def record(self, run_id: RunId, input_tokens: int,
               output_tokens: int) -> None: ...


# ── RuntimePorts ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RuntimePorts:
    """All ports required by AgentRuntime.  None are Optional in production.

    R2: CancellationPort removed — step_loop uses context.cancellation directly.
    G15: No web_mode or UI concerns.  Every port is explicit and typed.
    """
    llm: LLMPort
    tools: ToolPort
    hooks: HookGatePort
    live_events: LiveEventPort
    clock: ClockPort
    token_usage: TokenUsagePort
