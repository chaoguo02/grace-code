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
from typing import Any, Callable

from agent.task import (
    RunStatus,
    TaskIntent,
    TerminationReason,
    VerificationReason,
    VerificationStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GuardContext & GuardResult — unified guard evaluation types
# ---------------------------------------------------------------------------

@dataclass
class GuardContext:
    """All context a guard function might need to evaluate.

    Guards are stateless functions (GuardContext) → GuardResult.
    The TaskStateMachine owns the guard registry and evaluates guards
    before allowing transitions.
    """
    # Core runtime state
    step: int = 0
    max_steps: int = 40
    consecutive_failures: int = 0
    task_intent: TaskIntent = TaskIntent.EDIT
    verification_ok: bool = False
    had_any_write: bool = False
    had_any_read: bool = False

    # TSM reference (for guards that read TSM internal state)
    tsm: Any = None  # TaskStateMachine

    # Infra objects (passed by main loop)
    budget: Any = None           # ExecutionBudget
    breaker: Any = None          # CircuitBreaker
    completion_ctx: Any = None   # CompletionContext
    completion_policy: Any = None  # CompletionPolicy
    git_state: Any = None        # GitState
    event_log: Any = None        # EventLog

    # Context window
    context_size: int = 0
    request_budget: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.task_intent, TaskIntent):
            self.task_intent = TaskIntent(self.task_intent)


@dataclass
class GuardResult:
    """Result of a single guard evaluation.

    passed=True  → guard allows the transition
    passed=False → guard blocks; inject_message is added to conversation
    terminate=True → immediate termination, don't just block
    """
    passed: bool = True
    reason: str = ""
    inject_message: str = ""
    terminate: bool = False  # True → force_terminate, don't just block


# Guard function signature: (GuardContext) → GuardResult
GuardFn = Callable[[GuardContext], GuardResult]


# ---------------------------------------------------------------------------
# Built-in guard functions
# ---------------------------------------------------------------------------

def git_diff_guard(ctx: GuardContext) -> GuardResult:
    """Gate completion: require workspace-delta evidence for edit tasks.

    Without git diff evidence of changes, the state machine MUST NOT enter
    a verified completion outcome. Analysis tasks bypass this gate.
    """
    if ctx.task_intent is not TaskIntent.EDIT:
        return GuardResult(passed=True)

    git_state = ctx.git_state
    if git_state is None or not git_state.is_git_repo:
        return GuardResult(passed=True, reason="not a git repo — skipping diff gate")

    if not git_state.has_changes:
        return GuardResult(
            passed=False,
            reason="No git diff evidence of changes",
            inject_message=(
                "[RUNTIME] Cannot verify completion: no file changes detected "
                "via git diff. Your edits may not have persisted. "
                "Read the modified files to confirm your changes, then call finish again."
            ),
        )

    return GuardResult(passed=True)


def env_blocked_guard(ctx: GuardContext) -> GuardResult:
    """Detect environment-level failures while RUNNING.

    When the Runtime intercepts ModuleNotFoundError, missing commands,
    or permission errors, it terminates with a typed environment reason.
    """
    # This guard is evaluated when ToolErrorType.ENVIRONMENT_UNAVAILABLE is emitted.
    # is detected. It always passes in normal flow — the actual interception
    # happens in _run_body().
    return GuardResult(passed=True)


def circuit_breaker_guard(ctx: GuardContext) -> GuardResult:
    """Gate RUNNING→FAILED: check if the circuit breaker has tripped.

    The CircuitBreaker watches denial/error rhythm at the code level.
    When it trips, the task MUST terminate — the model gets no say.
    """
    breaker = ctx.breaker
    if breaker is not None and breaker.check():
        return GuardResult(
            passed=False,
            reason=f"Circuit breaker tripped: {breaker.trip_reason}",
            terminate=True,
        )
    return GuardResult(passed=True)


def budget_exhausted_guard(ctx: GuardContext) -> GuardResult:
    """Gate RUNNING→FAILED: check if execution budget is exhausted.

    When the budget is exhausted, the model gets one final turn with
    tools stripped. On the next iteration this guard terminates.
    """
    budget = ctx.budget
    if budget is not None and budget.is_exhausted:
        return GuardResult(
            passed=False,
            reason="Execution budget exhausted",
            inject_message=budget.force_finish_message(),
            terminate=True,
        )
    return GuardResult(passed=True)


def consecutive_failures_guard(ctx: GuardContext) -> GuardResult:
    """Gate RUNNING→FAILED: too many consecutive tool failures.

    Reads directly from CircuitBreaker._consecutive_tool_errors —
    the single source of truth for failure counting.
    """
    breaker = ctx.breaker
    if breaker is not None:
        max_failures = getattr(breaker.config, "max_consecutive_tool_errors", 3)
        failures = getattr(breaker, "_consecutive_tool_errors", 0)
        if failures >= max_failures:
            return GuardResult(
                passed=False,
                reason=f"{failures} consecutive tool failures (limit: {max_failures})",
                terminate=True,
            )
    # Fallback: check GuardContext field
    if ctx.consecutive_failures >= 3:
        return GuardResult(
            passed=False,
            reason=f"{ctx.consecutive_failures} consecutive failures",
            terminate=True,
        )
    return GuardResult(passed=True)


