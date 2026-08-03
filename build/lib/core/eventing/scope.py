"""
P1: ScopeKind + ScopeToken — session/task/global isolation.

Invariants enforced at construction:
- GLOBAL: session_id/task_id MUST be None
- SESSION: session_id MUST exist, task_id MUST be None
- TASK: session_id AND task_id MUST exist
- generation >= 0 always
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from core.eventing.identifiers import SessionId, TaskId


class ScopeKind(StrEnum):
    GLOBAL = "global"
    SESSION = "session"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class ScopeToken:
    kind: ScopeKind
    global_id: uuid.UUID  # identifies the server process
    generation: int       # rejects stale events from old sessions
    session_id: SessionId | None = None
    task_id: TaskId | None = None

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError(f"generation must be >= 0, got {self.generation}")

        if self.kind is ScopeKind.GLOBAL:
            if self.session_id is not None:
                raise ValueError("GLOBAL scope must not have session_id")
            if self.task_id is not None:
                raise ValueError("GLOBAL scope must not have task_id")

        elif self.kind is ScopeKind.SESSION:
            if self.session_id is None:
                raise ValueError("SESSION scope must have session_id")
            if self.task_id is not None:
                raise ValueError("SESSION scope must not have task_id")

        elif self.kind is ScopeKind.TASK:
            if self.session_id is None:
                raise ValueError("TASK scope must have session_id")
            if self.task_id is None:
                raise ValueError("TASK scope must have task_id")

    @classmethod
    def global_scope(cls, generation: int = 0) -> ScopeToken:
        return cls(
            kind=ScopeKind.GLOBAL,
            global_id=uuid.uuid4(),
            generation=generation,
        )

    @classmethod
    def session_scope(
        cls, global_id: uuid.UUID, session_id: SessionId, generation: int = 0,
    ) -> ScopeToken:
        return cls(
            kind=ScopeKind.SESSION,
            global_id=global_id,
            generation=generation,
            session_id=session_id,
        )

    @classmethod
    def task_scope(
        cls, global_id: uuid.UUID, session_id: SessionId,
        task_id: TaskId, generation: int = 0,
    ) -> ScopeToken:
        return cls(
            kind=ScopeKind.TASK,
            global_id=global_id,
            generation=generation,
            session_id=session_id,
            task_id=task_id,
        )

    def is_stale(self, current_generation: int) -> bool:
        """True if this token belongs to an old generation."""
        return self.generation < current_generation
