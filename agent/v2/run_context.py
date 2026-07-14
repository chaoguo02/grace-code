"""Typed, per-run resource facts shared with Runtime-managed tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import Event, Lock
from typing import TYPE_CHECKING

from agent.task import TerminationReason
from agent.v2.execution_budget import ExecutionBudget

if TYPE_CHECKING:
    from agent.policy import PhasePolicy
    from tools.base import ToolEffect


class CancellationState(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


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

    def __post_init__(self) -> None:
        if self.delegation_width < 1:
            raise ValueError("delegation_width must be positive")
        if self.delegation_step_limit is not None and self.delegation_step_limit < 1:
            raise ValueError("delegation_step_limit must be positive when provided")

    @property
    def delegation_token_limit(self) -> int:
        """Maximum child spend derived from the parent's remaining budget."""
        return self.budget.token_remaining // self.delegation_width
