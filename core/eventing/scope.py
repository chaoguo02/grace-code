"""
G4: ScopeToken — frozen identity, full-field equality, no implicit reopen.

Identity = (kind, global_id, session_id, task_id, generation).
Two tokens are equal iff ALL five fields match — same session at different
generations are DIFFERENT scopes.  This prevents stale events from being
routed to the wrong generation.
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
    """Immutable scope identifier.  Full identity: all five fields.

    Equality requires exact match on (kind, global_id, session_id,
    task_id, generation).  A SESSION scope at generation 1 is NOT
    equal to the same session at generation 2 — they are different scopes.
    """

    kind: ScopeKind
    global_id: uuid.UUID
    generation: int
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

    # ── Identity ─────────────────────────────────────────────────────────

    @property
    def identity(self) -> tuple[str, str, str | None, str | None, int]:
        """Canonical identity tuple for hash/equality and routing."""
        return (
            self.kind.value,
            str(self.global_id),
            str(self.session_id) if self.session_id else None,
            str(self.task_id) if self.task_id else None,
            self.generation,
        )

    def __hash__(self) -> int:
        return hash(self.identity)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ScopeToken):
            return NotImplemented
        return self.identity == other.identity

    # ── Factory helpers ──────────────────────────────────────────────────

    @classmethod
    def global_scope(cls, global_id: uuid.UUID | None = None,
                     generation: int = 0) -> ScopeToken:
        return cls(
            kind=ScopeKind.GLOBAL,
            global_id=global_id or uuid.uuid4(),
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

    # ── Helpers ──────────────────────────────────────────────────────────

    @property
    def scope_key(self) -> str:
        """Routing key (kind + session_id + task_id, no generation)."""
        if self.kind == ScopeKind.GLOBAL:
            return "global"
        if self.kind == ScopeKind.SESSION:
            return f"session:{self.session_id}"
        return f"task:{self.session_id}:{self.task_id}"

    @property
    def parent_key(self) -> str | None:
        """Key of the parent scope (for tree navigation)."""
        if self.kind == ScopeKind.SESSION:
            return "global"
        if self.kind == ScopeKind.TASK:
            return f"session:{self.session_id}"
        return None

    def is_stale(self, current_generation: int) -> bool:
        """True if this token belongs to an old generation."""
        return self.generation < current_generation

    def __repr__(self) -> str:
        return (
            f"ScopeToken({self.kind.value}, "
            f"gen={self.generation}, "
            f"sid={self.session_id}, tid={self.task_id})"
        )
