"""Task completion guard — Runtime validates before accepting model FINISH.

Claude Code pattern (query.ts): tool execution finishing does NOT mean the task
is done. After each iteration, the system checks: any oversized output to compress?
any pending queued instructions? any interception hooks? Only when ALL conditions
are met ("no incomplete tools, no context anomalies, no interception errors,
no pending progress, no budget constraints") is the task truly complete.

This module prevents the model from unilaterally declaring "I'm done" via
natural language. The Runtime MUST validate completion conditions first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.policy import CompletionPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CompletionContext — accumulated during the main loop
# ---------------------------------------------------------------------------

@dataclass
class CompletionContext:
    """Mutable context accumulated during the agent run.

    Zero Trust: no counters. Progress is measured by FACTS (files changed
    on disk, git diff evidence), not by "how many times did we call tool X."
    The GitState is the World Model — the only source of truth for completion.
    """

    files_read: set[str] = field(default_factory=set)
    files_written: set[str] = field(default_factory=set)
    had_any_read: bool = False
    had_any_write: bool = False
    total_tool_calls: int = 0  # diagnostic only, never used for decisions

    def record_tool_result(
        self, tool_name: str, path: str | None, success: bool
    ) -> None:
        """Record file-level facts. Failed calls leave no trace."""
        self.total_tool_calls += 1
        if not success:
            return  # ← failure is invisible. No counter. No state change.

        if tool_name in ("file_read", "file_view"):
            self.had_any_read = True
            if path:
                self.files_read.add(path)
        elif tool_name in ("file_write", "file_edit", "submit_findings"):
            self.had_any_write = True
            if path:
                self.files_written.add(path)


# ---------------------------------------------------------------------------
# CompletionCheckResult
# ---------------------------------------------------------------------------

@dataclass
class CompletionCheckResult:
    """Result of a pre-completion validation check."""

    can_complete: bool = True
    blocked_reason: str = ""
    inject_message: str = ""
    """If blocked, this message is injected into the conversation to guide the model."""


# ---------------------------------------------------------------------------
# TaskCompletionGuard
# ---------------------------------------------------------------------------

class TaskCompletionGuard:
    """Runtime-validated task completion — model cannot unilaterally declare done.

    Usage:
        guard = TaskCompletionGuard()
        result = guard.check(
            event_log=log,
            task_intent="edit",
            completion_policy=policy.completion,
        )
        if not result.can_complete:
            history.add(LLMMessage(role="user", content=result.inject_message))
            continue  # back to main loop
    """

    def __init__(
        self,
        *,
        min_tool_calls_for_completion: int = 1,
        warn_premature_completion_at_step: int = 3,
        warn_premature_completion_ratio: float = 0.3,
    ) -> None:
        self._min_tool_calls = min_tool_calls_for_completion
        self._warn_step = warn_premature_completion_at_step
        self._warn_ratio = warn_premature_completion_ratio

    def check(
        self,
        *,
        ctx: CompletionContext,
        task_intent: str = "edit",
        git_state: Any = None,
        completion_requires: dict[str, int] | None = None,
        **kwargs,  # absorb deprecated params silently
    ) -> CompletionCheckResult:
        """Run all completion validation checks against FACTS, not counters.

        The only question for edit tasks: does git diff show the expected changes?
        For subagents with completion_requires: did files get written?
        No amount of "tool call counts" can answer these questions.
        """
        # ── Git Diff Gate: the World Model verdict ──
        if task_intent == "edit" and git_state is not None and git_state.is_git_repo:
            if ctx.had_any_write and not git_state.has_changes:
                # Build fact-based injection: what was expected, what actually happened
                _written = sorted(ctx.files_written) if ctx.files_written else ["(none)"]
                return CompletionCheckResult(
                    can_complete=False,
                    blocked_reason="No git diff evidence of changes",
                    inject_message=(
                        f"[RUNTIME BLOCK] Expected files to be modified: {', '.join(_written)}. "
                        f"Current git diff is EMPTY — no file changes detected on disk. "
                        f"This is an OS-level fact, not a judgment. "
                        f"Read each file you intended to modify and confirm your edits "
                        f"actually persisted to the filesystem, then call finish."
                    ),
                )

        # ── Required deliverables (subagent contracts, not counters) ──
        if completion_requires:
            for tool_name, _min_count in completion_requires.items():
                if tool_name == "submit_findings":
                    # submit_findings writes to artifact store — check had_any_write
                    if not ctx.had_any_write:
                        return CompletionCheckResult(
                            can_complete=False,
                            blocked_reason=f"Required deliverable '{tool_name}' not produced",
                            inject_message=(
                                f"[SYSTEM] Cannot finish yet — you must call "
                                f"'{tool_name}' to submit your findings before finishing."
                            ),
                        )

        return CompletionCheckResult(can_complete=True)

    # ── The only check that matters ──
    # Git diff is the World Model. No counters. No heuristics. Just facts.
