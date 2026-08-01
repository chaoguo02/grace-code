"""
CC-Native Compaction Strategy chain.

Design (P0_1 Batch 2):
  CompactionStrategy (ABC) — one strategy in the progressive chain.
  MicroCompactor — no API call, time-decay clear of old tool outputs.
  SessionMemoryCompactor — reuse extracted session memory as summary.
  APICompactor — fork-agent LLM summary with circuit breaker.

The chain runs in priority order:
  Micro → SessionMemory → API → (DeterministicTrimmer as fallback)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from context.planner import BudgetPlan

# ── CompactResult ────────────────────────────────────────────────────────────

@dataclass
class CompactResult:
    compacted_messages: list[dict]
    summary: str | None
    tokens_saved: int
    strategy: str               # "micro" | "session_memory" | "api"
    preserved_start: int = 0    # first preserved message index


# ── CompactionStrategy (interface) ──────────────────────────────────────────

class CompactionStrategy(ABC):
    """One compaction strategy in the progressive chain.

    Each strategy is independently testable.
    Strategies are chained by priority — Micro first, API last.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy name for logging and stats."""
        ...

    @abstractmethod
    def compact(
        self,
        messages: list[dict],
        current_tokens: int,
        target_tokens: int,
        *,
        task_context: str = "",
    ) -> CompactResult:
        """Attempt to compact *messages*.

        Args:
            messages: Current conversation messages (dicts).
            current_tokens: Estimated token count of *messages*.
            target_tokens: Desired token count after compaction.
            task_context: Optional task description for the summary.

        Returns:
            CompactResult with compacted messages and stats.
            If the strategy cannot/should not run, return a no-op result
            with tokens_saved=0.
        """
        ...


# ── MicroCompactor ──────────────────────────────────────────────────────────

class MicroCompactor(CompactionStrategy):
    """CC-aligned MicroCompact — no API call.

    Clears content from OLD tool outputs (Read, Bash, Grep, Glob, etc.),
    replacing them with ``[Old tool result content cleared]``.

    Uses time-decay: older outputs cleared first.
    Current-turn outputs are NEVER cleared.
    Images/documents over 2_000 chars are also stripped.

    Aligns with CC's MicroCompact phase (timeBasedMCConfig.ts).
    """

    CLEARED_MARKER = "[Old tool result content cleared]"
    LARGE_CONTENT_THRESHOLD = 2_000  # chars
    MIN_TOOL_RESULTS_TO_CLEAR = 3    # only clear when there are >= this many

    @property
    def name(self) -> str:
        return "micro"

    def compact(
        self,
        messages: list[dict],
        current_tokens: int,
        target_tokens: int,
        *,
        task_context: str = "",
    ) -> CompactResult:
        # Find the last user message — everything after it is "current turn"
        last_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break

        # Collect indices of old tool_result messages to clear
        tool_result_indices: list[int] = []
        for i, m in enumerate(messages):
            if i >= last_user_idx:
                break  # never touch current-turn messages
            role = m.get("role", "")
            if role == "tool" or role == "tool_result":
                tool_result_indices.append(i)

        if len(tool_result_indices) < self.MIN_TOOL_RESULTS_TO_CLEAR:
            return CompactResult(
                compacted_messages=messages,
                summary=None,
                tokens_saved=0,
                strategy=self.name,
            )

        cleared = 0
        result = list(messages)
        for idx in tool_result_indices:
            m = result[idx]
            content = m.get("content", "")
            if _is_large_content(content, self.LARGE_CONTENT_THRESHOLD):
                old_len = _content_len(content)
                result[idx] = {**m, "content": self.CLEARED_MARKER}
                new_len = len(self.CLEARED_MARKER)
                cleared += max(0, old_len - new_len)

        # conservative: divide by 3.6 to estimate token savings
        tokens_saved = max(0, cleared // 4)

        return CompactResult(
            compacted_messages=result,
            summary=None,
            tokens_saved=tokens_saved,
            strategy=self.name,
            preserved_start=last_user_idx,
        )


# ── helpers ─────────────────────────────────────────────────────────────────

def _is_large_content(content, threshold: int) -> bool:
    """Check if content (str or list) exceeds the threshold in chars."""
    return _content_len(content) > threshold


def _content_len(content) -> int:
    """Approximate character length of message content."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(str(b)) for b in content)
    return len(str(content)) if content else 0
