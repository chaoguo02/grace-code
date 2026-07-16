"""Runtime Controller — orchestrates all Runtime-enforced per-step checks.

Claude Code philosophy: the Runtime is the operating system. It checks conditions
BEFORE each step, not after. The model has no say in these decisions.

This module consolidates what was previously scattered across _run_body():
- Circuit breaker check
- Execution budget check (WARNING → CRITICAL → EXHAUSTED)
- Context window check
- Max steps check
- Consecutive failure check

All checks return a StepDecision. The main loop simply obeys:
- CONTINUE: proceed with the step
- INJECT_MESSAGE: inject the message, then proceed (possibly with stripped tools)
- TERMINATE: call _finish_run() immediately — the model gets no more turns
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from agent.task import RunStatus, TerminationReason

if TYPE_CHECKING:
    from core.circuit_breaker import CircuitBreaker
    from agent.event_log import EventLog
    from agent.v2.execution_budget import BudgetLevel, BudgetStatus, ExecutionBudget
    from agent.v2.task_state_machine import TaskStateMachine
    from context.history import ConversationHistory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared Runtime types (merged from runtime_control.py)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolDecision:
    allowed: bool
    reason: str
    next_phase: str | None = None
    synthetic_observation: str | None = None


@dataclass(frozen=True)
class RecoveryAction:
    kind: Literal[
        "reflect",
        "hide_tools",
        "force_answer",
        "give_up",
        "ask_user",
        "deterministic_summary",
    ]
    reason: str
    prompt: str = ""
    summary: str = ""


# ---------------------------------------------------------------------------
# StepDecision
# ---------------------------------------------------------------------------

class StepAction(str, Enum):
    CONTINUE = "continue"           # Proceed normally
    INJECT_MESSAGE = "inject"       # Inject message, then proceed
    TERMINATE = "terminate"         # Stop immediately


@dataclass
class StepDecision:
    """Result of a per-step Runtime check. The main loop MUST obey this."""

    action: StepAction
    inject_message: str = ""
    """If action==INJECT_MESSAGE, this goes into the conversation."""

    strip_tools: bool = False
    """If True, the next LLM call gets tools=[] (budget exhausted)."""

    terminate_status: RunStatus | None = None
    terminate_summary: str = ""
    terminate_reason: TerminationReason = TerminationReason.NONE
    terminate_detail: str = ""


# ---------------------------------------------------------------------------
# RuntimeController
# ---------------------------------------------------------------------------

@dataclass
class RuntimeController:
    """Central Runtime authority for all per-step enforcement.

    Called at the START of each main loop iteration. Returns a StepDecision
    that the loop MUST obey. The model has no opportunity to override.

    Usage:
        controller = RuntimeController(
            budget=_execution_budget,
            breaker=circuit_breaker,
            state_machine=_tsm,
            cfg=agent_config,
        )

        for step in range(1, max_steps + 1):
            decision = controller.check(step, total_tokens, history, log)
            if decision.action == StepAction.TERMINATE:
                return finish_run(...)
            if decision.inject_message:
                history.add(LLMMessage(role="user", content=decision.inject_message))
            if decision.strip_tools:
                tools = []
            # ... build messages, call LLM, execute tools ...
    """

    budget: Any = None           # ExecutionBudget
    breaker: Any = None          # CircuitBreaker
    state_machine: Any = None    # TaskStateMachine

    # Config
    max_steps: int = 40
    budget_tokens: int = 80_000
    context_compact_buffer: int = 13_000
    max_context_window: int = 200_000
    max_consecutive_failures: int = 3

    # State tracking
    _compact_warning_injected: bool = False

    def check(
        self,
        step: int,
        total_tokens: int,
        history: Any,          # ConversationHistory
        log: Any,              # EventLog
        *,
        context_size: int = 0,
        request_budget: int = 0,
        consecutive_failures: int = 0,
    ) -> StepDecision:
        """Run all Runtime checks for this step.

        Checks are ordered from most severe to least:
        1. Circuit breaker — terminates immediately
        2. Max steps — strip tools, inject message, final turn
        3. Loop detection — terminates immediately
        4. Budget exhaustion — strip tools, force finish
        5. Budget critical/warning — inject message
        6. Context window — inject warning if nearly full
        """
        # ── Check 1: Circuit breaker (code-level, not prompt-based) ──
        if self.breaker is not None and self.breaker.check():
            reason = self.breaker.trip_reason
            logger.warning("Circuit breaker tripped: %s", reason)
            if self.budget is not None:
                self.budget.exhaust(reason)
            return StepDecision(
                action=StepAction.TERMINATE,
                terminate_status=RunStatus.GAVE_UP,
                terminate_summary=reason,
                terminate_reason=TerminationReason.CIRCUIT_BREAKER,
                terminate_detail=reason,
            )

        # ── Check 2: Max steps — final turn, tools stripped ──
        if step == self.max_steps:
            return StepDecision(
                action=StepAction.INJECT_MESSAGE,
                strip_tools=True,
                inject_message=(
                    f"[SYSTEM] Maximum steps ({self.max_steps}) reached. "
                    "Produce your final summary now. No more tool calls."
                ),
            )

        # ── Check 3: Execution budget ──
        if self.budget is not None:
            # P0 FIX: budget.is_exhausted guards against the case where
            # ExecutionBudget.check() returns COMFORTABLE after the first
            # exhaustion (because _state has already changed to EXHAUSTED).
            # Once exhausted, tools stay stripped — the model cannot recover.
            if self.budget.is_exhausted:
                return StepDecision(
                    action=StepAction.INJECT_MESSAGE,
                    strip_tools=True,
                    inject_message=self.budget.force_finish_message(),
                )
            budget_status = self.budget.check()
            if budget_status.is_exhausted:
                logger.warning("Execution budget exhausted at step %d", step)
                return StepDecision(
                    action=StepAction.INJECT_MESSAGE,
                    strip_tools=True,
                    inject_message=self.budget.force_finish_message(),
                )
            if budget_status.inject_message:
                return StepDecision(
                    action=StepAction.INJECT_MESSAGE,
                    inject_message=budget_status.inject_message,
                )

        # ── Check 5: Context window nearly full ──
        if (
            context_size > 0
            and request_budget > 0
            and context_size >= request_budget - self.context_compact_buffer
            and not self._compact_warning_injected
        ):
            self._compact_warning_injected = True
            return StepDecision(
                action=StepAction.INJECT_MESSAGE,
                inject_message=(
                    f"[SYSTEM] Context window is nearly full "
                    f"(~{context_size}/{request_budget} tokens). "
                    "Wrap up your work and call finish. Do not read new files."
                ),
            )

        # ── Check 5: Consecutive failures threshold ──
        if consecutive_failures >= self.max_consecutive_failures:
            return StepDecision(
                action=StepAction.TERMINATE,
                terminate_status=RunStatus.GAVE_UP,
                terminate_summary=(
                    f"Aborting: {consecutive_failures} consecutive tool failures"
                ),
                terminate_reason=TerminationReason.TOOL_FAILURE_LIMIT,
                terminate_detail=f"{consecutive_failures} consecutive tool failures",
            )

        return StepDecision(action=StepAction.CONTINUE)

    def reset(self) -> None:
        """Reset per-run state (call between runs)."""
        self._compact_warning_injected = False
