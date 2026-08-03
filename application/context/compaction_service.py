"""
G35: Compaction Service — snapshot-based, not EventBus-driven.

- PreCompact Hook is a synchronous gate (checked by HookDispatcher).
- Actual compaction is a direct command call, not an EventBus event.
- Result is a typed snapshot/fact — no EventBus driving business commands.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Immutable compaction snapshot."""
    tokens_before: int
    tokens_after: int
    messages_before: int
    messages_after: int
    summary: str = ""
    truncated: bool = False


class CompactionService:
    """Compacts conversation context as a direct command call.

    G35: NOT driven by EventBus.  Called directly by the Coordinator
    after PreCompact Hook gate passes.
    """

    # Budget ratios
    SYSTEM_RESERVE = 0.15   # 15% for system prompt
    RECENT_RESERVE = 0.50   # 50% for recent messages
    SUMMARY_RATIO = 0.35    # 35% for older summary

    def compact(self, messages: list[dict],
                system_prompt: str = "",
                max_tokens: int = 100_000) -> CompactionResult:
        """Compact messages to fit within budget.

        Strategy: keep system prompt + most recent messages + summary of older.
        Returns typed CompactionResult (a snapshot, not an EventBus command).
        """
        before = self._count_tokens(messages)
        before_count = len(messages)

        system_tokens = len(system_prompt) // 4 if system_prompt else 0
        available = max_tokens - system_tokens

        if before <= available:
            return CompactionResult(
                tokens_before=before, tokens_after=before,
                messages_before=before_count, messages_after=before_count,
                truncated=False,
            )

        # Keep most recent messages that fit in RECENT_RESERVE budget
        recent_budget = int(available * self.RECENT_RESERVE)
        kept = self._keep_recent(messages, recent_budget)

        after = self._count_tokens(kept)
        return CompactionResult(
            tokens_before=before, tokens_after=after + system_tokens,
            messages_before=before_count, messages_after=len(kept),
            summary=f"compacted {before_count} → {len(kept)} messages",
            truncated=True,
        )

    def _keep_recent(self, messages: list[dict], budget: int) -> list[dict]:
        """Keep most recent messages within token budget."""
        kept: list[dict] = []
        used = 0
        for m in reversed(messages):
            mt = len(str(m.get("content", ""))) // 4 + 10
            if used + mt > budget and kept:
                break
            used += mt
            kept.append(m)
        return list(reversed(kept))

    @staticmethod
    def _count_tokens(messages: list[dict]) -> int:
        """Estimate tokens for a message list."""
        return sum(len(str(m.get("content", ""))) // 4 + 10 for m in messages)
