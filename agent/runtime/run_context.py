"""Typed, per-run resource facts shared with Runtime-managed tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING

from agent.task import TerminationReason
from agent.v2.execution_budget import ExecutionBudget
from context.history import ConversationSnapshot
from llm.base import LLMMessage, LLMToolSchema

if TYPE_CHECKING:
    from core.policy import PhasePolicy
    from core.base import ToolEffect


class CancellationState(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ToolSchemaSnapshot:
    """Immutable copy of one tool contract visible to the parent model."""

    name: str
    description: str
    parameters_json: str

    @classmethod
    def capture(cls, schema: LLMToolSchema) -> "ToolSchemaSnapshot":
        return cls(
            name=schema.name,
            description=schema.description,
            parameters_json=json.dumps(
                schema.parameters,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )

    def materialize(self) -> LLMToolSchema:
        return LLMToolSchema(
            name=self.name,
            description=self.description,
            parameters=json.loads(self.parameters_json),
        )


@dataclass(frozen=True)
class AgentSpawnContext:
    """Runtime facts at the exact model-input boundary that requested a child."""

    conversation: ConversationSnapshot
    parent_session_id: str
    parent_agent_name: str
    repo_path: str
    model_name: str
    tool_schemas: tuple[ToolSchemaSnapshot, ...]

    def __post_init__(self) -> None:
        if not self.parent_session_id:
            raise ValueError("parent_session_id is required")
        if not self.parent_agent_name:
            raise ValueError("parent_agent_name is required")
        if not self.model_name:
            raise ValueError("model_name is required")
        resolved_repo = str(Path(self.repo_path).resolve())
        if not Path(resolved_repo).is_absolute():
            raise ValueError("repo_path must resolve to an absolute path")
        object.__setattr__(self, "repo_path", resolved_repo)
        names = [schema.name for schema in self.tool_schemas]
        if len(names) != len(set(names)):
            raise ValueError("tool schema names must be unique")

    @classmethod
    def capture(
        cls,
        *,
        messages: list[LLMMessage],
        parent_session_id: str,
        parent_agent_name: str,
        repo_path: str,
        model_name: str,
        tool_schemas: list[LLMToolSchema],
    ) -> "AgentSpawnContext":
        return cls(
            conversation=ConversationSnapshot.capture(messages),
            parent_session_id=parent_session_id,
            parent_agent_name=parent_agent_name,
            repo_path=repo_path,
            model_name=model_name,
            tool_schemas=tuple(
                ToolSchemaSnapshot.capture(schema) for schema in tool_schemas
            ),
        )


@dataclass
class CancellationToken:
    """Thread-safe hierarchical cancellation fact for one run-tree node."""

    _parent: "CancellationToken | None" = field(default=None, repr=False)
    _event: Event = field(default_factory=Event, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _reason: TerminationReason = field(
        default=TerminationReason.NONE, init=False, repr=False,
    )
    _detail: str = field(default="", init=False, repr=False)

    @property
    def state(self) -> CancellationState:
        return (
            CancellationState.CANCELLED
            if self._event.is_set()
            or (self._parent is not None and self._parent.is_cancelled)
            else CancellationState.ACTIVE
        )

    @property
    def is_cancelled(self) -> bool:
        return self.state is CancellationState.CANCELLED

    @property
    def reason(self) -> TerminationReason:
        if self._event.is_set() or self._parent is None:
            return self._reason
        return self._parent.reason

    @property
    def detail(self) -> str:
        if self._event.is_set() or self._parent is None:
            return self._detail
        return self._parent.detail

    def child(self) -> "CancellationToken":
        """Create an independently cancellable token inheriting this token."""
        return CancellationToken(_parent=self)

    def cancel(
        self,
        reason: TerminationReason = TerminationReason.USER_CANCELLED,
        detail: str = "",
    ) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = TerminationReason(reason)
            self._detail = detail or self._reason.value
            self._event.set()


@dataclass(frozen=True)
class RunContext:
    """Runtime-owned resources visible to tools for the current run only."""

    budget: ExecutionBudget
    cancellation: CancellationToken
    delegation_width: int = 1
    delegation_step_limit: int | None = None
    phase_policy: "PhasePolicy | None" = None
    delegation_effects: "frozenset[ToolEffect] | None" = None
    spawn_context: AgentSpawnContext | None = None

    def __post_init__(self) -> None:
        if self.delegation_width < 1:
            raise ValueError("delegation_width must be positive")
        if self.delegation_step_limit is not None and self.delegation_step_limit < 1:
            raise ValueError("delegation_step_limit must be positive when provided")

    @property
    def delegation_token_limit(self) -> int:
        """Maximum child spend derived from the parent's remaining budget."""
        return self.budget.token_remaining // self.delegation_width
