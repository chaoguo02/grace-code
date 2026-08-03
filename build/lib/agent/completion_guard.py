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
import os as _os
from dataclasses import dataclass, field, replace
from typing import Any, TYPE_CHECKING, Protocol

from agent.task import TaskIntent


class GitStateLike(Protocol):
    """Minimal interface for agent/core.py's _GitState consumed by completion_guard.

    Declaring this Protocol avoids a circular import between completion_guard
    and agent/core.py while still giving type-checkers visibility into the
    fields this module actually reads.
    """
    is_git_repo: bool
    has_changes: bool
    files_changed: set[str]
    current_diff: str
    _baseline_dirty_files: set[str]
from core.base import ToolEffect, ToolMetadata

if TYPE_CHECKING:
    from collections.abc import Callable
    from core.policy import CompletionPolicy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CompletionContext — accumulated during the main loop
# ---------------------------------------------------------------------------

@dataclass
class CompletionContext:
    """Mutable context accumulated during the agent run.

    Zero Trust: no counters. Progress is measured by FACTS (files changed
    on disk, git diff evidence), not by "how many times did we call tool X."
    The before/after workspace revision is the source of truth for completion.
    """

    files_read: set[str] = field(default_factory=set)
    files_written: set[str] = field(default_factory=set)
    had_any_read: bool = False
    had_any_write: bool = False
    total_tool_calls: int = 0  # diagnostic only, never used for decisions
    def record_tool_result(
        self,
        tool_name: str,
        metadata: ToolMetadata | None,
        path: str | None,
        success: bool,
    ) -> None:
        """Record file-level facts. Failed calls leave no trace."""
        self.total_tool_calls += 1
        if not success:
            return  # ← failure is invisible. No counter. No state change.

        if metadata is None:
            return
        if ToolEffect.READ_WORKSPACE in metadata.effects:
            self.had_any_read = True
            if path:
                self.files_read.add(path)
        if ToolEffect.WRITE_WORKSPACE in metadata.effects:
            self.had_any_write = True
            if path:
                self.files_written.add(path)


# ---------------------------------------------------------------------------
# CompletionCheckResult
# ---------------------------------------------------------------------------

@dataclass
class CompletionCheckResult:
    """Result of a pre-completion validation check.

    Three verdicts (grace build-mode pattern):
    - DONE:   Task is complete, proceed to FINISH.
    - RETRY:  Blocked with specific feedback. Model should retry with guidance.
    - ABORT:  Cannot be completed. Model should give up with the reason.
    """

    can_complete: bool = True
    verdict: str = "done"  # "done" | "retry" | "abort"
    blocked_reason: str = ""
    inject_message: str = ""
    """If blocked, this message is injected into the conversation to guide the model."""

    @classmethod
    def retry(cls, feedback: str, reason: str = "") -> "CompletionCheckResult":
        """Blocked with structured feedback — model should retry."""
        return cls(
            can_complete=False, verdict="retry",
            blocked_reason=reason or feedback,
            inject_message=feedback,
        )

    @classmethod
    def abort(cls, reason: str, detail: str = "") -> "CompletionCheckResult":
        """Cannot be completed — model should give up."""
        msg = f"[VERIFY ABORT] {reason}"
        if detail:
            msg += f"\n{detail}"
        return cls(
            can_complete=False, verdict="abort",
            blocked_reason=reason,
            inject_message=msg,
        )

    @classmethod
    def done(cls) -> "CompletionCheckResult":
        """Task is complete."""
        return cls(can_complete=True, verdict="done")


# ---------------------------------------------------------------------------
# TaskCompletionGuard
# ---------------------------------------------------------------------------

