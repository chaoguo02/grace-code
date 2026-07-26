"""
Runtime controller boundary tests.

Verifies the per-step gate returns immediate terminal decisions for hard stops
and stable injected messages for soft warnings.
"""

from unittest.mock import MagicMock


class TestRuntimeController:
    def test_circuit_breaker_terminates_immediately(self):
        from agent.runtime_controller import RuntimeController, StepAction

        breaker = MagicMock()
        breaker.check.return_value = True
        breaker.trip_reason = "permission circuit breaker"

        controller = RuntimeController(breaker=breaker)
        decision = controller.check(
            step=1,
            total_tokens=0,
            history=MagicMock(),
            log=MagicMock(),
        )

        assert decision.action is StepAction.TERMINATE
        assert decision.terminate_reason.value == "circuit_breaker"
        assert decision.terminate_summary == "permission circuit breaker"
        assert decision.terminate_detail == "permission circuit breaker"

    def test_max_steps_terminates_immediately(self):
        from agent.runtime_controller import RuntimeController, StepAction
        from agent.session.execution_budget import ExecutionBudget
        from agent.session.task_state_machine import TaskStateMachine

        budget = ExecutionBudget()
        budget.start()
        controller = RuntimeController(
            budget=budget,
            state_machine=TaskStateMachine(task_id="task-1"),
            max_steps=3,
        )

        decision = controller.check(
            step=3,
            total_tokens=0,
            history=MagicMock(),
            log=MagicMock(),
        )

        assert decision.action is StepAction.TERMINATE
        assert decision.terminate_reason.value == "max_steps"
        assert decision.terminate_status.value == "max_steps"
        assert decision.terminate_summary == "Maximum steps (3) reached"
        assert decision.terminate_detail == "Maximum steps (3) reached"
        assert budget.is_exhausted is True

    def test_budget_exhaustion_terminates_and_strips_followup_visibility(self):
        from agent.runtime_controller import RuntimeController, StepAction
        from agent.session.execution_budget import ExecutionBudget
        from agent.session.task_state_machine import TaskStateMachine

        budget = ExecutionBudget()
        budget.start()
        budget.exhaust("budget exhausted for test")
        controller = RuntimeController(
            budget=budget,
            state_machine=TaskStateMachine(task_id="task-2"),
        )

        decision = controller.check(
            step=1,
            total_tokens=0,
            history=MagicMock(),
            log=MagicMock(),
        )

        assert decision.action is StepAction.TERMINATE
        assert decision.terminate_reason.value == "budget_exhausted"
        assert decision.terminate_status.value == "gave_up"
        assert decision.terminate_summary == "Execution budget exhausted"
        assert "budget exhausted" in decision.terminate_detail.lower()
        assert decision.strip_tools is False

    def test_budget_crossing_limit_allows_one_tool_free_final_turn(self):
        from agent.runtime_controller import RuntimeController, StepAction
        from agent.session.execution_budget import (
            ExecutionBudget,
            ExecutionBudgetConfig,
        )

        budget = ExecutionBudget(
            config=ExecutionBudgetConfig(
                token_limit=100,
                step_limit=10,
                time_limit_seconds=0,
            ),
        )
        budget.start()
        budget.consume(100)
        controller = RuntimeController(budget=budget, max_steps=10)

        final_turn = controller.check(
            step=2,
            total_tokens=100,
            history=MagicMock(),
            log=MagicMock(),
        )

        assert final_turn.action is StepAction.INJECT_MESSAGE
        assert final_turn.strip_tools is True
        assert "final summary" in final_turn.inject_message.lower()

        terminal = controller.check(
            step=3,
            total_tokens=100,
            history=MagicMock(),
            log=MagicMock(),
        )
        assert terminal.action is StepAction.TERMINATE
        assert terminal.terminate_reason.value == "budget_exhausted"

    def test_consecutive_failures_terminate_immediately(self):
        from agent.runtime_controller import RuntimeController, StepAction

        controller = RuntimeController(max_consecutive_failures=2)
        decision = controller.check(
            step=1,
            total_tokens=0,
            history=MagicMock(),
            log=MagicMock(),
            consecutive_failures=2,
        )

        assert decision.action is StepAction.TERMINATE
        assert decision.terminate_reason.value == "tool_failure_limit"
        assert decision.terminate_summary == "Aborting: 2 consecutive tool failures"
        assert decision.terminate_detail == "2 consecutive tool failures"
