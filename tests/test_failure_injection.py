"""Failure injection tests (Phase 15 Issue 04).

Intentionally trigger permission rejection, tool failure, and budget
exhaustion, then verify:
1. The harness preserves its boundary (correct RunStatus)
2. The outcome is recorded correctly (TerminationReason)
3. The failure taxonomy classifies the outcome consistently
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.runtime_controller import RuntimeController, StepAction
from agent.task import RunStatus, TerminationReason
from observability.failure_policy import (
    BoundaryBehavior,
    FailureCategory,
    classify_termination,
    verify_boundary_preserved,
)


class TestBudgetExhaustionInjection:
    """Inject budget exhaustion and verify boundary."""

    def test_budget_exhausted_produces_gave_up_status(self):
        from agent.session.execution_budget import ExecutionBudget

        budget = ExecutionBudget()
        budget.start()
        budget.exhaust("injected for test")

        controller = RuntimeController(budget=budget)
        decision = controller.check(
            step=1, total_tokens=0,
            history=MagicMock(), log=MagicMock(),
        )

        assert decision.action is StepAction.TERMINATE
        assert decision.terminate_reason == TerminationReason.BUDGET_EXHAUSTED

        # Verify boundary preserved
        passed, msg = verify_boundary_preserved(
            decision.terminate_reason,
            decision.terminate_status,
        )
        assert passed, msg

    def test_budget_exhaustion_classified_as_resource_exhausted(self):
        policy = classify_termination(TerminationReason.BUDGET_EXHAUSTED)
        assert policy.category == FailureCategory.RESOURCE_EXHAUSTED
        assert policy.behavior == BoundaryBehavior.HALT_IMMEDIATELY

    def test_budget_exhaustion_boundary_violated_when_success(self):
        passed, msg = verify_boundary_preserved(
            TerminationReason.BUDGET_EXHAUSTED, RunStatus.SUCCESS,
        )
        assert not passed
        assert "boundary violation" in msg


class TestMaxStepsInjection:
    """Inject max steps reached and verify boundary."""

    def test_max_steps_produces_max_steps_status(self):
        from agent.session.execution_budget import ExecutionBudget

        budget = ExecutionBudget()
        budget.start()

        controller = RuntimeController(budget=budget, max_steps=5)
        decision = controller.check(
            step=5, total_tokens=0,
            history=MagicMock(), log=MagicMock(),
        )

        assert decision.action is StepAction.TERMINATE
        assert decision.terminate_reason == TerminationReason.MAX_STEPS
        assert decision.terminate_status == RunStatus.MAX_STEPS

        passed, msg = verify_boundary_preserved(
            decision.terminate_reason,
            decision.terminate_status,
        )
        assert passed, msg

    def test_max_steps_boundary_violated_when_success(self):
        passed, msg = verify_boundary_preserved(
            TerminationReason.MAX_STEPS, RunStatus.SUCCESS,
        )
        assert not passed


class TestToolFailureInjection:
    """Inject consecutive tool failures and verify boundary."""

    def test_consecutive_failures_trigger_termination(self):
        controller = RuntimeController(max_consecutive_failures=3)
        decision = controller.check(
            step=2, total_tokens=0,
            history=MagicMock(), log=MagicMock(),
            consecutive_failures=3,
        )

        assert decision.action is StepAction.TERMINATE
        assert decision.terminate_reason == TerminationReason.TOOL_FAILURE_LIMIT

        passed, msg = verify_boundary_preserved(
            decision.terminate_reason, RunStatus.GAVE_UP,
        )
        assert passed, msg

    def test_tool_failure_classified_correctly(self):
        policy = classify_termination(TerminationReason.TOOL_FAILURE_LIMIT)
        assert policy.category == FailureCategory.TOOL_FAILURE
        assert policy.behavior == BoundaryBehavior.HALT_IMMEDIATELY
        assert policy.max_recovery_attempts == 0


class TestCircuitBreakerInjection:
    """Inject circuit breaker trip and verify boundary."""

    def test_circuit_breaker_produces_gave_up(self):
        breaker = MagicMock()
        breaker.check.return_value = True
        breaker.trip_reason = "permission denial limit"

        controller = RuntimeController(breaker=breaker)
        decision = controller.check(
            step=1, total_tokens=0,
            history=MagicMock(), log=MagicMock(),
        )

        assert decision.action is StepAction.TERMINATE
        assert decision.terminate_reason == TerminationReason.CIRCUIT_BREAKER

        passed, msg = verify_boundary_preserved(
            decision.terminate_reason, RunStatus.GAVE_UP,
        )
        assert passed, msg

    def test_circuit_breaker_no_recovery_allowed(self):
        policy = classify_termination(TerminationReason.CIRCUIT_BREAKER)
        assert policy.max_recovery_attempts == 0
        assert policy.behavior == BoundaryBehavior.HALT_IMMEDIATELY


class TestPermissionRejectionInjection:
    """Inject guard rejection and verify boundary."""

    def test_guard_rejected_classified_as_authority_denied(self):
        policy = classify_termination(TerminationReason.GUARD_REJECTED)
        assert policy.category == FailureCategory.AUTHORITY_DENIED
        assert policy.behavior == BoundaryBehavior.HALT_IMMEDIATELY
        assert policy.expected_status == RunStatus.GAVE_UP

    def test_guard_rejection_boundary_preserved(self):
        passed, msg = verify_boundary_preserved(
            TerminationReason.GUARD_REJECTED, RunStatus.GAVE_UP,
        )
        assert passed, msg

    def test_guard_rejection_boundary_violated_on_success(self):
        passed, msg = verify_boundary_preserved(
            TerminationReason.GUARD_REJECTED, RunStatus.SUCCESS,
        )
        assert not passed

    def test_hook_stopped_classified_as_authority_denied(self):
        policy = classify_termination(TerminationReason.HOOK_STOPPED)
        assert policy.category == FailureCategory.AUTHORITY_DENIED
        assert policy.expected_status == RunStatus.GAVE_UP


class TestEnvironmentBlockedInjection:
    """Inject environment unavailability and verify boundary."""

    def test_environment_unavailable_halts_after_inject(self):
        policy = classify_termination(TerminationReason.ENVIRONMENT_UNAVAILABLE)
        assert policy.category == FailureCategory.ENVIRONMENT_BLOCKED
        assert policy.behavior == BoundaryBehavior.HALT_AFTER_INJECT
        assert policy.expected_status == RunStatus.BLOCKED

    def test_environment_boundary_preserved(self):
        passed, msg = verify_boundary_preserved(
            TerminationReason.ENVIRONMENT_UNAVAILABLE, RunStatus.BLOCKED,
        )
        assert passed, msg


class TestModelErrorInjection:
    """Inject model errors and verify boundary."""

    def test_model_error_produces_failed(self):
        policy = classify_termination(TerminationReason.MODEL_ERROR)
        assert policy.category == FailureCategory.MODEL_ERROR
        assert policy.expected_status == RunStatus.FAILED

    def test_prompt_too_long_allows_one_recovery(self):
        policy = classify_termination(TerminationReason.PROMPT_TOO_LONG)
        assert policy.category == FailureCategory.MODEL_ERROR
        assert policy.behavior == BoundaryBehavior.RECOVERABLE
        assert policy.max_recovery_attempts == 1

    def test_prompt_too_long_boundary_after_recovery_exhausted(self):
        passed, msg = verify_boundary_preserved(
            TerminationReason.PROMPT_TOO_LONG, RunStatus.FAILED,
        )
        assert passed, msg


class TestCancellationInjection:
    """Inject user cancellation and verify boundary."""

    def test_user_cancelled_produces_cancelled(self):
        policy = classify_termination(TerminationReason.USER_CANCELLED)
        assert policy.category == FailureCategory.USER_CANCELLED
        assert policy.expected_status == RunStatus.CANCELLED

    def test_aborted_tools_produces_cancelled(self):
        policy = classify_termination(TerminationReason.ABORTED_TOOLS)
        assert policy.category == FailureCategory.USER_CANCELLED
        assert policy.expected_status == RunStatus.CANCELLED

    def test_cancellation_boundary_preserved(self):
        passed, msg = verify_boundary_preserved(
            TerminationReason.USER_CANCELLED, RunStatus.CANCELLED,
        )
        assert passed, msg


class TestReplayStepRecordCapture:
    """Verify that terminal decisions emit step records with correct metadata."""

    def test_terminal_decision_carries_termination_reason(self):
        from observability.models import build_replay_runtime_decision

        class FakeDecision:
            action = StepAction.TERMINATE
            strip_tools = False
            inject_message = ""
            terminate_reason = TerminationReason.BUDGET_EXHAUSTED
            terminate_status = RunStatus.GAVE_UP
            terminate_detail = "budget exhausted"
            terminate_summary = "Execution budget exhausted"

        replay_decision = build_replay_runtime_decision(FakeDecision())
        assert replay_decision.action == "terminate"
        assert replay_decision.terminate_reason == "budget_exhausted"

    def test_continue_decision_has_no_termination(self):
        from observability.models import build_replay_runtime_decision

        class FakeDecision:
            action = StepAction.CONTINUE
            strip_tools = False
            inject_message = ""
            terminate_reason = TerminationReason.NONE
            terminate_status = None
            terminate_detail = ""
            terminate_summary = ""

        replay_decision = build_replay_runtime_decision(FakeDecision())
        assert replay_decision.action == "continue"
        assert replay_decision.terminate_reason == "none"


class TestStreamingProviderTimeout:
    """Streaming calls must honor the same wall-clock boundary as complete()."""

    def test_stream_timeout_suppresses_late_deltas(self):
        import time
        from types import SimpleNamespace

        from llm.invoker import LLMInvoker

        chunks: list[str] = []

        class HungStreamingBackend:
            model_name = "timeout-test"

            def stream(self, messages, tools, *, on_text, on_thought):
                del messages, tools, on_thought
                on_text("before-timeout")
                time.sleep(0.08)
                on_text("after-timeout")
                return SimpleNamespace(
                    total_tokens=0,
                    cache_stats=None,
                    finish_reason="stop",
                    output_tokens=0,
                )

        config = SimpleNamespace(
            request_timeout=0.02,
            llm_retry_delay=0.0,
            llm_max_retries=1,
            stream=True,
            stream_callback=chunks.append,
            thought_callback=None,
            max_tokens=128,
        )

        with pytest.raises(TimeoutError, match="timed out"):
            LLMInvoker(HungStreamingBackend(), config).invoke([], [])

        # The abandoned daemon may finish, but it must not mutate the live turn.
        time.sleep(0.1)
        assert chunks == ["before-timeout"]
