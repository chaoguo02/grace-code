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
    """Mutable context accumulated during the agent run, used by the guard."""

    files_read: set[str] = field(default_factory=set)
    """Normalized paths of files successfully read (file_read/file_view)."""

    files_written: set[str] = field(default_factory=set)
    """Normalized paths of files successfully written (file_write/file_edit)."""

    had_any_read: bool = False
    """True if at least one file_read or file_view succeeded."""

    had_any_write: bool = False
    """True if at least one file_write or file_edit succeeded."""

    total_tool_calls: int = 0
    """Total number of distinct tool-call actions executed."""

    total_successful_tool_calls: int = 0
    """Tool-call actions where at least one observation succeeded."""

    def record_tool_result(
        self, tool_name: str, path: str | None, success: bool
    ) -> None:
        """Record a single tool execution result. Call after each tool call."""
        self.total_tool_calls += 1
        if success:
            self.total_successful_tool_calls += 1
            if tool_name in ("file_read", "file_view"):
                self.had_any_read = True
                if path:
                    self.files_read.add(path)
            elif tool_name in ("file_write", "file_edit"):
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
        task_max_steps: int = 40,
        current_step: int = 1,
        completion_policy: "CompletionPolicy | None" = None,
    ) -> CompletionCheckResult:
        """Run all completion validation checks.

        Returns CompletionCheckResult — if can_complete=False, the inject_message
        MUST be added to the conversation and the loop must continue.
        """
        # ── Check 1: CompletionPolicy requirements ──
        if completion_policy is not None:
            result = self._check_completion_policy(ctx, completion_policy)
            if not result.can_complete:
                return result

        # ── Check 2: Premature completion (for edit tasks) ──
        if task_intent == "edit":
            result = self._check_premature_completion(ctx, current_step, task_max_steps)
            if not result.can_complete:
                return result

        # ── Check 3: Evidence-based completion (for analysis tasks) ──
        # Only enforce when CompletionPolicy requires reads, or the task
        # description explicitly targets files.
        require_evidence = (
            task_intent == "analysis"
            and (completion_policy is not None and completion_policy.require_any_read)
        )
        if require_evidence:
            result = self._check_analysis_evidence(ctx)
            if not result.can_complete:
                return result

        return CompletionCheckResult(can_complete=True)

    # ── Individual checks ────────────────────────────────────────────────

    def _check_completion_policy(
        self, ctx: CompletionContext, policy: "CompletionPolicy"
    ) -> CompletionCheckResult:
        """Validate CompletionPolicy requirements against accumulated context."""
        # Check require_any_read
        if policy.require_any_read and not ctx.had_any_read:
            return CompletionCheckResult(
                can_complete=False,
                blocked_reason="CompletionPolicy: require_any_read not satisfied",
                inject_message=(
                    "[SYSTEM] You cannot finish yet. The task policy requires "
                    "you to read at least one file before completing. "
                    "Use file_read or file_view to inspect the relevant files, "
                    "then call finish when you have evidence to support your conclusions."
                ),
            )

        # Check require_any_write
        if policy.require_any_write and not ctx.had_any_write:
            return CompletionCheckResult(
                can_complete=False,
                blocked_reason="CompletionPolicy: require_any_write not satisfied",
                inject_message=(
                    "[SYSTEM] You cannot finish yet. The task policy requires "
                    "you to write at least one file before completing. "
                    "Use file_write or file_edit to make the required changes, "
                    "then call finish."
                ),
            )

        # Check required_reads (specific paths)
        if policy.required_reads:
            missing = policy.required_reads - ctx.files_read
            if missing:
                return CompletionCheckResult(
                    can_complete=False,
                    blocked_reason=f"CompletionPolicy: required_reads not satisfied: {sorted(missing)}",
                    inject_message=(
                        f"[SYSTEM] You cannot finish yet. The task policy requires "
                        f"you to read these files before completing: {', '.join(sorted(missing))}. "
                        f"Use file_read to read each one, then call finish."
                    ),
                )

        # Check required_writes (specific paths)
        if policy.required_writes:
            missing = policy.required_writes - ctx.files_written
            if missing:
                return CompletionCheckResult(
                    can_complete=False,
                    blocked_reason=f"CompletionPolicy: required_writes not satisfied: {sorted(missing)}",
                    inject_message=(
                        f"[SYSTEM] You cannot finish yet. The task policy requires "
                        f"you to write these files before completing: {', '.join(sorted(missing))}. "
                        f"Use file_write or file_edit to modify each one, then call finish."
                    ),
                )

        return CompletionCheckResult(can_complete=True)

    def _check_premature_completion(
        self, ctx: CompletionContext, current_step: int, max_steps: int
    ) -> CompletionCheckResult:
        """Check if the model is trying to finish too early without doing work.

        For edit tasks: if very few steps taken relative to budget, and no edits
        were made, the model may be trying to skip work.
        """
        if current_step >= self._warn_step and current_step >= int(max_steps * self._warn_ratio):
            return CompletionCheckResult(can_complete=True)

        if ctx.total_tool_calls < self._min_tool_calls:
            return CompletionCheckResult(
                can_complete=False,
                blocked_reason="Premature completion: no tool calls made",
                inject_message=(
                    f"[SYSTEM] You are trying to finish without having made any "
                    f"tool calls. You must do actual work — read files, edit code, "
                    f"or run commands — before calling finish. "
                    f"Re-read the task description and take at least one concrete action."
                ),
            )

        return CompletionCheckResult(can_complete=True)

    def _check_analysis_evidence(
        self, ctx: CompletionContext
    ) -> CompletionCheckResult:
        """For analysis tasks: verify the agent actually read evidence.

        The model should not claim "I've analyzed X" without having read X.
        """
        if not ctx.had_any_read:
            return CompletionCheckResult(
                can_complete=False,
                blocked_reason="Analysis without evidence: no file reads performed",
                inject_message=(
                    "[SYSTEM] You are trying to finish an analysis task without "
                    "having read any files or searched any code. An analysis MUST "
                    "be based on evidence from the actual code. "
                    "Use file_read, file_view, or search_text to inspect the "
                    "relevant code, then call finish with evidence-backed findings."
                ),
            )

        return CompletionCheckResult(can_complete=True)
