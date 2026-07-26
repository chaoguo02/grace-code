"""
P1-2: Extract _finish_run from nested closure → ReActAgent._build_run_result().

Verifies:
  M1: _FinishRunContext dataclass constructed correctly
  M2: _build_run_result() produces RunResult via explicit ctx parameter
  M3: task_obs_closed mutates correctly (nonlocal semantics preserved)
"""

from unittest.mock import MagicMock, patch

from agent.core import ReActAgent
from agent.task import (
    RunStatus,
    TerminationReason,
    VerificationReason,
    VerificationStatus,
)


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
            tsm=MagicMock(termination_reason=None, verification_status=None,
                          verification_reason=None),
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
            tsm=MagicMock(termination_reason=None, verification_status=None,
                          verification_reason=None),
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

    def test_unverified_status_remains_metadata_not_answer_text(self):
        from agent.core import _FinishRunContext, _GitState

        git_state = _GitState()
        git_state.has_changes = True
        ctx = _FinishRunContext(
            git_state=git_state,
            task=MagicMock(task_id="t-unverified", repo_path="/tmp/r"),
            completion_ctx=MagicMock(had_any_write=True),
            tsm=MagicMock(
                termination_reason=TerminationReason.NONE,
                verification_status=VerificationStatus.UNAVAILABLE,
                verification_reason=VerificationReason.NO_TEST_ENVIRONMENT,
            ),
            reflection_counts={},
            get_consecutive_failures=lambda: 0,
            log=MagicMock(),
            task_obs=MagicMock(),
            task_context=MagicMock(),
        )
        agent = ReActAgent.__new__(ReActAgent)
        agent._accumulated_plan_contract = None
        agent._cfg = MagicMock(stats_collector=None)

        with patch("agent.core._refresh_git_state"):
            result = agent._build_run_result(
                status=RunStatus.SUCCESS,
                summary="The requested change is complete.",
                steps_taken=2,
                total_tokens_used=100,
                ctx=ctx,
            )

        assert result.summary == "The requested change is complete."
        assert result.verification_status is VerificationStatus.UNAVAILABLE
        assert result.verification_reason is VerificationReason.NO_TEST_ENVIRONMENT
        assert result.workspace_delta is not None
        assert result.workspace_delta.has_changes is True
        assert result.workspace_delta.is_run_scoped is False

    def test_finish_run_context_holds_all_fields(self):
        """_FinishRunContext has all expected fields (10 total — reference types only)."""
        from agent.core import _FinishRunContext, _GitState

        git_state = _GitState()
        ctx = _FinishRunContext(
            git_state=git_state,
            task=MagicMock(),
            completion_ctx=MagicMock(),
            tsm=MagicMock(),
            reflection_counts={},
            get_consecutive_failures=lambda: 0,
            log=MagicMock(),
            task_obs=MagicMock(),
            task_context=MagicMock(),
        )

        for field in ("git_state", "task", "completion_ctx",
                       "tsm", "reflection_counts",
                       "get_consecutive_failures", "log", "task_obs",
                       "task_context", "task_obs_closed"):
            assert hasattr(ctx, field), f"Missing field: {field}"
