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

from agent.task import RunStatus

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
    task_intent: str = "edit"
    verification_ok: bool = False
    had_any_write: bool = False
    had_any_read: bool = False

    # TSM reference (for guards that read TSM internal state)
    tsm: Any = None  # TaskStateMachine

    # Infra objects (passed by main loop)
    budget: Any = None           # ExecutionBudget
    breaker: Any = None          # CircuitBreaker
    loop_detector: Any = None    # MacroLoopDetector
    completion_ctx: Any = None   # CompletionContext
    completion_policy: Any = None  # CompletionPolicy
    git_state: Any = None        # GitState
    event_log: Any = None        # EventLog

    # Context window
    context_size: int = 0
    request_budget: int = 0


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
# MacroActionRecord — for loop detection (moved from MacroLoopDetector)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MacroActionRecord:
    """A single macro-level action for loop pattern analysis."""
    action_type: str   # one of: read_file, search_code, write_file, run_shell, validate, spawn_subagent, finish, other
    tool_name: str = ""
    detail: str = ""   # file path, subagent name, command prefix, etc.

    def signature(self) -> str:
        """Compact fingerprint for pattern matching."""
        if self.detail:
            return f"{self.action_type}:{self.tool_name}:{self.detail}"
        return f"{self.action_type}:{self.tool_name}"


# Tool name → macro action type mapping (moved from MacroLoopDetector)
_TOOL_TO_MACRO: dict[str, str] = {
    "task": "spawn_subagent",
    "file_read": "read_file",
    "file_view": "read_file",
    "search_text": "search_code",
    "find_files": "search_code",
    "find_symbol": "search_code",
    "file_write": "write_file",
    "file_edit": "write_file",
    "edit": "write_file",
    "bash": "run_shell",
    "shell": "run_shell",
    "zsh": "run_shell",
    "test": "validate",
    "pytest": "validate",
    "finish": "finish",
    "task_complete": "finish",
}


# ---------------------------------------------------------------------------
# Built-in guard functions
# ---------------------------------------------------------------------------

def git_diff_guard(ctx: GuardContext) -> GuardResult:
    """Gate COMPLETING→COMPLETED_VERIFIED: require git diff evidence for edit tasks.

    Without git diff evidence of changes, the state machine MUST NOT enter
    COMPLETED_VERIFIED. Analysis tasks bypass this gate.
    """
    if ctx.task_intent != "edit":
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
    """Gate RUNNING→BLOCKED_BY_ENV: detect environment-level failures.

    When the Runtime intercepts ModuleNotFoundError, missing commands,
    or permission errors, this guard forces BLOCKED_BY_ENV — a terminal
    state that requires user intervention.
    """
    # This guard is evaluated by the main loop when tool_error.is_environmental
    # is detected. It always passes in normal flow — the actual interception
    # happens in _run_body() via force_transition().
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


def self_critique_guard(ctx: GuardContext) -> GuardResult:
    """Gate COMPLETING→RUNNING: force the LLM to review its own output.

    Zero Trust principle: reflection MUST be a separate physical node, not
    embedded in the generation step. This guard forces the LLM to stop and
    re-examine its output for contradictions, unverified claims, and
    incomplete parts — BEFORE finish is accepted.
    """
    if ctx.task_intent != "analysis":
        return GuardResult(passed=True)

    # This guard always "passes" (doesn't block completion), but it injects
    # a critique prompt that FORCES the LLM to self-review. The LLM gets one
    # more turn to fix its output, then calls finish again.
    # On the second pass, self_critique_guard_count prevents infinite loops.
    tsm = ctx.tsm
    count = getattr(tsm, "_self_critique_count", 0)
    if count >= 2:
        return GuardResult(passed=True)  # already critiqued twice, let it through

    tsm._self_critique_count = count + 1
    return GuardResult(
        passed=True,
        inject_message=(
            "[SYSTEM] REFLECTING NODE — independent verification.\n"
            "IGNORE your previous reasoning. Review ONLY: task description, "
            "your output, and file evidence.\n"
            "Check: contradictions, unverified claims, incomplete parts, "
            "scope violations.\n"
            "Fix any issues and call finish again, or call finish with same output if clean."
        ),
    )


