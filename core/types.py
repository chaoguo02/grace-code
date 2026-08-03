"""core/types.py

Core data types — extracted from core/base.py for better cohesion.
core/base.py re-exports all symbols for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hooks.protocol import HookAttachment


# ---------------------------------------------------------------------------
# ObservationStatus, ToolOutcome, Observation
# ---------------------------------------------------------------------------

class ObservationStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


class ToolOutcome(str, Enum):
    NONE = "none"
    EMPTY = "empty"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    FAILED = "failed"
    TEST_TARGET_MISSING = "test_target_missing"


@dataclass
class Observation:
    status: ObservationStatus
    output: str
    tool_name: str
    tokens_used: int = 0
    error: str | None = None
    metadata: dict[str, Any] | None = None
    outcome: ToolOutcome = ToolOutcome.NONE
    modified_files: list[str] = field(default_factory=list)
    attachments: tuple["HookAttachment", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = {
            k: v.value if isinstance(v, Enum) else v
            for k, v in self.__dict__.items()
        }
        result["attachments"] = [
            {
                "kind": attachment.kind.value,
                "text": attachment.text,
                "source": attachment.source,
            }
            for attachment in self.attachments
        ]
        return result

    def is_success(self) -> bool:
        return self.status == ObservationStatus.SUCCESS

    def is_expected_block(self) -> bool:
        return bool(self.metadata and self.metadata.get("expected_block"))

    def __repr__(self) -> str:
        return (
            f"Observation(tool={self.tool_name}, "
            f"status={self.status.value}, "
            f"len={len(self.output)})"
        )


# ---------------------------------------------------------------------------
# ActionType, ToolCall, Action
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    TOOL_CALL = "tool_call"
    REFLECTION = "reflection"
    FINISH = "finish"
    GIVE_UP = "give_up"


@dataclass
class ToolCall:
    name: str
    params: dict[str, Any]
    id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"name": self.name, "params": self.params}
        if self.id is not None:
            payload["id"] = self.id
        return payload


@dataclass
class Action:
    action_type: ActionType
    thought: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "thought": self.thought,
            "message": self.message,
            "tool_calls": [tool_call.to_dict() for tool_call in self.tool_calls],
        }

    def is_terminal(self) -> bool:
        return self.action_type in (ActionType.FINISH, ActionType.GIVE_UP)

    def __repr__(self) -> str:
        if self.tool_calls:
            names = " + ".join(tool_call.name for tool_call in self.tool_calls)
            return f"Action({self.action_type.value}, tools=[{names}])"
        return f"Action({self.action_type.value})"


# ---------------------------------------------------------------------------
# LLMToolSchema — 工具 Schema
# ---------------------------------------------------------------------------

class ToolDescriptionTier(str, Enum):
    """Runtime description fidelity for LLM context.

    Tools with high call frequency stay at FULL.  Low-frequency tools
    are downgraded to SUMMARY or SCHEMA_ONLY when context pressure
    exceeds the dynamic budget.

    SCHEMA_ONLY preserves the parameter schema — the model can still
    invoke the tool correctly even without the full description text.
    NAME_ONLY was removed (Phase 2 #5) because a tool without visible
    parameters is effectively invisible to the model.
    """
    FULL = "full"              # Complete description + prompt_contract + params
    SUMMARY = "summary"        # One-line description + params (no contract)
    SCHEMA_ONLY = "schema_only"  # Name + first-sentence description + full params
    NAME_ONLY = "name_only"    # DEPRECATED — kept for backward compat, never selected


@dataclass
class LLMToolSchema:
    """向 LLM 描述一个可用工具的 schema。"""
    name: str
    description: str
    parameters: dict[str, Any]
    prompt_contract: tuple[str, ...] = ()
    deferred: bool = False
    tier: ToolDescriptionTier = ToolDescriptionTier.FULL


# ---------------------------------------------------------------------------
# Tool metadata enums
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    """Declarative risk metadata assigned by tools.

    Not yet consumed by PermissionPipeline (which uses isReadOnly()/ToolEffect
    instead).  Kept as declarative metadata for future wiring — see
    docs/MIGRATION_GAP_CLOSURE_EXECUTION_PLAN_2026-08-03.md.
    """
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolEffect(str, Enum):
    UNKNOWN = "unknown"
    READ_WORKSPACE = "read_workspace"
    WRITE_WORKSPACE = "write_workspace"
    DISCOVER_WORKSPACE = "discover_workspace"
    READ_VCS = "read_vcs"
    WRITE_VCS = "write_vcs"
    NETWORK = "network"
    READ_AGENT_STATE = "read_agent_state"
    WRITE_AGENT_STATE = "write_agent_state"
    PRODUCE_DELIVERABLE = "produce_deliverable"
    EXECUTE = "execute"
    TEST = "test"
    DELEGATE_READ_ONLY = "delegate_read_only"
    DELEGATE_WRITE = "delegate_write"


class PathAccess(str, Enum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    DISCOVER = "discover"
    DIFF = "diff"
    WORKSPACE_WIDE = "workspace_wide"


class ToolDependency(str, Enum):
    NONE = "none"
    ARTIFACT_STORE = "artifact_store"


class ToolRole(str, Enum):
    PERSIST_MEMORY = "persist_memory"
    DELEGATE = "delegate"


class ToolConcurrency(str, Enum):
    SERIAL = "serial"
    PARALLEL_SAFE = "parallel_safe"


class ModifierScope(str, Enum):
    """Skill modifier lifecycle scope.

    TURN: modifier applies only during the turn where the tool was called.
          Auto-deactivated when ToolExecutionPipeline.after_tool_use fires.
    RUN:  modifier applies for the entire agent run. Deactivated at
          PolicyAwareToolRegistry.deactivate_skill_modifier() end-of-run.
    """
    TURN = "turn"
    RUN = "run"


# Phase 1: tool source constants for unified evidence and namespace tracking.
# Single source of truth — no hardcoded strings in recorder or guard code.
_TOOL_SOURCE_SYSTEM = "system"
_TOOL_SOURCE_MCP = "mcp"
_TOOL_SOURCE_PROJECT = "project"


TOOL_SOURCE_PRIORITY: dict[str, int] = {
    _TOOL_SOURCE_SYSTEM: 3,
    _TOOL_SOURCE_PROJECT: 2,
    _TOOL_SOURCE_MCP: 1,
}
"""Tool source priority for namespace collision resolution. System > Project > MCP."""


class RetryMode(str, Enum):
    NEVER = "never"
    AUTOMATIC = "automatic"
    APPROVAL = "approval"


class IdempotencyStrategy(str, Enum):
    NONE = "none"
    INVOCATION_KEY = "invocation_key"
    USER_ACKNOWLEDGED = "user_acknowledged"


@dataclass(frozen=True)
class RetryPolicy:
    """Declarative retry contract for one logical tool invocation."""

    mode: RetryMode = RetryMode.NEVER
    max_attempts: int = 1
    base_delay_ms: int = 250
    max_delay_ms: int = 4000
    retryable_error_types: frozenset[str] = frozenset({
        "timeout", "unavailable", "environment_unavailable",
    })
    idempotency_strategy: IdempotencyStrategy = IdempotencyStrategy.NONE

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", RetryMode(self.mode))
        object.__setattr__(
            self, "idempotency_strategy",
            IdempotencyStrategy(self.idempotency_strategy),
        )
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_ms < 0 or self.max_delay_ms < self.base_delay_ms:
            raise ValueError("retry delays must be non-negative and ordered")


@dataclass(frozen=True)
class ToolMetadata:
    effects: frozenset[ToolEffect] = frozenset({ToolEffect.UNKNOWN})
    path_access: PathAccess = PathAccess.NONE
    path_parameter: str = ""
    dependency: ToolDependency = ToolDependency.NONE
    roles: frozenset[ToolRole] = frozenset()
    required_permissions: frozenset[str] = frozenset()
    """Declarative application permissions required by this tool call."""
    requires_user_interaction: bool = False
    """CC-aligned: when True, this tool ALWAYS prompts for user confirmation,
    even in bypassPermissions mode or when an allow rule matches.
    Equivalent to MCP _meta['anthropic/requiresUserInteraction'].
    """
    retry_policy: RetryPolicy | None = None
    """Explicit override.  None derives a safe policy from call effects."""
    source: str = "system"
    """Tool provenance: "system" (native/builtin), "project" (skill), "mcp" (MCP transport).
    Phase 3: used for namespace collision resolution priority."""
