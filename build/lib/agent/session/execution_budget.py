"""Unified Execution Budget — global Token + Step + Time budget pool.

Claude Code pattern (query.ts): strict token budget. If output hits the cap and is
truncated, the system inserts a "recovery prompt" to guide the model to continue.
If context is too long, it triggers context folding and message compression.

This module provides a unified budget that:
1. Tracks token consumption, step count, and elapsed time in one place.
2. Enforces three-level escalation:
   - WARNING (80%): inject "budget running low, start wrapping up"
   - CRITICAL (95%): inject "budget critical, finish NOW or I will force-stop"
   - EXHAUSTED (100%): strip tools, force completion — model cannot call tools
3. Parent and subagents share the budget pool (subagent consumption is charged
   to the parent).

Integration:
    budget = ExecutionBudget(token_limit=80_000, step_limit=40, time_limit_s=300)
    budget.transition(ExecutionBudgetState.RUNNING)

    for step in range(max_steps):
        status = budget.check()
        if status.level == BudgetLevel.EXHAUSTED:
            result = budget.exhausted_terminate_message()
            # strip tools, inject result, break
        elif status.level == BudgetLevel.CRITICAL:
            history.add(LLMMessage(role="user", content=status.inject_message))

        ...  # execute step
        budget.consume_tokens(response.total_tokens)
        budget.record_step()
"""

from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BudgetLevel — escalation levels
# ---------------------------------------------------------------------------

class BudgetLevel(str, Enum):
    COMFORTABLE = "comfortable"  # < 80% consumed
    WARNING = "warning"          # 80-95% consumed
    CRITICAL = "critical"        # 95-100% consumed
    EXHAUSTED = "exhausted"      # 100%+ consumed — force stop


# ---------------------------------------------------------------------------
# ExecutionBudgetState
# ---------------------------------------------------------------------------

class ExecutionBudgetState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    EXHAUSTED = "exhausted"
    COMPLETED = "completed"


# ---------------------------------------------------------------------------
# BudgetStatus
# ---------------------------------------------------------------------------

@dataclass
class BudgetStatus:
    """Result of a budget check."""
    level: BudgetLevel
    token_used: int
    token_limit: int
    steps_taken: int
    step_limit: int
    elapsed_seconds: float
    time_limit_s: float
    inject_message: str = ""
    """If non-empty, this MUST be injected into the conversation."""

    @property
    def is_exhausted(self) -> bool:
        return self.level == BudgetLevel.EXHAUSTED

    @property
    def token_percent(self) -> float:
        if self.token_limit <= 0:
            return 0.0
        return min(100.0, self.token_used / self.token_limit * 100)

    @property
    def step_percent(self) -> float:
        if self.step_limit <= 0:
            return 0.0
        return min(100.0, self.steps_taken / self.step_limit * 100)


# ---------------------------------------------------------------------------
# ExecutionBudgetConfig
# ---------------------------------------------------------------------------

@dataclass
class ExecutionBudgetConfig:
    """Configuration for the unified execution budget."""

    token_limit: int = 200_000
    """Maximum billable tokens for the entire run (including subagents)."""

    step_limit: int = 100
    """Maximum main-loop steps."""

    time_limit_seconds: float = 1800.0
    """Wall-clock time limit in seconds. 0 = disabled. Default 30 min for build agent."""

    warning_threshold: float = 0.80
    """Fraction of limit at which WARNING level triggers."""

    critical_threshold: float = 0.95
    """Fraction of limit at which CRITICAL level triggers."""

# ---------------------------------------------------------------------------
# BudgetUsage — consumption attribution (P2)
# ---------------------------------------------------------------------------

@dataclass
class BudgetUsage:
    """Track where budget is spent: productive work vs retries vs overhead."""
    productive_steps: int = 0
    retry_steps: int = 0         # safety checks (Read-before-Edit etc) triggering rework
    overhead_tokens: int = 0     # system/error messages consuming budget
    subagent_tokens: int = 0     # tokens consumed by delegated subagents

    @property
    def total_steps(self) -> int:
        return self.productive_steps + self.retry_steps

    def to_dict(self) -> dict:
        return {
            "productive_steps": self.productive_steps,
            "retry_steps": self.retry_steps,
            "overhead_tokens": self.overhead_tokens,
            "subagent_tokens": self.subagent_tokens,
            "total_steps": self.total_steps,
        }


