"""Unit tests for agent/completion_guard.py — Git-fact-based completion validation."""

import pytest
from agent.completion_guard import CompletionCheckResult, CompletionContext, TaskCompletionGuard


class _FakeGitState:
    def __init__(self, has_changes=True, is_git_repo=True):
        self.has_changes = has_changes
        self.is_git_repo = is_git_repo


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

    def test_failures_are_fully_invisible(self):
        """Zero Trust: failed calls leave NO trace in progress state."""
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "src/missing.py", False)
        ctx.record_tool_result("shell", None, False)
        ctx.record_tool_result("file_write", "out.py", False)
        assert not ctx.had_any_read
        assert not ctx.had_any_write
        assert len(ctx.files_read) == 0
        assert len(ctx.files_written) == 0

    def test_total_calls_is_diagnostic_only(self):
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "a.py", True)
        ctx.record_tool_result("file_write", "b.py", False)
        ctx.record_tool_result("shell", None, True)
        assert ctx.total_tool_calls == 3

    def test_no_counters_exist(self):
        """Killed: tool_success_counts, tool_failure_counts, tool_call_counts."""
        ctx = CompletionContext()
        assert not hasattr(ctx, "tool_success_counts")
        assert not hasattr(ctx, "tool_failure_counts")
        assert not hasattr(ctx, "tool_call_counts")


class TestGitDiffGuard:
    """The only completion check: does git diff show changes?"""

    def test_edit_with_git_diff_passes(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_write", "src/x.py", True)
        git = _FakeGitState(has_changes=True, is_git_repo=True)
        result = guard.check(ctx=ctx, task_intent="edit", git_state=git)
        assert result.can_complete

    def test_edit_without_git_diff_blocks(self):
        """LLM wrote files but git diff shows nothing → blocked."""
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_write", "src/x.py", True)
        git = _FakeGitState(has_changes=False, is_git_repo=True)
        result = guard.check(ctx=ctx, task_intent="edit", git_state=git)
        assert not result.can_complete
        assert "no git diff evidence" in result.blocked_reason.lower()

    def test_analysis_passes_without_git_changes(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", "src/a.py", True)
        git = _FakeGitState(has_changes=False, is_git_repo=True)
        result = guard.check(ctx=ctx, task_intent="analysis", git_state=git)
        assert result.can_complete

    def test_no_writes_no_block(self):
        """Analysis with no writes — no diff needed."""
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        git = _FakeGitState(has_changes=False, is_git_repo=True)
        result = guard.check(ctx=ctx, task_intent="edit", git_state=git)
        assert result.can_complete

    def test_non_git_repo_passes(self):
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_write", "src/x.py", True)
        git = _FakeGitState(has_changes=False, is_git_repo=False)
        result = guard.check(ctx=ctx, task_intent="edit", git_state=git)
        assert result.can_complete  # can't check git in non-git repo

    def test_100_shell_calls_zero_diff_blocked(self):
        """LLM called shell 100 times successfully but no file changes → blocked.
        This is the key Zero Trust test: counts don't matter. Facts matter."""
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        for _ in range(100):
            ctx.record_tool_result("shell", None, True)  # all "successful"
        ctx.record_tool_result("file_write", "src/x.py", True)
        git = _FakeGitState(has_changes=False, is_git_repo=True)
        result = guard.check(ctx=ctx, task_intent="edit", git_state=git)
        assert not result.can_complete
