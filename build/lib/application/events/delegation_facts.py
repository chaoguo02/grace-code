"""
P3: Delegation/child-task facts — independent payload classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.eventing.identifiers import RunId, TaskId


class DelegationStatus(StrEnum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ChildTaskStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DelegationCreatedV1:
    delegation_id: str
    parent_run_id: RunId
    topology: str = ""         # "single" | "fan_out" | "chain"
    task_count: int = 0

    def __post_init__(self) -> None:
        if self.task_count < 0:
            raise ValueError(f"task_count >= 0, got {self.task_count}")


@dataclass(frozen=True, slots=True)
class DelegationCompletedV1:
    delegation_id: str
    parent_run_id: RunId
    status: str = DelegationStatus.COMPLETED.value
    successful_tasks: int = 0
    failed_tasks: int = 0


@dataclass(frozen=True, slots=True)
class ChildTaskStartedV1:
    task_id: TaskId
    delegation_id: str
    parent_run_id: RunId
    child_session_id: str = ""
    agent_type: str = ""


@dataclass(frozen=True, slots=True)
class ChildTaskCompletedV1:
    task_id: TaskId
    delegation_id: str
    parent_run_id: RunId
    child_session_id: str = ""
    status: str = ChildTaskStatus.COMPLETED.value
    steps_taken: int = 0
    tokens_used: int = 0