def evidence_validation_guard(ctx: GuardContext) -> GuardResult:
    """Gate COMPLETING→RUNNING: Runtime validates severity claims.

    The LLM fills in blanks (findings). The Runtime judges correctness.
    If a finding is marked HIGH without exploit evidence, the Runtime
    tells the LLM to downgrade it — not as advice, but as a factual correction.
    """
    if ctx.task_intent != "analysis":
        return GuardResult(passed=True)

    tsm = ctx.tsm
    count = getattr(tsm, "_evidence_check_count", 0)
    if count >= 1:
        return GuardResult(passed=True)  # already checked once

    tsm._evidence_check_count = count + 1
    return GuardResult(
        passed=True,
        inject_message=(
            "[RUNTIME VALIDATION] Review each HIGH-severity finding:\n"
            "- Does it cite a CONCRETE exploit path (exact command, URL, code line)?\n"
            "- Does it include reproduction steps that an attacker could follow?\n"
            "- If NO to either: the Runtime will mark it as MEDIUM (design risk, not confirmed vulnerability).\n"
            "Downgrade any finding that lacks exploit evidence, then call finish."
        ),
    )


def stop_hook_retry_guard(ctx: GuardContext) -> GuardResult:
    """Gate COMPLETING→FAILED: stop hook retries exceeded.

    When the stop hook blocks COMPLETING more than _MAX_STOP_HOOK_RETRIES
    times, the task should fail rather than loop indefinitely.
    """
    # This guard is triggered by the main loop when _stop_hook_count > threshold.
    # Default: always passes — the actual check is in _run_body().
    return GuardResult(passed=True)


def progress_guard(ctx: GuardContext) -> GuardResult:
    """Gate RUNNING→RUNNING: detect no-progress stalls.

    This guard reads TSM's internal _no_progress_count. It does NOT compute
    what "progress" means — that's handled by TSM._update_progress().
    The guard only checks: has the counter exceeded the limit?
    """
    tsm = ctx.tsm
    if tsm is None:
        return GuardResult(passed=True)

    if tsm._no_progress_count >= tsm._no_progress_limit:
        is_analysis = (
            tsm.task_intent is not None
            and getattr(tsm.task_intent, "is_analysis", False)
        )
        kind = "analysis" if is_analysis else "edit"
        return GuardResult(
            passed=False,
            terminate=True,
            reason=(
                f"No progress for {tsm._no_progress_count} macro actions "
                f"({kind} task). "
                f"Distinct files read: {len(tsm._distinct_files_read)}. "
                f"Distinct files written: {len(tsm._distinct_files_written)}."
            ),
        )
    return GuardResult(passed=True)


def loop_detect_guard(ctx: GuardContext) -> GuardResult:
    """Gate RUNNING→RUNNING: detect fingerprint loops.

    Only triggers when BOTH conditions are met:
    1. No progress for at least _loop_min_repetitions actions
    2. Last N fingerprints are all identical (consecutive repeat)
    """
    tsm = ctx.tsm
    if tsm is None:
        return GuardResult(passed=True)

    history = tsm._macro_action_history
    if len(history) < tsm._loop_min_repetitions:
        return GuardResult(passed=True)

    # Only trigger fingerprint check when there's also no progress
    if tsm._no_progress_count < tsm._loop_min_repetitions:
        return GuardResult(passed=True)

    recent = history[-tsm._macro_window_size:]
    fingerprints = [r.signature() for r in recent]
    last_n = fingerprints[-tsm._loop_min_repetitions:]
    if len(set(last_n)) == 1:
        return GuardResult(
            passed=False,
            terminate=True,
            reason=(
                f"Loop detected: [{last_n[0]}] "
                f"repeated {tsm._loop_min_repetitions}x consecutively "
                f"with no progress for {tsm._no_progress_count} actions."
            ),
        )
    return GuardResult(passed=True)


