"""Context Collapse — CC-aligned read-time projection for context compression.

Architecture (CC: contextCollapse/):
  CollapseStore   — records collapse operations as {range, summary} entries
  project_view()  — applies collapse store to produce a compressed message list
  ContextCollapser — decides when to collapse + generates summaries via LLM

Key design principles (CC):
  - Original messages are NEVER mutated — only the projected view changes
  - Collapse records persist across restarts (cross-turn persistence)
  - project_view() runs at read time before every API call
  - Collapse is cheaper than AutoCompact (summarizes a subset, not everything)
  - recover_from_overflow() commits pending collapses on 413 prompt-too-long
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Collapse Entry ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CollapseEntry:
    """One collapse operation: replace messages [start, end) with a summary.

    CC: CollapseStore entry — immutable record of a completed collapse.
    """
    start: int         # inclusive start index in the original message list
    end: int           # exclusive end index
    summary: str       # LLM-generated summary of messages[start:end]

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "summary": self.summary}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CollapseEntry":
        return cls(start=int(d["start"]), end=int(d["end"]), summary=str(d["summary"]))


# ── Collapse Store ──────────────────────────────────────────────────────────

@dataclass
class CollapseStore:
    """Ordered collection of collapse entries (CC: CollapseStore).

    Entries must be non-overlapping and sorted by start index.
    The store is mutable — entries are added as collapses complete.
    """

    entries: list[CollapseEntry] = field(default_factory=list)

    def add(self, entry: CollapseEntry) -> None:
        """Add a collapse entry, maintaining sorted non-overlapping order."""
        # Ensure non-overlapping by removing any entries that intersect
        self.entries = [
            e for e in self.entries
            if e.end <= entry.start or e.start >= entry.end
        ]
        self.entries.append(entry)
        self.entries.sort(key=lambda e: e.start)

    @property
    def total_collapsed(self) -> int:
        """Number of original messages collapsed into summaries."""
        return sum(e.end - e.start for e in self.entries)

    @property
    def is_empty(self) -> bool:
        return len(self.entries) == 0

    def to_dicts(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.entries]

    @classmethod
    def from_dicts(cls, dicts: list[dict[str, Any]]) -> "CollapseStore":
        return cls(entries=[CollapseEntry.from_dict(d) for d in dicts])


# ── Project View ────────────────────────────────────────────────────────────

def project_view(
    messages: list[dict],
    store: CollapseStore,
) -> list[dict]:
    """CC: projectView() — produce a compressed view at read time.

    Walks the original message list and replaces collapsed ranges with
    synthetic summary messages.  Original messages are NOT mutated —
    the caller passes a fresh list or the result is a new list.

    Args:
        messages: original message dicts (from ConversationHistory.to_dicts())
        store: CollapseStore with collapse entries

    Returns:
        New list with collapsed ranges replaced by summary messages.
    """
    if store.is_empty:
        return list(messages)

    result: list[dict] = []
    idx = 0
    for entry in store.entries:
        # Copy messages before the collapsed range
        result.extend(messages[idx:entry.start])
        # Insert the summary as a synthetic user message
        result.append({
            "role": "user",
            "content": (
                f"[CONTEXT COLLAPSE — messages {entry.start}-{entry.end - 1} summarized]\n"
                f"{entry.summary}"
            ),
        })
        idx = entry.end
    # Copy remaining messages after the last collapse
    result.extend(messages[idx:])
    return result


# ── Context Collapser ───────────────────────────────────────────────────────

@dataclass
class ContextCollapser:
    """Decides when to collapse and generates collapse summaries.

    CC integration point: runs between MicroCompact and AutoCompact.
    If collapse frees enough tokens, AutoCompact is skipped entirely.
    """

    # How many messages to collapse at once
    batch_size: int = 8
    # Collapse messages older than this many turns
    min_age_turns: int = 4
    # Trigger: collapsed token count > this fraction of budget
    trigger_ratio: float = 0.75

    def should_collapse(
        self,
        history_dicts: list[dict],
        history_budget: int,
        *,
        store: CollapseStore | None = None,
    ) -> bool:
        """Check if collapse would help reduce context pressure.

        Returns True if there are enough old messages worth collapsing,
        AND the effective token count exceeds the trigger threshold.
        """
        from context.token_budget import estimate_tokens

        if len(history_dicts) < self.batch_size + self.min_age_turns:
            return False

        # Only collapse if we're over the threshold
        total = sum(estimate_tokens(m.get("content", "")) for m in history_dicts)
        if total < int(history_budget * self.trigger_ratio):
            return False

        # Don't collapse if the store already covers most of the history
        if store is not None and store.total_collapsed >= len(history_dicts) // 2:
            return False

        return True

    def pick_range(
        self,
        history_dicts: list[dict],
        store: CollapseStore | None = None,
    ) -> tuple[int, int]:
        """Pick the range of messages to collapse next.

        CC pattern: collapse the oldest messages that aren't already collapsed.
        Returns (start, end) — inclusive start, exclusive end.
        """
        # Start after the last collapse, or at the beginning
        start = 0
        if store is not None and not store.is_empty:
            start = store.entries[-1].end

        # Collapse up to batch_size messages, but leave recent ones alone
        end = min(start + self.batch_size, len(history_dicts) - self.min_age_turns)
        if end <= start:
            return (0, 0)
        return (start, end)

    def build_collapse_prompt(
        self,
        messages: list[dict],
        start: int,
        end: int,
    ) -> str:
        """Build the LLM prompt for generating a collapse summary.

        CC: the collapse agent is a lightweight summarizer that extracts
        key facts, decisions, file paths, and errors from the range.
        """
        range_text = "\n".join(
            f"[{i}] {m.get('role', '?')}: {str(m.get('content', ''))[:500]}"
            for i, m in enumerate(messages[start:end])
        )
        return (
            "Summarize the following conversation segment. Extract only:\n"
            "- Key decisions made\n"
            "- Files modified or created (with paths)\n"
            "- Errors encountered and how they were resolved\n"
            "- Important facts discovered\n\n"
            "Be concise. This summary will replace the original messages "
            "to save context space.\n\n"
            f"{range_text}\n\n"
            "Summary:"
        )
