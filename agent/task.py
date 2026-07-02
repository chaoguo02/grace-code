"""
Core task and runtime data models.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    TASK_START = "task_start"
    ACTION = "action"
    OBSERVATION = "observation"
    REFLECTION = "reflection"
    PHASE_START = "phase_start"
    PHASE_END = "phase_end"
    TOOL_DECISION = "tool_decision"
    RECOVERY_ACTION = "recovery_action"
    CLAIM_CREATED = "claim_created"
    ANALYSIS_PHASE = "analysis_phase"
    EVIDENCE_RECORD = "evidence_record"
    PLAN_GENERATED = "plan_generated"
    REPLAN_GENERATED = "replan_generated"
    DAG_GRAPH = "dag_graph"
    SUBTASK_START = "subtask_start"
    SUBTASK_COMPLETE = "subtask_complete"
    SUBTASK_FAILED = "subtask_failed"
    SUBTASK_SKIPPED = "subtask_skipped"
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"


class ActionType(str, Enum):
    TOOL_CALL = "tool_call"
    REFLECTION = "reflection"
    FINISH = "finish"
    GIVE_UP = "give_up"


class ObservationStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


class RunStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    MAX_STEPS = "max_steps"
    GAVE_UP = "gave_up"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class TaskShape:
    kind: str = "implementation"
    explicit_paths: frozenset[str] = field(default_factory=frozenset)
    requires_plan: bool = False
    requires_read_plan: bool = False
    confidence: float = 0.0
    reason: str = ""


@dataclass
class Task:
    description: str
    repo_path: str
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    intent: str = "edit"
    issue_url: str | None = None
    test_cmd: str | None = None
    max_steps: int = 40
    budget_tokens: int = 80_000
    metadata: dict[str, Any] = field(default_factory=dict)
    explicit_read_paths: frozenset[str] | None = None
    explicit_write_paths: frozenset[str] | None = None
    shape: TaskShape | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("explicit_read_paths", "explicit_write_paths"):
            value = payload.get(key)
            if isinstance(value, frozenset):
                payload[key] = sorted(value)
        shape = payload.get("shape")
        if isinstance(shape, dict):
            explicit_paths = shape.get("explicit_paths")
            if isinstance(explicit_paths, (list, tuple, set, frozenset)):
                shape["explicit_paths"] = sorted(explicit_paths)
        return payload

    def __repr__(self) -> str:
        return f"Task(id={self.task_id!r}, desc={self.description[:60]!r})"


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


@dataclass
class Observation:
    status: ObservationStatus
    output: str
    tool_name: str
    tokens_used: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_success(self) -> bool:
        return self.status == ObservationStatus.SUCCESS

    def is_expected_block(self) -> bool:
        return bool(self.metadata.get("expected_block"))

    def __repr__(self) -> str:
        return (
            f"Observation(tool={self.tool_name}, "
            f"status={self.status.value}, "
            f"len={len(self.output)})"
        )


@dataclass
class Event:
    event_type: EventType
    task_id: str
    payload: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }


@dataclass
class RunResult:
    task_id: str
    status: RunStatus
    summary: str
    steps_taken: int
    total_tokens: int = 0
    patch: str | None = None
    error: str | None = None
    cache_stats: Any = None

    def is_success(self) -> bool:
        return self.status == RunStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return (
            f"RunResult(status={self.status.value}, "
            f"steps={self.steps_taken}, "
            f"tokens={self.total_tokens})"
        )