def stop_hook_retry_guard(ctx: GuardContext) -> GuardResult:
    """Gate COMPLETING→FAILED: stop hook retries exceeded.

    When the stop hook blocks COMPLETING more than _MAX_STOP_HOOK_RETRIES
    times, the task should fail rather than loop indefinitely.
    """
    # This guard is triggered by the main loop when _stop_hook_count > threshold.
    # Default: always passes — the actual check is in _run_body().
    return GuardResult(passed=True)


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
    TaskState.COMPLETING: frozenset({TaskState.COMPLETED, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.COMPLETED:  frozenset(),
    TaskState.FAILED:     frozenset(),
    TaskState.CANCELLED:  frozenset(),
}

_TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED,
})


class GuardTransition(str, Enum):
    """Typed guard registration points; no transition-name string parsing."""

    RUNNING_TO_FAILED = "running_to_failed"
    RUNNING_TO_RUNNING = "running_to_running"
    COMPLETING_TO_COMPLETED = "completing_to_completed"
    COMPLETING_TO_FAILED = "completing_to_failed"
    COMPLETING_TO_RUNNING = "completing_to_running"


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

    # Structured diagnostic detail; never used to select a state transition.
    block_detail: dict | None = None
    termination_reason: TerminationReason = TerminationReason.NONE
    verification_status: VerificationStatus = VerificationStatus.NOT_APPLICABLE
    verification_reason: VerificationReason = VerificationReason.NONE
    outcome_detail: str = ""

    # Timing
    _started_at: float = 0.0
    _completed_at: float = 0.0
    _step_count: int = 0

    # Hooks
    _on_transition: Callable[[TaskState, TaskState, str], None] | None = None

    # ── Guard registry ──
    _guards: dict[GuardTransition, list[GuardFn]] = field(default_factory=dict)

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

    def complete(
        self,
        verification_status: VerificationStatus,
        verification_reason: VerificationReason = VerificationReason.NONE,
        detail: str = "",
    ) -> None:
        """Complete from COMPLETING with an orthogonal verification outcome."""
        self.transition(TaskState.COMPLETED, detail)
        self.verification_status = verification_status
        self.verification_reason = verification_reason
        self.outcome_detail = detail

    def fail(self, reason: TerminationReason, detail: str = "") -> None:
        """Fail through the validated state graph; Runtime cannot bypass it."""
        self.transition(TaskState.FAILED, detail or reason.value)
        self.termination_reason = reason
        self.outcome_detail = detail

    def cancel(self, detail: str = "") -> None:
        self.transition(TaskState.CANCELLED, detail or TerminationReason.USER_CANCELLED.value)
        self.termination_reason = TerminationReason.USER_CANCELLED
        self.outcome_detail = detail

    # ── Guard registry methods ──

    def add_guard(self, transition: GuardTransition, guard_fn: GuardFn) -> None:
        """Register a guard function for a specific transition.

        ``transition`` is a GuardTransition enum; arbitrary names are rejected.
        """
        if not isinstance(transition, GuardTransition):
            raise TypeError("transition must be a GuardTransition")
        if transition not in self._guards:
            self._guards[transition] = []
        self._guards[transition].append(guard_fn)
        logger.debug(
            "TaskStateMachine [%s]: registered guard for %s (%d total)",
            self.task_id, transition.value, len(self._guards[transition]),
        )

    def evaluate_guards(
        self, transition: GuardTransition, context: GuardContext
    ) -> GuardResult:
        """Evaluate all guards for a transition. Returns first failure, or success.

        Guards are evaluated in registration order. The first guard that returns
        passed=False short-circuits — remaining guards are not evaluated.

        Returns GuardResult(passed=True) if no guards are registered for this key.
        """
        guards = self._guards.get(transition, [])
        if not guards:
            return GuardResult(passed=True)

        for i, guard_fn in enumerate(guards):
            try:
                result = guard_fn(context)
            except Exception:
                logger.error(
                    "TaskStateMachine [%s]: guard %d for %s raised exception",
                    self.task_id, i, transition.value, exc_info=True,
                )
                return GuardResult(
                    passed=False,
                    reason=f"Guard {i} for {transition.value} raised an internal error",
                    terminate=True,
                )
            if not result.passed:
                logger.info(
                    "TaskStateMachine [%s]: guard %d blocked %s: %s",
                    self.task_id, i, transition.value, result.reason,
                )
                return result

        return GuardResult(passed=True)

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
            "termination_reason": self.termination_reason.value,
            "verification_status": self.verification_status.value,
            "verification_reason": self.verification_reason.value,
        }

    # ── Map to RunStatus ──

    def to_run_status(self, error: str = "") -> RunStatus:
        """Convert terminal state to RunStatus for existing interfaces."""
        mapping = {
            TaskState.COMPLETED: RunStatus.SUCCESS,
            TaskState.FAILED: RunStatus.FAILED,
            TaskState.CANCELLED: RunStatus.CANCELLED,
        }
        if (
            self._state == TaskState.FAILED
            and self.termination_reason == TerminationReason.ENVIRONMENT_UNAVAILABLE
        ):
            return RunStatus.BLOCKED
        if (
            self._state == TaskState.FAILED
            and self.termination_reason == TerminationReason.MAX_STEPS
        ):
            return RunStatus.MAX_STEPS
        # Non-terminal → treat as in-progress
        return mapping.get(self._state, RunStatus.SUCCESS)
