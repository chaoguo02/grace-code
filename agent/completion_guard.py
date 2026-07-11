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

    tool_call_counts: dict[str, int] = field(default_factory=dict)
    """Per-tool call count: {'submit_findings': 2, 'file_read': 5, ...}.
    Used by CompletionGuard to enforce completion_requires contracts."""

    def record_tool_result(
        self, tool_name: str, path: str | None, success: bool
    ) -> None:
        """Record a single tool execution result. Call after each tool call."""
        self.total_tool_calls += 1
        self.tool_call_counts[tool_name] = self.tool_call_counts.get(tool_name, 0) + 1
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
        completion_requires: dict[str, int] | None = None,
    ) -> CompletionCheckResult:
        """Run all completion validation checks.

        completion_requires: per-tool minimum call counts. e.g.
        {"submit_findings": 1} means the agent MUST call submit_findings
        at least once before FINISH is accepted. Enforced by Runtime,
        not by prompt.

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

        # ── Check 3: Required tool calls (Runtime-enforced contract) ──
        if completion_requires:
            result = self._check_required_tools(ctx, completion_requires)
            if not result.can_complete:
                return result

        # ── Check 4: Evidence-based completion (for analysis tasks) ──
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
        """Validate that the agent did meaningful work toward the task goal.

        Claude Code Stop Hook pattern: completion is verified by checking
        "is the goal achieved?", not "did the agent perform action X?".

        Meaningful work = wrote files OR (read files + analyzed).
        The idempotent case (task already done in a prior run) is NOT a
        failure — it means the agent verified the goal is already met.
        """
        # ── Has the agent done meaningful work? ──
        _did_work = (
            ctx.had_any_write
            or ctx.had_any_read  # read + analyzed = meaningful, even if idempotent
        )

        if policy.require_any_read and not _did_work:
            return CompletionCheckResult(
                can_complete=False,
                blocked_reason="No meaningful work done (no reads, no writes)",
                inject_message=(
                    "[SYSTEM] You cannot finish yet — you have not done any "
                    "meaningful work. Read the relevant files, make changes, "
                    "or run commands, then call finish."
                ),
            )

        if policy.require_any_write and not _did_work:
            return CompletionCheckResult(
                can_complete=False,
                blocked_reason="No meaningful work done (no reads, no writes)",
                inject_message=(
                    "[SYSTEM] You cannot finish yet — you have not done any "
                    "meaningful work. Read the relevant files, make changes, "
                    "or run commands, then call finish."
                ),
            )

        # Log idempotent completion: agent read + verified, no writes needed
        if policy.require_any_write and not ctx.had_any_write and _did_work:
            logger.info(
                "Idempotent completion: agent read %d files, confirmed task "
                "already done. No redundant edits forced.",
                len(ctx.files_read),
            )

        # ── Specific file requirements (hard gates) ──
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

    def _check_required_tools(
        self, ctx: CompletionContext, required: dict[str, int]
    ) -> CompletionCheckResult:
        """Enforce Runtime contract: certain tools MUST be called before FINISH.

        This is NOT prompt-based advice — it's a hard gate. The model cannot
        unilaterally declare completion if required tools were never called.
        """
        missing = []
        for tool_name, min_count in required.items():
            actual = ctx.tool_call_counts.get(tool_name, 0)
            if actual < min_count:
                missing.append((tool_name, min_count, actual))

        if not missing:
            return CompletionCheckResult(can_complete=True)

        # Build a clear, structured error message
        missing_desc = "; ".join(
            f"{name} (need {need}, called {have})"
            for name, need, have in missing
        )
        tool_list = ", ".join(name for name, _, _ in missing)
        return CompletionCheckResult(
            can_complete=False,
            blocked_reason=f"Required tools not called: {missing_desc}",
            inject_message=(
                f"[SYSTEM] Cannot finish yet — required deliverables missing.\n"
                f"Required tool(s): {tool_list}\n"
                f"Details: {missing_desc}\n"
                f"You MUST call the required tool(s) before calling finish. "
                f"This is a Runtime-enforced contract, not optional."
            ),
        )