# ---------------------------------------------------------------------------
# TaskState enum
# ---------------------------------------------------------------------------

class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETING = "completing"
    REFLECTING = "reflecting"            # ← NEW: independent self-review node before completion
    COMPLETED = "completed"              # legacy — kept for backward compat
    COMPLETED_VERIFIED = "completed_verified"      # git diff + test evidence confirmed
    COMPLETED_UNVERIFIED = "completed_unverified"  # guards passed, no runtime verification
    # ── Legacy sub-states (deprecated — use failure_reason field instead) ──
    COMPLETED_UNVERIFIED_NO_ENV = "completed_unverified_no_env"
    COMPLETED_UNVERIFIED_FAILED = "completed_unverified_failed"
    # Failure reasons are now ORTHOGONAL: store in tsm.failure_reason, not state enum
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_BY_ENV = "blocked_by_env"    # env-level failure: missing deps, permissions, etc.


# Allowed transitions: current -> {next states}
_ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING:    frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING:    frozenset({TaskState.COMPLETING, TaskState.FAILED, TaskState.CANCELLED, TaskState.BLOCKED_BY_ENV}),
    TaskState.COMPLETING: frozenset({TaskState.REFLECTING, TaskState.COMPLETED, TaskState.COMPLETED_VERIFIED, TaskState.COMPLETED_UNVERIFIED, TaskState.COMPLETED_UNVERIFIED_NO_ENV, TaskState.COMPLETED_UNVERIFIED_FAILED, TaskState.RUNNING, TaskState.FAILED}),
    TaskState.REFLECTING: frozenset({TaskState.COMPLETED, TaskState.COMPLETED_VERIFIED, TaskState.COMPLETED_UNVERIFIED, TaskState.COMPLETED_UNVERIFIED_NO_ENV, TaskState.COMPLETED_UNVERIFIED_FAILED, TaskState.RUNNING, TaskState.FAILED}),
    TaskState.COMPLETED:                      frozenset(),   # terminal (legacy)
    TaskState.COMPLETED_VERIFIED:             frozenset(),   # terminal
    TaskState.COMPLETED_UNVERIFIED:           frozenset(),   # terminal (generic)
    TaskState.COMPLETED_UNVERIFIED_NO_ENV:    frozenset(),   # terminal
    TaskState.COMPLETED_UNVERIFIED_FAILED:    frozenset(),   # terminal
    TaskState.FAILED:                         frozenset(),   # terminal
    TaskState.CANCELLED:                      frozenset(),   # terminal
    TaskState.BLOCKED_BY_ENV:                 frozenset(),   # terminal
}

_TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.COMPLETED, TaskState.COMPLETED_VERIFIED,
    TaskState.COMPLETED_UNVERIFIED, TaskState.COMPLETED_UNVERIFIED_NO_ENV, TaskState.COMPLETED_UNVERIFIED_FAILED,
    TaskState.FAILED, TaskState.CANCELLED, TaskState.BLOCKED_BY_ENV,
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

    # Structured detail for BLOCKED_BY_ENV and FAILED states
    block_detail: dict | None = None
    """Structured error info for BLOCKED_BY_ENV."""

    # ── Orthogonal failure reason (NOT part of state enum) ──
    failure_reason: str = ""
    """Why the task ended unverified/failed. Values: 'no_env', 'test_failed',
    'loop_detected', 'budget_exhausted', 'circuit_breaker', 'max_steps'.
    Stored orthogonally to prevent state explosion."""

    # Timing
    _started_at: float = 0.0
    _completed_at: float = 0.0
    _step_count: int = 0

    # Hooks
    _on_transition: Callable[[TaskState, TaskState, str], None] | None = None

    # ── Guard registry ──
    _guards: dict[str, list[GuardFn]] = field(default_factory=dict)
    """Guards keyed by transition name, e.g. 'RUNNING->FAILED', 'COMPLETING->COMPLETED_VERIFIED'.
    Each transition can have multiple guards; all must pass for the transition to be allowed."""

    # ── Progress tracking (from MacroLoopDetector, now owned by TSM) ──
    _distinct_files_read: set[str] = field(default_factory=set)
    _distinct_files_written: set[str] = field(default_factory=set)
    _macro_action_history: list[MacroActionRecord] = field(default_factory=list)
    _no_progress_count: int = 0
    task_intent: Any = None  # TaskIntent, set by _run_body()
    # Configurable thresholds (overridable)
    _macro_window_size: int = 6
    _loop_min_repetitions: int = 2
    _no_progress_limit: int = 8  # 2 * 4, matching old MacroLoopDetectorConfig

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

    def force_transition(self, to: TaskState, reason: str = "") -> None:
        """Emergency transition that bypasses _ALLOWED_TRANSITIONS validation.

        Use this ONLY for Runtime-initiated state changes where the Runtime
        has determined (through code-level evidence, not model claims) that
        the state MUST change. Examples:
        - Circuit breaker trips mid-step
        - Environment error intercepted (→ BLOCKED_BY_ENV)
        - External cancellation signal

        Regular lifecycle transitions (PENDING→RUNNING, RUNNING→COMPLETING,
        COMPLETING→COMPLETED) should still use transition().
        """
        previous = self._state
        self._state = to
        now = _time.time()
        self._state_history.append((to, now, f"[FORCED] {reason}" if reason else "[FORCED]"))

        if previous == TaskState.PENDING and to == TaskState.RUNNING:
            self._started_at = now
        if to in _TERMINAL_STATES:
            self._completed_at = now

        logger.warning(
            "TaskStateMachine [%s]: FORCED %s → %s (%s)",
            self.task_id, previous.value, to.value, reason or "no reason given",
        )

        if self._on_transition is not None:
            try:
                self._on_transition(previous, to, reason)
            except Exception:
                logger.debug("on_transition hook failed", exc_info=True)

    def record_reflection(self, reason: str = "") -> None:
        """Record a reflection injection — breaks loop patterns. Call when the
        Runtime injects a reflection prompt (test_failed, no_edit, etc.)."""
        self._macro_action_history.clear()
        self._macro_action_history.append(MacroActionRecord(
            action_type="reflect", detail=reason[:80],
        ))
        self._no_progress_count = 0

    # ── Guard registry methods ──

    def add_guard(self, transition_key: str, guard_fn: GuardFn) -> None:
        """Register a guard function for a specific transition.

        transition_key format: 'SOURCE->TARGET', e.g.:
        - 'RUNNING->FAILED' — pre-step safety checks
        - 'COMPLETING->COMPLETED_VERIFIED' — diff + test evidence
        - 'COMPLETING->RUNNING' — completion blocked, back to loop

        All guards for a transition must pass before the transition is allowed.
        """
        if transition_key not in self._guards:
            self._guards[transition_key] = []
        self._guards[transition_key].append(guard_fn)
        logger.debug(
            "TaskStateMachine [%s]: registered guard for %s (%d total)",
            self.task_id, transition_key, len(self._guards[transition_key]),
        )

    def evaluate_guards(
        self, transition_key: str, context: GuardContext
    ) -> GuardResult:
        """Evaluate all guards for a transition. Returns first failure, or success.

        Guards are evaluated in registration order. The first guard that returns
        passed=False short-circuits — remaining guards are not evaluated.

        Returns GuardResult(passed=True) if no guards are registered for this key.
        """
        guards = self._guards.get(transition_key, [])
        if not guards:
            return GuardResult(passed=True)

        for i, guard_fn in enumerate(guards):
            try:
                result = guard_fn(context)
            except Exception:
                logger.error(
                    "TaskStateMachine [%s]: guard %d for %s raised exception",
                    self.task_id, i, transition_key, exc_info=True,
                )
                return GuardResult(
                    passed=False,
                    reason=f"Guard {i} for {transition_key} raised an internal error",
                    terminate=True,
                )
            if not result.passed:
                logger.info(
                    "TaskStateMachine [%s]: guard %d blocked %s: %s",
                    self.task_id, i, transition_key, result.reason,
                )
                return result

        return GuardResult(passed=True)

    def make_transition_key(self, source: TaskState, target: TaskState) -> str:
        """Build a transition key string, e.g. 'RUNNING->FAILED'."""
        return f"{source.value.upper()}->{target.value.upper()}"

    # ── Feed: the main loop feeds every action+result into TSM ──

    def feed(self, tool_name: str, params: dict, success: bool,
             file_path: str = "") -> None:
        """Feed one tool execution into the TSM.

        The main loop calls this after every tool call. TSM updates internal
        progress/macro-action state. The main loop does NOT need to know
        HOW progress is calculated — that's encapsulated here.
        """
        # 1. Update progress tracking
        self._update_progress(tool_name, params, success, file_path)
        # 2. Update macro action history for loop detection
        self._update_macro_history(tool_name, params)
        # 3. Record step
        self.record_step()

    def _update_progress(
        self, tool_name: str, params: dict, success: bool, file_path: str
    ) -> None:
        """Update progress state. Encapsulates ALL progress heuristics."""
        macro_type = _TOOL_TO_MACRO.get(tool_name, "other")
        is_write = macro_type == "write_file"
        is_read = macro_type == "read_file"
        is_validate = macro_type == "validate"
        is_finish = macro_type == "finish"

        # Track distinct files
        if is_read and file_path:
            is_new = file_path not in self._distinct_files_read
            self._distinct_files_read.add(file_path)
        else:
            is_new = False

        if is_write and file_path:
            self._distinct_files_written.add(file_path)

        # Determine if this action counts as progress
        is_analysis = (
            self.task_intent is not None
            and getattr(self.task_intent, "is_analysis", False)
        )
        if is_write or is_validate or is_finish:
            made_progress = True
        elif is_analysis and (macro_type == "read_file" and is_new):
            made_progress = True
        elif is_analysis and macro_type == "search_code":
            made_progress = True
        elif not is_analysis and macro_type == "read_file" and is_new:
            # Edit task: reading NEW files = exploration progress
            made_progress = True
        else:
            made_progress = False

        if made_progress:
            self._no_progress_count = 0
            if is_write:
                self._macro_action_history.clear()  # hard reset on write
        else:
            self._no_progress_count += 1

    def _update_macro_history(
        self, tool_name: str, params: dict
    ) -> None:
        """Append a macro-action record for loop fingerprint analysis."""
        macro_type = _TOOL_TO_MACRO.get(tool_name, "other")
        detail = ""
        if macro_type == "spawn_subagent":
            detail = params.get("subagent_type", params.get("description", ""))
        elif macro_type in ("read_file", "write_file"):
            detail = params.get("path", params.get("file_path", ""))
        elif macro_type == "run_shell":
            cmd = params.get("command", params.get("cmd", ""))
            detail = str(cmd)[:60]

        record = MacroActionRecord(
            action_type=macro_type,
            tool_name=tool_name,
            detail=detail,
        )
        self._macro_action_history.append(record)

        # Trim to window
        if len(self._macro_action_history) > self._macro_window_size * 2:
            self._macro_action_history = self._macro_action_history[-self._macro_window_size:]

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
            TaskState.COMPLETED_VERIFIED: RunStatus.SUCCESS,
            TaskState.COMPLETED_UNVERIFIED: RunStatus.SUCCESS,
            TaskState.COMPLETED_UNVERIFIED_NO_ENV: RunStatus.SUCCESS,
            TaskState.COMPLETED_UNVERIFIED_FAILED: RunStatus.SUCCESS,
            TaskState.FAILED: RunStatus.FAILED,
            TaskState.CANCELLED: RunStatus.GAVE_UP,
            TaskState.BLOCKED_BY_ENV: RunStatus.BLOCKED,
        }
        # Non-terminal → treat as in-progress
        return mapping.get(self._state, RunStatus.SUCCESS)