# ---------------------------------------------------------------------------
# ExecutionBudget
# ---------------------------------------------------------------------------

class BudgetExhausted(Exception):
    """Raised when the budget is exhausted and the agent cannot continue."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class BudgetReservation:
    """One idempotent child allocation owned by its parent budget."""

    def __init__(self, budget: "ExecutionBudget", reserved_tokens: int) -> None:
        self._budget = budget
        self.reserved_tokens = reserved_tokens
        self._settled = False
        self._lock = threading.Lock()

    def settle(self, actual_tokens: int) -> None:
        with self._lock:
            if self._settled:
                return
            self._settled = True
        self._budget._settle_reservation(
            self.reserved_tokens,
            max(0, int(actual_tokens)),
        )

    def release(self) -> None:
        self.settle(0)

    @property
    def is_settled(self) -> bool:
        with self._lock:
            return self._settled


@dataclass
class ExecutionBudget:
    """Unified execution budget tracking token, step, and time consumption.

    Usage:
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=80_000, step_limit=40,
        ))
        budget.start()

        for step in range(max_steps):
            status = budget.check()
            if status.level == BudgetLevel.EXHAUSTED:
                # Force finish — strip tools, inject final message
                break
            if status.inject_message:
                history.add(LLMMessage(role="user", content=status.inject_message))

            response = backend.complete(messages, tools)
            budget.consume(response.total_tokens)
            budget.record_step()

        budget.complete()
    """

    config: ExecutionBudgetConfig = field(default_factory=ExecutionBudgetConfig)

    # ── Counters ──
    _state: ExecutionBudgetState = ExecutionBudgetState.PENDING
    _token_used: int = 0
    _steps_taken: int = 0
    _started_at: float = 0.0
    _completed_at: float = 0.0
    _last_level: BudgetLevel = BudgetLevel.COMFORTABLE
    _warning_injected: bool = False
    _critical_injected: bool = False
    _force_finish_injected: bool = False
    _token_reserved: int = 0
    _token_lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
    )
    usage: BudgetUsage = field(default_factory=BudgetUsage)

    def record_retry_step(self) -> None:
        """Mark a step as triggered by safety check (Read-before-Edit etc)."""
        self.usage.retry_steps += 1

    def record_overhead_tokens(self, tokens: int) -> None:
        """Track tokens spent on system/error messages."""
        self.usage.overhead_tokens += tokens

    def record_subagent_tokens(self, tokens: int) -> None:
        """Track tokens consumed by delegated subagents."""
        self.usage.subagent_tokens += tokens

    def get_usage_report(self) -> dict:
        """Return budget consumption attribution for diagnostics."""
        return {
            **self.usage.to_dict(),
            "token_used": self._token_used,
            "token_reserved": self.token_reserved,
            "token_limit": self.config.token_limit,
            "steps_taken": self._steps_taken,
            "step_limit": self.config.step_limit,
            "elapsed_s": round(self.elapsed_seconds, 1),
            "time_limit_s": self.config.time_limit_seconds,
        }

    # ── Properties ──

    @property
    def state(self) -> ExecutionBudgetState:
        return self._state

    @property
    def token_used(self) -> int:
        with self._token_lock:
            return self._token_used

    @property
    def token_reserved(self) -> int:
        with self._token_lock:
            return self._token_reserved

    @property
    def steps_taken(self) -> int:
        return self._steps_taken

    @property
    def elapsed_seconds(self) -> float:
        from core.time_utils import elapsed_seconds

        return elapsed_seconds(self._started_at, self._completed_at)

    @property
    def token_remaining(self) -> int:
        with self._token_lock:
            return max(
                0,
                self.config.token_limit
                - self._token_used
                - self._token_reserved,
            )

    @property
    def steps_remaining(self) -> int:
        return max(0, self.config.step_limit - self._steps_taken)

    @property
    def is_exhausted(self) -> bool:
        return self._state == ExecutionBudgetState.EXHAUSTED

    # ── Lifecycle ──

    def start(self) -> None:
        """Mark the budget as running. Call before the main loop."""
        self._state = ExecutionBudgetState.RUNNING
        self._started_at = _time.time()
        logger.debug(
            "ExecutionBudget started: tokens=%d, steps=%d, time=%.0fs",
            self.config.token_limit, self.config.step_limit,
            self.config.time_limit_seconds,
        )

    def complete(self) -> None:
        """Mark the budget as completed (graceful finish)."""
        self._state = ExecutionBudgetState.COMPLETED
        self._completed_at = _time.time()

    def exhaust(self, reason: str = "") -> None:
        """Force-exhaust the budget."""
        self._state = ExecutionBudgetState.EXHAUSTED
        self._completed_at = _time.time()
        if reason:
            logger.warning("ExecutionBudget exhausted: %s", reason)

    # ── Consumption ──

    def consume(self, tokens: int) -> None:
        """Consume tokens from the budget. Charges subagent usage here."""
        with self._token_lock:
            self._token_used += max(0, tokens)

    def reserve_tokens(self, tokens: int) -> BudgetReservation:
        """Atomically allocate a child ceiling from the remaining parent pool."""
        requested = max(0, int(tokens))
        if requested <= 0:
            raise BudgetExhausted("child token reservation must be positive")
        with self._token_lock:
            available = max(
                0,
                self.config.token_limit
                - self._token_used
                - self._token_reserved,
            )
            if requested > available:
                raise BudgetExhausted(
                    f"child requested {requested} tokens but only "
                    f"{available} remain"
                )
            self._token_reserved += requested
        return BudgetReservation(self, requested)

    def _settle_reservation(
        self,
        reserved_tokens: int,
        actual_tokens: int,
    ) -> None:
        with self._token_lock:
            self._token_reserved = max(
                0, self._token_reserved - max(0, reserved_tokens),
            )
            self._token_used += max(0, actual_tokens)
            self.usage.subagent_tokens += max(0, actual_tokens)

    def record_step(self, *, retry: bool = False) -> int:
        """Record a main-loop iteration. Returns the new step count."""
        self._steps_taken += 1
        if retry:
            self.usage.retry_steps += 1
        else:
            self.usage.productive_steps += 1
        return self._steps_taken

    # ── Check ──

    def check(self) -> BudgetStatus:
        """Check budget levels and return a BudgetStatus with instructions.

        Call this at the start of each main-loop iteration.
        If the returned status has a non-empty inject_message, it MUST be
        added to the conversation history.
        """
        if self._state != ExecutionBudgetState.RUNNING:
            return BudgetStatus(
                level=BudgetLevel.COMFORTABLE,
                token_used=self._token_used,
                token_limit=self.config.token_limit,
                steps_taken=self._steps_taken,
                step_limit=self.config.step_limit,
                elapsed_seconds=self.elapsed_seconds,
                time_limit_s=self.config.time_limit_seconds,
            )

        # Determine the limiting factor
        with self._token_lock:
            token_committed = self._token_used
            token_allocated = self._token_used + self._token_reserved
        token_pct = token_allocated / max(1, self.config.token_limit)
        step_pct = self._steps_taken / max(1, self.config.step_limit)
        time_pct = 0.0
        if self.config.time_limit_seconds > 0:
            time_pct = self.elapsed_seconds / self.config.time_limit_seconds

        max_pct = max(token_pct, step_pct, time_pct)

        # Determine level
        if max_pct >= 1.0:
            level = BudgetLevel.EXHAUSTED
        elif max_pct >= self.config.critical_threshold:
            level = BudgetLevel.CRITICAL
        elif max_pct >= self.config.warning_threshold:
            level = BudgetLevel.WARNING
        else:
            level = BudgetLevel.COMFORTABLE

        # Build inject message
        inject_message = ""
        if level == BudgetLevel.EXHAUSTED and not self._force_finish_injected:
            self._force_finish_injected = True
            self._state = ExecutionBudgetState.EXHAUSTED
            inject_message = self._build_exhausted_message(token_pct, step_pct, time_pct)
        elif level == BudgetLevel.CRITICAL and not self._critical_injected:
            self._critical_injected = True
            inject_message = self._build_critical_message(token_pct, step_pct, time_pct)
        elif level == BudgetLevel.WARNING and not self._warning_injected:
            self._warning_injected = True
            inject_message = self._build_warning_message(token_pct, step_pct, time_pct)

        if level != self._last_level:
            logger.debug(
                "ExecutionBudget level: %s → %s (token=%.0f%%, step=%.0f%%, time=%.0f%%)",
                self._last_level.value, level.value,
                token_pct * 100, step_pct * 100, time_pct * 100,
            )
            self._last_level = level

        return BudgetStatus(
            level=level,
            token_used=token_committed,
            token_limit=self.config.token_limit,
            steps_taken=self._steps_taken,
            step_limit=self.config.step_limit,
            elapsed_seconds=self.elapsed_seconds,
            time_limit_s=self.config.time_limit_seconds,
            inject_message=inject_message,
        )

    # ── Message builders ──

    def _build_warning_message(
        self, token_pct: float, step_pct: float, time_pct: float
    ) -> str:
        """Build the WARNING-level injection message."""
        parts = ["[SYSTEM] ⚠️ Execution budget warning:"]
        if token_pct >= self.config.warning_threshold:
            parts.append(
                f"- Tokens: {self._token_used}/{self.config.token_limit} "
                f"({token_pct * 100:.0f}% used, {self.token_remaining} remaining)"
            )
        if step_pct >= self.config.warning_threshold:
            parts.append(
                f"- Steps: {self._steps_taken}/{self.config.step_limit} "
                f"({step_pct * 100:.0f}% used, {self.steps_remaining} remaining)"
            )
        if time_pct >= self.config.warning_threshold:
            parts.append(
                f"- Time: {self.elapsed_seconds:.0f}s/{self.config.time_limit_seconds:.0f}s"
            )
        parts.append(
            "Start wrapping up. Prioritize the most important remaining work. "
            "Avoid reading new files unless essential. Focus on delivering results."
        )
        return "\n".join(parts)

    def _build_critical_message(
        self, token_pct: float, step_pct: float, time_pct: float
    ) -> str:
        """Build the CRITICAL-level injection message."""
        dominant = "budget"
        if token_pct >= step_pct and token_pct >= time_pct:
            dominant = "token budget"
        elif step_pct >= time_pct:
            dominant = "step budget"

        return (
            f"[SYSTEM] 🔴 CRITICAL: {dominant} nearly exhausted!\n"
            f"- Tokens: {self._token_used}/{self.config.token_limit}\n"
            f"- Steps: {self._steps_taken}/{self.config.step_limit}\n"
            f"- Time: {self.elapsed_seconds:.0f}s\n\n"
            "You MUST finish in the next 1-2 turns. Do NOT start new work. "
            "Do NOT read new files. Do NOT spawn subagents. "
            "Produce your final answer NOW using finish. "
            "If you call a tool instead, it will be rejected — tools will be "
            "stripped at the next turn."
        )

    def _build_exhausted_message(
        self, token_pct: float, step_pct: float, time_pct: float
    ) -> str:
        """Build the EXHAUSTED-level injection message."""
        return (
            f"[SYSTEM] 🛑 Budget exhausted. No more tool calls available.\n"
            f"- Tokens used: {self._token_used}/{self.config.token_limit}\n"
            f"- Steps taken: {self._steps_taken}/{self.config.step_limit}\n"
            f"- Elapsed: {self.elapsed_seconds:.0f}s\n\n"
            "Produce your final summary now — describe what was accomplished, "
            "what remains, and any key findings."
        )

    # ── Exhausted termination summary ──

    @staticmethod
    def exhausted_terminate_message() -> str:
        """Return the summary message when execution is terminated due to budget exhaustion.

        Used as ``terminate_detail`` in ``StepDecision`` and as
        ``inject_message`` in the ``budget_exhausted_guard``.
        """
        return (
            "Execution budget exhausted — the agent was unable to complete "
            "the task within the allocated token, step, or time limits. "
            "Consider increasing the budget or breaking the task into "
            "smaller pieces."
        )

    # ── Serialization ──

    def to_summary(self) -> dict:
        """Export budget state for diagnostics."""
        return {
            "state": self._state.value,
            "token_used": self._token_used,
            "token_limit": self.config.token_limit,
            "token_percent": round(
                self._token_used / max(1, self.config.token_limit) * 100, 1
            ),
            "steps_taken": self._steps_taken,
            "step_limit": self.config.step_limit,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "last_level": self._last_level.value,
        }
