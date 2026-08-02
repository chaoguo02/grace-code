"""agent/recovery.py

Agent 循环状态管理 — RecoveryState, Transition, AgentTurnState。
从 agent/core.py 提取。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm.base import LLMMessage, LLMToolSchema
    from agent.task import RunResult


# ---------------------------------------------------------------------------
# RecoveryState — CC-aligned continue-site tracking
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecoveryState:
    """Tracks recovery attempts across loop iterations (CC: State fields)."""
    escalation_applied: bool = False
    output_recovery_count: int = 0
    has_attempted_reactive_compact: bool = False
    nudge_count: int = 0
    last_nudge_tokens: int = 0

    _MAX_OUTPUT_RECOVERY: int = 3
    _DIMINISHING_THRESHOLD: int = 500
    _COMPLETION_RATIO: float = 0.9
    _ESCALATED_MAX_TOKENS: int = 64000

    def can_escalate(self, current_max_tokens: int) -> bool:
        return not self.escalation_applied and current_max_tokens < self._ESCALATED_MAX_TOKENS

    def can_recover_output(self) -> bool:
        return self.output_recovery_count < self._MAX_OUTPUT_RECOVERY

    def can_reactive_compact(self) -> bool:
        return not self.has_attempted_reactive_compact

    def is_diminishing(self, current_tokens: int) -> bool:
        if self.nudge_count < 3:
            return False
        delta = current_tokens - self.last_nudge_tokens
        return delta < self._DIMINISHING_THRESHOLD

    def should_nudge(self, total_tokens: int, budget: int) -> bool:
        if budget <= 0:
            return False
        return (
            total_tokens < int(budget * self._COMPLETION_RATIO)
            and not self.is_diminishing(total_tokens)
        )

    def reset_for_new_turn(self) -> "RecoveryState":
        return replace(self, has_attempted_reactive_compact=False)


# ---------------------------------------------------------------------------
# Transition — typed why the loop continued (CC: Continue)
# ---------------------------------------------------------------------------

class TransitionReason(str, Enum):
    NEXT_TURN = "next_turn"
    STOP_HOOK_BLOCKING = "stop_hook_blocking"
    COMPLETION_BLOCKED = "completion_blocked"
    ESCALATION = "escalation"
    RECOVERY = "recovery"
    REACTIVE_COMPACT = "reactive_compact"
    NUDGE = "nudge"
    REFLECTION = "reflection"


@dataclass(frozen=True)
class Transition:
    reason: TransitionReason
    detail: str = ""

    @classmethod
    def next_turn(cls) -> "Transition":
        return cls(TransitionReason.NEXT_TURN)

    @classmethod
    def escalation(cls, new_max_tokens: int) -> "Transition":
        return cls(TransitionReason.ESCALATION, f"max_tokens→{new_max_tokens}")

    @classmethod
    def recovery(cls, attempt: int) -> "Transition":
        return cls(TransitionReason.RECOVERY, f"attempt_{attempt}")

    @classmethod
    def reactive_compact(cls) -> "Transition":
        return cls(TransitionReason.REACTIVE_COMPACT)

    @classmethod
    def nudge(cls, remaining: int) -> "Transition":
        return cls(TransitionReason.NUDGE, f"budget_remaining={remaining}")

    @classmethod
    def stop_hook_blocking(cls) -> "Transition":
        return cls(TransitionReason.STOP_HOOK_BLOCKING)

    @classmethod
    def completion_blocked(cls, detail: str = "") -> "Transition":
        return cls(TransitionReason.COMPLETION_BLOCKED, detail)

    @classmethod
    def reflection(cls) -> "Transition":
        return cls(TransitionReason.REFLECTION)


# ---------------------------------------------------------------------------
# AgentTurnState — immutable per-turn state (CC: State in queryLoop)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentTurnState:
    """Immutable cross-turn state. Each continue site produces a new instance."""
    turn_count: int = 0
    messages: tuple["LLMMessage", ...] = ()
    tool_schemas: tuple["LLMToolSchema", ...] = ()
    total_tokens: int = 0
    child_turn_phase: str = "none"
    recovery: "RecoveryState" = field(default_factory=RecoveryState)
    stop_hook_count: int = 0
    stop_hook_verify_count: int = 0
    transition: "Transition | None" = None

    def with_updates(self, **kwargs) -> "AgentTurnState":
        return replace(self, **kwargs)

    def with_recovery_update(self, **kwargs) -> "AgentTurnState":
        return replace(self, recovery=replace(self.recovery, **kwargs))

    def with_transition(self, transition: Transition, **kwargs) -> "AgentTurnState":
        return replace(self, transition=transition, **kwargs)


@dataclass(frozen=True)
class TurnOutcome:
    """Result of processing one agent turn (CC: Terminal | Continue)."""
    terminal: "RunResult | None" = None
    next_state: AgentTurnState | None = None

    @classmethod
    def continue_(cls, state: AgentTurnState, reason: str = "") -> "TurnOutcome":
        return cls(next_state=state.with_updates(transition_reason=reason))

    @classmethod
    def terminate(cls, result: "RunResult") -> "TurnOutcome":
        return cls(terminal=result)
