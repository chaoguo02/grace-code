"""Unit tests for agent/completion_guard.py — Task completion validation."""

import pytest
from agent.completion_guard import (
    CompletionCheckResult,
    CompletionContext,
    TaskCompletionGuard,
)
from agent.policy import CompletionPolicy


class TestCompletionContext:
    def test_tracks_file_reads(self):
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "src/app.py", True)
        assert ctx.had_any_read
        assert "src/app.py" in ctx.files_read

    def test_tracks_file_writes(self):
        ctx = CompletionContext()
        ctx.record_tool_result("file_write", "src/out.py", True)
        assert ctx.had_any_write
        assert "src/out.py" in ctx.files_written

    def test_does_not_count_failures(self):
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "src/missing.py", False)
        assert not ctx.had_any_read
        assert len(ctx.files_read) == 0
        assert ctx.total_tool_calls == 1
        assert ctx.total_successful_tool_calls == 0

    def test_tracks_total_calls(self):
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "a.py", True)
        ctx.record_tool_result("file_write", "b.py", False)
        ctx.record_tool_result("shell", None, True)
        assert ctx.total_tool_calls == 3
        assert ctx.total_successful_tool_calls == 2


class TestCompletionGuardEditTasks:
    def test_allows_completion_after_write(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_write", "src/x.py", True)
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            completion_policy=CompletionPolicy(require_any_write=True),
        )
        assert result.can_complete

    def test_blocks_completion_without_write(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        # No writes recorded
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            completion_policy=CompletionPolicy(require_any_write=True),
        )
        assert not result.can_complete
        assert "require_any_write" in result.blocked_reason.lower()
        assert "cannot finish" in result.inject_message.lower()

    def test_allows_without_write_when_policy_does_not_require(self):
        guard = TaskCompletionGuard(min_tool_calls_for_completion=0)
        ctx = CompletionContext()
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            completion_policy=CompletionPolicy(require_any_write=False),
        )
        assert result.can_complete

    def test_blocks_on_required_reads_missing(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "src/a.py", True)
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            completion_policy=CompletionPolicy(
                required_reads=frozenset({"src/a.py", "src/b.py"}),
            ),
        )
        assert not result.can_complete
        assert "src/b.py" in result.inject_message

    def test_passes_when_required_reads_satisfied(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "src/a.py", True)
        ctx.record_tool_result("file_read", "src/b.py", True)
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            completion_policy=CompletionPolicy(
                required_reads=frozenset({"src/a.py", "src/b.py"}),
            ),
        )
        assert result.can_complete


class TestCompletionGuardAnalysisTasks:
    def test_blocks_analysis_without_reads_when_required(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        result = guard.check(
            ctx=ctx,
            task_intent="analysis",
            completion_policy=CompletionPolicy(require_any_read=True),
        )
        assert not result.can_complete
        assert "require_any_read" in result.blocked_reason.lower()

    def test_allows_analysis_with_reads(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "src/app.py", True)
        result = guard.check(
            ctx=ctx,
            task_intent="analysis",
            completion_policy=CompletionPolicy(require_any_read=True),
        )
        assert result.can_complete

    def test_allows_analysis_without_reads_when_not_required(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        result = guard.check(
            ctx=ctx,
            task_intent="analysis",
            completion_policy=CompletionPolicy(require_any_read=False),
        )
        assert result.can_complete


class TestCompletionGuardPrematureCompletion:
    def test_blocks_zero_tool_calls(self):
        guard = TaskCompletionGuard(min_tool_calls_for_completion=1)
        ctx = CompletionContext()
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            current_step=1,
            task_max_steps=40,
        )
        assert not result.can_complete

    def test_allows_after_tool_calls(self):
        guard = TaskCompletionGuard(min_tool_calls_for_completion=1)
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "x.py", True)
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            current_step=2,
            task_max_steps=40,
        )
        assert result.can_complete

    def test_allows_premature_when_late_in_run(self):
        """If the model made it to step 30/40 with no tool calls,
        something else is wrong; don't block on premature completion."""
        guard = TaskCompletionGuard(
            min_tool_calls_for_completion=1,
            warn_premature_completion_at_step=3,
            warn_premature_completion_ratio=0.3,
        )
        ctx = CompletionContext()
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            current_step=30,
            task_max_steps=40,
        )
        assert result.can_complete  # past the warning threshold


class TestCompletionGuardIntegration:
    def test_multiple_checks_can_all_fail(self):
        """When multiple checks fail, the first one wins."""
        guard = TaskCompletionGuard(min_tool_calls_for_completion=1)
        ctx = CompletionContext()
        result = guard.check(
            ctx=ctx,
            task_intent="edit",
            current_step=1,
            task_max_steps=40,
            completion_policy=CompletionPolicy(
                require_any_write=True,
                require_any_read=True,
            ),
        )
        assert not result.can_complete
        # First check (CompletionPolicy require_any_read) should fire
        assert "read" in result.inject_message.lower()

    def test_write_resets_after_check(self):
        """After the guard blocks once and the model does the work,
        a subsequent check should pass."""
        guard = TaskCompletionGuard()
        ctx = CompletionContext()

        # First check: blocked (no write)
        r1 = guard.check(
            ctx=ctx, task_intent="edit",
            completion_policy=CompletionPolicy(require_any_write=True),
        )
        assert not r1.can_complete

        # Model does the work
        ctx.record_tool_result("file_write", "src/out.py", True)

        # Second check: allowed
        r2 = guard.check(
            ctx=ctx, task_intent="edit",
            completion_policy=CompletionPolicy(require_any_write=True),
        )
        assert r2.can_complete
