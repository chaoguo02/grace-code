"""
P1-2: Extract _finish_run from nested closure → ReActAgent._build_run_result().

Verifies:
  M1: _FinishRunContext dataclass constructed correctly
  M2: _build_run_result() produces RunResult via explicit ctx parameter
  M3: task_obs_closed mutates correctly (nonlocal semantics preserved)
"""

from unittest.mock import MagicMock

from agent.core import ReActAgent


# ────────────────────────────────────────────────────────────────────────────
# M1 + M2: _FinishRunContext + _build_run_result()
# ────────────────────────────────────────────────────────────────────────────

class TestBuildRunResult:
    """Verify the extracted method produces correct RunResult."""

    def test_build_run_result_success(self):
        """_build_run_result() with basic ctx → RunResult with correct fields."""
        from agent.core import _FinishRunContext, _GitState

        # Minimal context
        git_state = _GitState()
        ctx = _FinishRunContext(
            git_state=git_state,
            task=MagicMock(task_id="task-abc", repo_path="/tmp/repo"),
            completion_ctx=MagicMock(had_any_write=False),
            verification_ok=True,
            tsm=MagicMock(termination_reason=None, verification_status=None,
                          verification_reason=None),
            completion_blocked=0,
            reflection_counts={},
            get_consecutive_failures=lambda: 0,
            log=MagicMock(),
            task_obs=MagicMock(),
            task_context=MagicMock(),
        )

        # Create minimal agent instance
        agent = ReActAgent.__new__(ReActAgent)
        agent._accumulated_plan_contract = None
        mock_cfg = MagicMock(stats_collector=None)
        agent._cfg = mock_cfg

        result = agent._build_run_result(
            status=MagicMock(value="completed"),
            summary="All done.",
            steps_taken=3,
            total_tokens_used=1500,
            ctx=ctx,
        )

        assert result.task_id == "task-abc"
        assert result.summary == "All done."
        assert result.steps_taken == 3
        assert result.total_tokens == 1500
        assert result.contract is None

    def test_build_run_result_sets_task_obs_closed(self):
        """_build_run_result() sets ctx.task_obs_closed = True on first call."""
        from agent.core import _FinishRunContext, _GitState

        git_state = _GitState()
        ctx = _FinishRunContext(
            git_state=git_state,
            task=MagicMock(task_id="t2", repo_path="/tmp/r"),
            completion_ctx=MagicMock(had_any_write=False),
            verification_ok=True,
            tsm=MagicMock(termination_reason=None, verification_status=None,
                          verification_reason=None),
            completion_blocked=0,
            reflection_counts={},
            get_consecutive_failures=lambda: 0,
            log=MagicMock(),
            task_obs=MagicMock(),
            task_context=MagicMock(),
        )

        agent = ReActAgent.__new__(ReActAgent)
        agent._accumulated_plan_contract = None
        mock_cfg = MagicMock(stats_collector=None)
        agent._cfg = mock_cfg

        assert ctx.task_obs_closed is False
        agent._build_run_result(
            status=MagicMock(value="completed"),
            summary="ok", steps_taken=1, total_tokens_used=100, ctx=ctx,
        )
        assert ctx.task_obs_closed is True

    def test_finish_run_context_holds_all_fields(self):
        """_FinishRunContext has all 12 expected fields."""
        from agent.core import _FinishRunContext, _GitState

        git_state = _GitState()
        ctx = _FinishRunContext(
            git_state=git_state,
            task=MagicMock(),
            completion_ctx=MagicMock(),
            verification_ok=False,
            tsm=MagicMock(),
            completion_blocked=0,
            reflection_counts={},
            get_consecutive_failures=lambda: 0,
            log=MagicMock(),
            task_obs=MagicMock(),
            task_context=MagicMock(),
        )

        for field in ("git_state", "task", "completion_ctx", "verification_ok",
                       "tsm", "completion_blocked", "reflection_counts",
                       "get_consecutive_failures", "log", "task_obs",
                       "task_context", "task_obs_closed"):
            assert hasattr(ctx, field), f"Missing field: {field}"
