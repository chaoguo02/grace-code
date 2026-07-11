"""Macro-action loop detection — catches global flow patterns, not just tool calls.

Claude Code pattern: the real solution to loops is *prevention*, not detection.
Dynamic Workflows split tasks into sub-agents with independent contexts and focused
goals. But when loops DO occur, they must be caught at the macro-flow level, not
just the tool-call level.

This detector tracks "macro actions" — high-level semantic categories of what the
agent is doing — and detects repeating patterns that indicate the agent is stuck
in a cycle across turns.

Macro action types:
    SPAWN_SUBAGENT  — dispatching a fork/task subagent
    READ_FILE       — reading a file (file_read, file_view)
    SEARCH_CODE     — searching codebase (grep, search_text, find_files)
    WRITE_FILE      — writing/editing a file (file_write, file_edit)
    RUN_SHELL       — executing a shell command
    VALIDATE        — running tests, checking results
    REFLECT         — received a reflection/injection, changing course
    FINISH          — model called finish/task_complete

Detection strategy:
    1. Track the last N macro actions (window configurable, default 6)
    2. Look for repeated patterns of length 2-3 within the window
    3. If a pattern repeats ≥ 2 times with no progress → circuit break
    4. Progress is measured by: file changes, distinct files read, test results
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MacroAction
# ---------------------------------------------------------------------------

class MacroActionType(str, Enum):
    SPAWN_SUBAGENT = "spawn_subagent"
    READ_FILE = "read_file"
    SEARCH_CODE = "search_code"
    WRITE_FILE = "write_file"
    RUN_SHELL = "run_shell"
    VALIDATE = "validate"
    REFLECT = "reflect"
    FINISH = "finish"
    OTHER = "other"


# Map tool names to macro action types
_TOOL_TO_MACRO: dict[str, MacroActionType] = {
    "task": MacroActionType.SPAWN_SUBAGENT,
    "file_read": MacroActionType.READ_FILE,
    "file_view": MacroActionType.READ_FILE,
    "search_text": MacroActionType.SEARCH_CODE,
    "find_files": MacroActionType.SEARCH_CODE,
    "find_symbol": MacroActionType.SEARCH_CODE,
    "file_write": MacroActionType.WRITE_FILE,
    "file_edit": MacroActionType.WRITE_FILE,
    "edit": MacroActionType.WRITE_FILE,
    "bash": MacroActionType.RUN_SHELL,
    "shell": MacroActionType.RUN_SHELL,
    "zsh": MacroActionType.RUN_SHELL,
    "test": MacroActionType.VALIDATE,
    "pytest": MacroActionType.VALIDATE,
    "finish": MacroActionType.FINISH,
    "task_complete": MacroActionType.FINISH,
}

# Macro actions that indicate forward progress (reset repetition counters)
_PROGRESS_ACTIONS = frozenset({
    MacroActionType.WRITE_FILE,
    MacroActionType.VALIDATE,
    MacroActionType.FINISH,
})


# ---------------------------------------------------------------------------
# MacroActionRecord
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MacroActionRecord:
    action_type: MacroActionType
    tool_name: str = ""
    detail: str = ""  # short description (file path, subagent name, etc.)

    def signature(self) -> str:
        """A compact string key for pattern matching."""
        return f"{self.action_type.value}:{self.tool_name}"


# ---------------------------------------------------------------------------
# MacroLoopDetectorConfig
# ---------------------------------------------------------------------------

@dataclass
class MacroLoopDetectorConfig:
    """Configuration for macro-action loop detection."""

    window_size: int = 6
    """Number of recent macro actions to analyze."""

    min_pattern_length: int = 2
    """Minimum length of a repeating pattern to detect."""

    max_pattern_length: int = 3
    """Maximum length of a repeating pattern to detect (avoid combinatorics)."""

    min_repetitions: int = 2
    """How many times a pattern must repeat to trigger detection."""

    max_no_progress_cycles: int = 2
    """Maximum macro-action cycles without any progress action (×3 actions)."""

    noise_tolerant_min_occurrences: int = 3
    """For noise-tolerant detection: trip if any 2-action pair appears ≥ this many
    times within the window (even non-consecutively). 0 = disabled."""

    enabled: bool = True


# ---------------------------------------------------------------------------
# MacroLoopDetector
# ---------------------------------------------------------------------------

class MacroLoopTripped(Exception):
    """Raised when a macro-level loop is detected."""

    def __init__(self, pattern: str, repetitions: int, detail: str) -> None:
        super().__init__(
            f"Macro loop detected: pattern [{pattern}] repeated {repetitions}x. {detail}"
        )
        self.pattern = pattern
        self.repetitions = repetitions


@dataclass
class MacroLoopDetector:
    """Detects repeating patterns in high-level agent behavior.

    Unlike local tool-level loop detection (which catches "same tool + same params"
    repeated), this detects broader patterns like:

        SPAWN_SUBAGENT → READ_FILE → SPAWN_SUBAGENT → READ_FILE → SPAWN_SUBAGENT

    This pattern indicates the parent agent is delegating, reading results,
    then delegating again in a loop rather than making progress.

    Usage:
        detector = MacroLoopDetector()
        detector.record_tool_call("task", {"subagent_type": "explore"})
        detector.record_tool_call("file_read", {"path": "a.py"})
        detector.record_tool_call("task", {"subagent_type": "explore"})
        detector.record_tool_call("file_read", {"path": "b.py"})
        detector.record_tool_call("task", {"subagent_type": "explore"})
        # → tripped! pattern SPAWN→READ repeated 2+ times
    """

    config: MacroLoopDetectorConfig = field(default_factory=MacroLoopDetectorConfig)
    _history: list[MacroActionRecord] = field(default_factory=list)
    _no_progress_count: int = 0
    _distinct_files_read: set[str] = field(default_factory=set)
    _distinct_files_written: set[str] = field(default_factory=set)
    _trip_reason: str = ""

    # ── Properties ──

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    @property
    def is_tripped(self) -> bool:
        return bool(self._trip_reason)

    # ── Recording ──

    def record_tool_call(
        self, tool_name: str, params: dict[str, Any] | None = None
    ) -> None:
        """Record a tool call and check for macro loops."""
        if not self.config.enabled:
            return

        params = params or {}
        macro_type = _TOOL_TO_MACRO.get(tool_name, MacroActionType.OTHER)

        # Build detail string
        detail = ""
        if macro_type == MacroActionType.SPAWN_SUBAGENT:
            detail = params.get("subagent_type", params.get("description", ""))
        elif macro_type in (MacroActionType.READ_FILE, MacroActionType.WRITE_FILE):
            detail = params.get("path", params.get("file_path", ""))
        elif macro_type == MacroActionType.RUN_SHELL:
            cmd = params.get("command", params.get("cmd", ""))
            detail = str(cmd)[:60]

        record = MacroActionRecord(
            action_type=macro_type,
            tool_name=tool_name,
            detail=detail,
        )
        self._history.append(record)

        # Track progress indicators
        if macro_type == MacroActionType.READ_FILE and detail:
            self._distinct_files_read.add(detail)
        if macro_type == MacroActionType.WRITE_FILE and detail:
            self._distinct_files_written.add(detail)
            self._no_progress_count = 0
            # Clear history on write: pattern detection restarts after real progress
            self._history.clear()
        if macro_type in _PROGRESS_ACTIONS:
            self._no_progress_count = 0
        else:
            self._no_progress_count += 1

        # Trim history to window
        if len(self._history) > self.config.window_size * 2:
            self._history = self._history[-self.config.window_size:]

        # Check for macro loops
        self._check()

    def record_reflection(self, reason: str = "") -> None:
        """Record a reflection injection — breaks repetition patterns."""
        if not self.config.enabled:
            return
        self._history.clear()  # fresh start after reflection
        self._history.append(MacroActionRecord(
            action_type=MacroActionType.REFLECT,
            detail=reason[:80],
        ))
        self._no_progress_count = 0

    def record_finish(self) -> None:
        """Record a finish/complete call."""
        if not self.config.enabled:
            return
        self._history.clear()  # fresh start after finish
        self._history.append(MacroActionRecord(
            action_type=MacroActionType.FINISH,
        ))

    # ── Check ──

    def _check(self) -> bool:
        """Check for macro-loop patterns. Returns True if tripped."""
        if self._trip_reason:
            return True  # already tripped — don't overwrite reason

        if len(self._history) < self.config.window_size:
            return False

        recent = self._history[-self.config.window_size:]

        # ── Check 1: No-progress cycle ──
        if self._no_progress_count >= self.config.max_no_progress_cycles * 4:
            # ~4 macro actions per turn, so max_no_progress_cycles turns of no progress
            self._trip_reason = (
                f"Macro loop: {self._no_progress_count} macro actions without progress "
                f"(no writes, validates, or finishes). Distinct files read: "
                f"{len(self._distinct_files_read)}. Distinct files written: "
                f"{len(self._distinct_files_written)}."
            )
            logger.warning("MacroLoopDetector tripped: %s", self._trip_reason)
            return True

        # ── Check 2: Repeating pattern detection ──
        signatures = [r.signature() for r in recent]

        for pattern_len in range(
            self.config.min_pattern_length,
            self.config.max_pattern_length + 1,
        ):
            repetitions = self._count_pattern_repetitions(signatures, pattern_len)
            if repetitions >= self.config.min_repetitions:
                pattern_slice = signatures[-pattern_len:]
                # Skip single-action-type patterns — those are the local
                # loop detector's job. Macro detection focuses on multi-action
                # cycles (e.g., SPAWN → READ → SPAWN → READ).
                unique_actions = set(pattern_slice)
                if len(unique_actions) <= 1:
                    continue
                pattern_str = " → ".join(pattern_slice)
                self._trip_reason = (
                    f"Macro loop: pattern [{pattern_str}] repeated "
                    f"{repetitions}x in window of {len(recent)} actions."
                )
                logger.warning("MacroLoopDetector tripped: %s", self._trip_reason)
                return True

        # ── Check 3: Noise-tolerant pair frequency ──
        # Count how many times each 2-action signature pair appears in the
        # window, even non-consecutively. Catches patterns like:
        #   SPAWN → READ → (noise) → SPAWN → READ → (noise) → SPAWN → READ
        if self.config.noise_tolerant_min_occurrences > 0:
            trip = self._check_noise_tolerant(signatures)
            if trip:
                return True

        return False

    def _check_noise_tolerant(self, signatures: list[str]) -> bool:
        """Check if any 2-action pair appears frequently in the window,
        even when the occurrences are not consecutive.

        For window size 6, if SPAWN→READ appears 3 times, it means the
        agent has done this cycle 3 times in 6 actions — clearly looping.
        """
        if len(signatures) < self.config.noise_tolerant_min_occurrences * 2:
            return False

        # Count occurrences of each adjacent pair
        pair_counts: dict[str, int] = {}
        for i in range(len(signatures) - 1):
            pair = f"{signatures[i]}→{signatures[i + 1]}"
            pair_counts[pair] = pair_counts.get(pair, 0) + 1

        for pair, count in pair_counts.items():
            if count >= self.config.noise_tolerant_min_occurrences:
                # Ensure at least 2 distinct action types (skip same-action noise)
                actions = pair.split("→")
                if len(set(actions)) >= 2:
                    self._trip_reason = (
                        f"Macro loop (noise-tolerant): pair [{pair}] appeared "
                        f"{count}x in window of {len(signatures)} actions "
                        f"(threshold: {self.config.noise_tolerant_min_occurrences})."
                    )
                    logger.warning("MacroLoopDetector tripped: %s", self._trip_reason)
                    return True

        return False

    @staticmethod
    def _count_pattern_repetitions(
        signatures: list[str], pattern_len: int
    ) -> int:
        """Count how many times the trailing pattern repeats consecutively.

        E.g., signatures = [A, B, A, B, A, B], pattern_len = 2
        → trailing pattern = [A, B], repeats = 3
        """
        if len(signatures) < pattern_len * 2:
            return 0

        trailing = signatures[-pattern_len:]
        count = 1
        pos = len(signatures) - pattern_len * 2

        while pos >= 0:
            segment = signatures[pos:pos + pattern_len]
            if segment == trailing:
                count += 1
                pos -= pattern_len
            else:
                break

        return count

    # ── Reset ──

    def reset(self) -> None:
        """Reset all state."""
        self._history.clear()
        self._no_progress_count = 0
        self._distinct_files_read.clear()
        self._distinct_files_written.clear()
        self._trip_reason = ""

    # ── Serialization ──

    def to_summary(self) -> dict:
        """Export detector state for diagnostics."""
        signatures = [r.signature() for r in self._history[-8:]]
        return {
            "history_len": len(self._history),
            "recent_pattern": " → ".join(signatures) if signatures else "",
            "no_progress_count": self._no_progress_count,
            "distinct_files_read": len(self._distinct_files_read),
            "distinct_files_written": len(self._distinct_files_written),
            "trip_reason": self._trip_reason,
        }
