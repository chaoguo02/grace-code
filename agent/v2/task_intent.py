"""Task Intent — structured execution intent for the Runtime layer.

Claude Code Intent Layer pattern: the agent's mission is NOT a string.
It's a typed object that the Runtime (MacroLoopDetector, CompletionGuard,
PermissionPipeline) uses to make decisions without guessing.

Before this: MacroLoopDetector used a string "edit"/"analysis" that was
never wired. Progress was defined only by file writes — fatal for Plan agent.

After this: TaskIntent is created at the entry layer, injected into
RuntimeController and MacroLoopDetector at construction time. Every
Runtime component knows EXACTLY what progress means for this task.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IntentPhase(str, Enum):
    ANALYSIS = "analysis"    # read, search, explore — writes are NOT progress
    EXECUTION = "execution"  # edit, write, test — only writes/validates count


@dataclass(frozen=True)
class TaskIntent:
    """Immutable execution intent. Created by the entry layer, enforced
    by the Runtime. The agent has no opportunity to override.

    phase: ANALYSIS → reading new files = progress. EXECUTION → writes = progress.
    """

    phase: IntentPhase = IntentPhase.EXECUTION

    # ── Factory presets ──────────────────────────────────────────────

    @classmethod
    def for_plan(cls) -> "TaskIntent":
        """Plan agent: analysis phase — exploration IS progress."""
        return cls(phase=IntentPhase.ANALYSIS)

    @classmethod
    def for_build(cls) -> "TaskIntent":
        """Build agent: execution phase — only writes count."""
        return cls(phase=IntentPhase.EXECUTION)

    @classmethod
    def from_string(cls, value: str) -> "TaskIntent":
        """Backward-compatible: "analysis"/"edit" → TaskIntent."""
        if value == "analysis":
            return cls.for_plan()
        return cls.for_build()

    # ── Queries ──────────────────────────────────────────────────────

    @property
    def is_analysis(self) -> bool:
        return self.phase == IntentPhase.ANALYSIS

    @property
    def is_execution(self) -> bool:
        return self.phase == IntentPhase.EXECUTION
