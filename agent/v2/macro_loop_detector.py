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

# Macro actions that indicate forward progress.
# For analysis tasks, READING NEW FILES and SEARCHING are also progress.
# The detector checks task_intent to decide which metric to use.
_PROGRESS_ACTIONS_EDIT = frozenset({
    MacroActionType.WRITE_FILE,
    MacroActionType.VALIDATE,
    MacroActionType.FINISH,
})

# Analysis tasks: reading/searching new territory IS forward progress.
# Re-reading the same files or repeating the same searches is not.
_PROGRESS_ACTIONS_ANALYSIS = frozenset({
    MacroActionType.WRITE_FILE,
    MacroActionType.VALIDATE,
    MacroActionType.FINISH,
    MacroActionType.READ_FILE,    # reading NEW files = progress
    MacroActionType.SEARCH_CODE,  # searching = progress
})

# How many distinct "discoveries" count as one progress reset.
# Prevents the detector from being too lenient (every new file = reset).
_MIN_NEW_FILES_FOR_PROGRESS = 3


# ---------------------------------------------------------------------------
# MacroActionRecord
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MacroActionRecord:
    action_type: MacroActionType
    tool_name: str = ""
    detail: str = ""  # short description (file path, subagent name, etc.)

    def signature(self) -> str:
        """A compact string key for fingerprint matching. Includes payload."""
        if self.detail:
            return f"{self.action_type.value}:{self.tool_name}:{self.detail}"
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
    """Maximum macro-action cycles without any progress action (×4 actions).

    For analysis tasks (read-only), this is effectively disabled — analysis
    tasks never produce writes, so no_progress is expected behavior.
    Set analysis_no_progress_multiplier to increase tolerance.
    """

    analysis_no_progress_multiplier: int = 5
    """For analysis tasks: multiply max_no_progress_cycles by this factor.
    Analysis tasks don't write files, so the no_progress check is much
    looser (default: 2*5=10 cycles, ~40 macro actions)."""

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
    task_intent: Any = None  # TaskIntent — injected by _run_body()

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

        # ── Intent-aware progress tracking ──
        if macro_type == MacroActionType.READ_FILE and detail:
            _is_new = detail not in self._distinct_files_read
            self._distinct_files_read.add(detail)
        else:
            _is_new = False

        if macro_type == MacroActionType.WRITE_FILE and detail:
            self._distinct_files_written.add(detail)

        # Determine which progress metric to use based on intent
        _is_analysis = (
            self.task_intent is not None and self.task_intent.is_analysis
        )
        if _is_analysis:
            # Analysis: reading NEW files or searching = progress.
            # Re-reading the same files = NOT progress (confirmation loop).
            _made_progress = (
                macro_type in _PROGRESS_ACTIONS_ANALYSIS
                and (macro_type not in (MacroActionType.READ_FILE,) or _is_new
                     or len(self._distinct_files_read) % _MIN_NEW_FILES_FOR_PROGRESS == 0)
            )
        else:
            # Edit or unknown intent: writes, validates, finishes = hard progress.
            # Reading NEW files = soft progress ONLY if intent is explicitly edit
            # (agent is exploring before editing). Unknown intent keeps old behavior.
            _made_progress = (
                macro_type in _PROGRESS_ACTIONS_EDIT
                or (self.task_intent is not None
                    and not self.task_intent.is_analysis
                    and macro_type == MacroActionType.READ_FILE
                    and _is_new)
            )

        if _made_progress:
            self._no_progress_count = 0
            if macro_type == MacroActionType.WRITE_FILE:
                self._history.clear()  # write = hard reset
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

        # ── Check 1: No-progress cycle (intent-aware) ──
        # Progress is defined per intent — edit tasks need writes, analysis
        # tasks count reading NEW files as progress. The multiplier hack is
        # gone: if the agent is truly stuck (re-reading same files, no new
        # discoveries), this fires for both edit AND analysis.
        _no_progress_limit = self.config.max_no_progress_cycles * 4
        if self._no_progress_count >= _no_progress_limit:
            _is_analysis = (
                self.task_intent is not None and self.task_intent.is_analysis
            )
            _kind = "analysis" if _is_analysis else "edit"
            self._trip_reason = (
                f"Macro loop ({_kind}): {self._no_progress_count} macro actions "
                f"without progress. Distinct files read: "
                f"{len(self._distinct_files_read)}. Distinct files written: "
                f"{len(self._distinct_files_written)}."
            )
            logger.warning("MacroLoopDetector tripped: %s", self._trip_reason)
            return True

        # ── Check 2: Exact payload fingerprint loops ──
        # Catches two patterns:
        #   a) Same action (type+payload) repeated consecutively (鬼打墙)
        #   b) Alternating pair A→B→A→B repeated (spawn→read→spawn→read)
        # Does NOT catch READ(A)→SEARCH(B)→READ(C)→SEARCH(D) — normal exploration.
        #
        # CRITICAL: only trip the fingerprint check when there is NO progress.
        # Consecutive identical tool calls during active exploration (new files
        # being read, distinct searches) are NOT a loop — they're the agent
        # being thorough. The no_progress_count gates this: the agent must
        # have made ZERO progress actions for at least min_repetitions cycles.
        if len(recent) >= self.config.min_repetitions and self._no_progress_count >= self.config.min_repetitions:
            fingerprints = [r.signature() for r in recent]

            # a) Consecutive identical: last N fingerprints all the same
            _last_n = fingerprints[-self.config.min_repetitions:]
            if len(set(_last_n)) == 1:
                self._trip_reason = (
                    f"Macro loop (exact): [{_last_n[0]}] "
                    f"repeated {self.config.min_repetitions}x consecutively "
                    f"(no progress for {self._no_progress_count} actions)."
                )
                logger.warning("MacroLoopDetector tripped: %s", self._trip_reason)
                return True

            # b) Alternating pair: A→B→A→B repeated (e.g. spawn→read→spawn→read)
            _alt_len = self.config.min_repetitions * 2
            if len(fingerprints) >= _alt_len:
                _alt = fingerprints[-_alt_len:]
                _a, _b = _alt[0], _alt[1]
                if _a != _b and all(
                    _alt[i] == _a and _alt[i+1] == _b
                    for i in range(0, _alt_len, 2)
                ):
                    self._trip_reason = (
                        f"Macro loop (alternating): [{_a}] ↔ [{_b}] "
                        f"repeated {self.config.min_repetitions}x "
                        f"(no progress for {self._no_progress_count} actions)."
                    )
                    logger.warning("MacroLoopDetector tripped: %s", self._trip_reason)
                    return True

        return False

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