class TaskCompletionGuard:
    """Runtime-validated task completion — model cannot unilaterally declare done.

    Supports three verdicts:
    - DONE:   proceed to FINISH.
    - RETRY:  inject feedback, model retries with guidance.
    - ABORT:  force give_up, task cannot be completed.

    Usage:
        guard = TaskCompletionGuard()
        result = guard.check(
            event_log=log,
            task_intent="edit",
            completion_policy=policy.completion,
            verify_callback=user_verify_fn,  # optional per-task verifier
        )
        if result.verdict == "retry":
            history.add(LLMMessage(role="user", content=result.inject_message))
            continue
        elif result.verdict == "abort":
            history.add(LLMMessage(role="user", content=result.inject_message))
            action.action_type = ActionType.GIVE_UP
            break
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
        task_intent: TaskIntent | str = TaskIntent.EDIT,
        git_state: GitStateLike | None = None,
        verify_callback: "Callable[[], CompletionCheckResult] | None" = None,
        mode_policy: Any = None,
        evidence_requirements: Any = None,
        evidence_store: Any = None,
        **kwargs,  # absorb deprecated params silently
    ) -> CompletionCheckResult:
        """Run all completion validation checks against FACTS, not counters.

        The only question for edit tasks: does git diff show the expected changes?
        An optional verify_callback runs LAST — it can override all built-in checks.
        """
        # ── Built-in checks ──
        typed_intent = TaskIntent(task_intent)
        if typed_intent is TaskIntent.EDIT and git_state is not None and git_state.is_git_repo:
            if ctx.had_any_write and not git_state.has_changes:
                _written = sorted(ctx.files_written) if ctx.files_written else ["(none)"]
                return CompletionCheckResult.retry(
                    feedback=(
                        f"[RUNTIME BLOCK] Expected files to be modified: {', '.join(_written)}. "
                        f"The current workspace revision equals the run baseline — "
                        f"no net file changes were detected on disk. "
                        f"Read each file you intended to modify and confirm your edits "
                        f"actually persisted to the filesystem, then call finish."
                    ),
                    reason="No workspace revision delta",
                )
            # When the workspace was already dirty at baseline, has_changes is
            # trivially True even if the agent's writes didn't persist.  Verify
            # that the agent's files actually appear in the INCREMENTAL diff:
            #   delta = current_dirty_files - baseline_dirty_files
            # Only files that became dirty DURING this run count as agent work.
            if ctx.had_any_write and git_state.has_changes and ctx.files_written:
                _changed = git_state.files_changed
                if _changed:
                    _run_changed = getattr(git_state, "_run_changed_files", None)
                    _baseline_dirty = getattr(git_state, '_baseline_dirty_files', set())
                    _incremental = (
                        set(_run_changed)
                        if _run_changed is not None
                        else _changed - _baseline_dirty
                    )
                    # If nothing changed incrementally, the agent's writes had no
                    # net effect on the working tree — block.
                    if not _incremental:
                        _agent_files = {f for f in ctx.files_written if f}
                        return CompletionCheckResult.retry(
                            feedback=(
                                f"[RUNTIME BLOCK] Files written ({', '.join(sorted(_agent_files)[:5])}) "
                                f"produced no new changes beyond the pre-run baseline. "
                                f"Verify your edits actually persisted to disk, "
                                f"then call finish."
                            ),
                            reason="Agent files produced no incremental diff",
                        )
                    # Check overlap against incremental delta only
                    _agent_files = {f for f in ctx.files_written if f}
                    _inc_basenames = {
                        _os.path.basename(f.replace("\\", "/")) for f in _incremental
                    }
                    _agent_basenames = {
                        _os.path.basename(f.replace("\\", "/")) for f in _agent_files
                    }
                    _overlap = bool(_agent_basenames & _inc_basenames)
                    if not _overlap:
                        return CompletionCheckResult.retry(
                            feedback=(
                                f"[RUNTIME BLOCK] Files written ({', '.join(sorted(_agent_files)[:5])}) "
                                f"do not appear in the incremental git diff "
                                f"({', '.join(sorted(_incremental)[:5])}). "
                                f"Verify your edits actually persisted to disk, "
                                f"then call finish."
                            ),
                            reason="Agent files missing from workspace diff",
                        )

        # ── Evidence requirements gate ──
        if evidence_store is not None and evidence_requirements is not None:
            effective_requirements = evidence_requirements
            verification_requirement = getattr(
                evidence_requirements,
                "verification_requirement",
                "not_required",
            )
            if verification_requirement == "required_if_workspace_changed":
                has_workspace_changes = (
                    (
                        git_state is not None
                        and git_state.is_git_repo
                        and git_state.has_changes
                    )
                    or bool(ctx.had_any_write)
                )
                effective_requirements = replace(
                    evidence_requirements,
                    verification_requirement=(
                        "required" if has_workspace_changes else "not_required"
                    ),
                )
            if (
                not effective_requirements.is_empty
                or evidence_store.count > 0
            ):
                evaluation = evidence_store.evaluate(effective_requirements)
                if not evaluation.satisfied:
                    missing = [
                        f"{item.tool or item.code}({item.code})"
                        for item in evaluation.missing
                    ]
                    failures = [
                        f"{item.kind}:{item.reason}"
                        for item in evaluation.failed
                    ]
                    retryable = all(
                        item.retryable
                        for item in (*evaluation.missing, *evaluation.failed)
                    )
                    if not retryable:
                        return CompletionCheckResult.abort(
                            reason="required_evidence_not_satisfied",
                            detail="; ".join((*missing, *failures)),
                        )
                    return CompletionCheckResult.retry(
                        feedback=(
                            "[RUNTIME BLOCK] Required evidence is not satisfied: "
                            f"{'; '.join((*missing, *failures))}. "
                            "Complete or repeat the required operations before finishing."
                        ),
                        reason=(
                            "required_evidence_not_satisfied: "
                            f"{len(evaluation.missing)} missing, "
                            f"{len(evaluation.failed)} failed"
                        ),
                    )

        # ── Per-task verify callback (highest priority) ──
        # Supports both () -> CompletionCheckResult and
        # (CompletionContext) -> CompletionCheckResult signatures.
        # Signature is detected once and cached on the callback.
        if verify_callback is not None:
            _takes_ctx = getattr(verify_callback, '_takes_ctx', None)
            if _takes_ctx is None:
                import inspect as _inspect
                try:
                    _takes_ctx = len(_inspect.signature(verify_callback).parameters) > 0
                except (ValueError, TypeError):
                    _takes_ctx = False
                try:
                    verify_callback._takes_ctx = _takes_ctx  # type: ignore[attr-defined]
                except (TypeError, AttributeError):
                    pass
            callback_result = verify_callback(ctx) if _takes_ctx else verify_callback()
            if not callback_result.can_complete:
                return callback_result

        return CompletionCheckResult.done()

    # ── The only check that matters ──
    # Git diff is the World Model. No counters. No heuristics. Just facts.
