"""Explicit Task State Machine — Runtime enforces transitions, not the model.

Claude Code pattern (query.ts): tool execution finishing does NOT mean the task
is done. The system checks post-conditions after every iteration. Only when ALL
conditions are met is the task truly complete.

This module provides an explicit state machine with guarded transitions.
The model CANNOT unilaterally declare "done" via natural language — only the
Runtime can advance the state based on validated conditions.

States:
    PENDING     — task created, not yet started
    RUNNING     — main loop executing, tools available
    COMPLETING  — model called FINISH, Runtime validating completion guards
    COMPLETED   — all guards passed, task successful
    FAILED      — unrecoverable error, circuit breaker tripped, or model gave up
    CANCELLED   — externally cancelled (user interrupt, timeout)

Transitions (only these are legal):
    PENDING    → RUNNING     (start)
    RUNNING    → COMPLETING  (model calls FINISH)
    RUNNING    → FAILED      (circuit breaker trips, LLM error, GAVE_UP)
    RUNNING    → CANCELLED   (external signal)
    COMPLETING → COMPLETED   (all guards pass)
    COMPLETING → RUNNING     (guard blocks — re-enter loop with injection)
    COMPLETING → FAILED      (stop hook retry limit exceeded)
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from agent.task import RunStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TaskState enum
# ---------------------------------------------------------------------------

class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Allowed transitions: current -> {next states}
_ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING:    frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING:    frozenset({TaskState.COMPLETING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.COMPLETING: frozenset({TaskState.COMPLETED, TaskState.RUNNING, TaskState.FAILED}),
    TaskState.COMPLETED:  frozenset(),   # terminal
    TaskState.FAILED:     frozenset(),   # terminal
    TaskState.CANCELLED:  frozenset(),   # terminal
}

_TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED,
})


# ---------------------------------------------------------------------------
# TaskStateMachine
# ---------------------------------------------------------------------------

@dataclass
class TaskStateMachine:
    """Explicit state machine for task lifecycle.

    Usage in the main loop:
        tsm = TaskStateMachine(task_id="abc123")
        tsm.transition(TaskState.RUNNING)  # start

        for step in range(max_steps):
            tsm.record_step()
            ...  # execute step

            if model_called_finish:
                tsm.transition(TaskState.COMPLETING)
                if guard_result.can_complete:
                    tsm.transition(TaskState.COMPLETED)
                    return success
                else:
                    tsm.transition(TaskState.RUNNING)  # back to loop
                    continue
    """

    task_id: str
    _state: TaskState = TaskState.PENDING
    _state_history: list[tuple[TaskState, float, str]] = field(default_factory=list)
    """(state, timestamp, reason) tuples for audit trail."""

    # Timing
    _started_at: float = 0.0
    _completed_at: float = 0.0
    _step_count: int = 0

    # Hooks
    _on_transition: Callable[[TaskState, TaskState, str], None] | None = None

    # ── Properties ──

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in _TERMINAL_STATES

    @property
    def is_running(self) -> bool:
        return self._state == TaskState.RUNNING

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at == 0:
            return 0.0
        end = self._completed_at if self._completed_at > 0 else _time.time()
        return end - self._started_at

    @property
    def step_count(self) -> int:
        return self._step_count

    @property
    def history(self) -> list[tuple[TaskState, float, str]]:
        return list(self._state_history)

    # ── Transition ──

    def transition(self, to: TaskState, reason: str = "") -> None:
        """Attempt a state transition. Raises ValueError if illegal."""
        allowed = _ALLOWED_TRANSITIONS.get(self._state, frozenset())
        if to not in allowed:
            raise ValueError(
                f"Illegal state transition: {self._state.value} → {to.value}. "
                f"Allowed: {[s.value for s in sorted(allowed)]}"
            )

        previous = self._state
        self._state = to
        now = _time.time()
        self._state_history.append((to, now, reason))

        if previous == TaskState.PENDING and to == TaskState.RUNNING:
            self._started_at = now
        if to in _TERMINAL_STATES:
            self._completed_at = now

        logger.debug(
            "TaskStateMachine [%s]: %s → %s (%s)",
            self.task_id, previous.value, to.value, reason or "no reason given",
        )

        if self._on_transition is not None:
            try:
                self._on_transition(previous, to, reason)
            except Exception:
                logger.debug("on_transition hook failed", exc_info=True)

    # ── Step tracking ──

    def record_step(self) -> int:
        """Record a main loop iteration. Returns the new step count."""
        if self._state != TaskState.RUNNING:
            logger.warning(
                "TaskStateMachine [%s]: record_step() called in state %s",
                self.task_id, self._state.value,
            )
        self._step_count += 1
        return self._step_count

    # ── Serialization ──

    def to_summary(self) -> dict:
        """Export state machine status for diagnostics."""
        return {
            "task_id": self.task_id,
            "state": self._state.value,
            "step_count": self._step_count,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "is_terminal": self.is_terminal,
            "transition_count": len(self._state_history),
        }

    # ── Map to RunStatus ──

    def to_run_status(self, error: str = "") -> RunStatus:
        """Convert terminal state to RunStatus for existing interfaces."""
        mapping = {
            TaskState.COMPLETED: RunStatus.SUCCESS,
            TaskState.FAILED: RunStatus.FAILED,
            TaskState.CANCELLED: RunStatus.GAVE_UP,
        }
        # Non-terminal → treat as in-progress
        return mapping.get(self._state, RunStatus.SUCCESS)
