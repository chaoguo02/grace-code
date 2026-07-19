"""core/types.py

Core data types — extracted from core/base.py for better cohesion.
core/base.py re-exports all symbols for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# ObservationStatus, ToolOutcome, Observation
# ---------------------------------------------------------------------------

class ObservationStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


class ToolOutcome(str, Enum):
    NONE = "none"
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

    def to_dict(self) -> dict[str, Any]:
        return {k: v.value if isinstance(v, Enum) else v
                for k, v in self.__dict__.items()}

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

@dataclass
class LLMToolSchema:
    """向 LLM 描述一个可用工具的 schema。"""
    name: str
    description: str
    parameters: dict[str, Any]


# ---------------------------------------------------------------------------
# Tool metadata enums
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
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
    EVIDENCE_LEDGER = "evidence_ledger"


class ToolRole(str, Enum):
    PERSIST_MEMORY = "persist_memory"
    DELEGATE = "delegate"


class ToolConcurrency(str, Enum):
    SERIAL = "serial"
    PARALLEL_SAFE = "parallel_safe"


@dataclass(frozen=True)
class ToolMetadata:
    effects: frozenset[ToolEffect] = frozenset({ToolEffect.UNKNOWN})
    path_access: PathAccess = PathAccess.NONE
    path_parameter: str = ""
    dependency: ToolDependency = ToolDependency.NONE
    roles: frozenset[ToolRole] = frozenset()
    requires_user_interaction: bool = False
    """CC-aligned: when True, this tool ALWAYS prompts for user confirmation,
    even in bypassPermissions mode or when an allow rule matches.
    Equivalent to MCP _meta['anthropic/requiresUserInteraction']."""
