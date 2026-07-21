"""
agent/loop/types.py — extracted control types for the ReAct main loop.

Contains the LoopAction enum, StepResult dataclass, and BlockTracker
dataclass originally embedded as a raw dict in agent/core.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.task import RunStatus, RunResult


class LoopAction(Enum):
    """Control-flow directive returned by every extracted loop step."""
    CONTINUE = auto()            # proceed to next step iteration
    RETRY_WITH_COMPACT = auto()  # compact and re-try LLM call (same step)
    RETURN = auto()              # exit loop, return RunResult


@dataclass
class StepResult:
    """A single step's output — the main loop applies these mutations.

    When action is RETURN, ``return_result`` must be set.
    When action is CONTINUE, ``history_messages`` and ``observations``
    record the state mutations to apply.
    """
    action: LoopAction
    return_result: "RunResult | None" = None
    history_messages: list = field(default_factory=list)
    step_increment: int = 1
    tokens_consumed: int = 0


@dataclass
class CompletionBlockTracker:
    """Completion-guard block counter — replaces the raw dict in core.py.

    Tracks consecutive blocks by reason and forces a give_up after the
    threshold is exceeded.  The original code used a dict with a sentinel
    key ``'_last_reason'`` (P1-5); this dataclass eliminates that pattern.
    """

    threshold: int = 3
    _last_reason: str = ""
    _count_by_reason: dict[str, int] = field(default_factory=dict)

    def should_block(self, reason: str) -> bool:
        """Increment the counter for *reason* and check the threshold.

        Returns True when the agent should give_up (same reason blocked
        threshold times consecutively).
        """
        if reason == self._last_reason:
            self._count_by_reason[reason] = self._count_by_reason.get(reason, 0) + 1
        else:
            self._last_reason = reason
            self._count_by_reason[reason] = 1
        return self._count_by_reason[reason] >= self.threshold
