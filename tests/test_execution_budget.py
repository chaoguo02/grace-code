"""Unit tests for agent/v2/execution_budget.py — Unified Execution Budget."""

import time
import pytest
from agent.v2.execution_budget import (
    BudgetLevel,
    BudgetStatus,
    ExecutionBudget,
    ExecutionBudgetConfig,
    ExecutionBudgetState,
    BudgetExhausted,
)


class TestExecutionBudgetConfig:
    def test_defaults(self):
        cfg = ExecutionBudgetConfig()
        assert cfg.token_limit == 80_000
        assert cfg.step_limit == 40
        assert cfg.time_limit_seconds == 600.0
        assert cfg.warning_threshold == 0.80
        assert cfg.critical_threshold == 0.95
        assert cfg.enabled is True


class TestBudgetStatus:
    def test_token_percent(self):
        status = BudgetStatus(
            level=BudgetLevel.COMFORTABLE,
            token_used=40_000, token_limit=80_000,
            steps_taken=10, step_limit=40,
            elapsed_seconds=5.0, time_limit_s=60.0,
        )
        assert status.token_percent == 50.0
        assert status.step_percent == 25.0

    def test_token_percent_no_limit(self):
        status = BudgetStatus(
            level=BudgetLevel.COMFORTABLE,
            token_used=1000, token_limit=0,
            steps_taken=5, step_limit=0,
            elapsed_seconds=1.0, time_limit_s=0.0,
        )
        assert status.token_percent == 0.0
        assert status.step_percent == 0.0


class TestExecutionBudgetLifecycle:
    def test_initial_state(self):
        budget = ExecutionBudget()
        assert budget.state == ExecutionBudgetState.PENDING
        assert budget.token_used == 0
        assert budget.steps_taken == 0
        assert not budget.is_exhausted

    def test_start_transitions_to_running(self):
        budget = ExecutionBudget()
        budget.start()
        assert budget.state == ExecutionBudgetState.RUNNING

    def test_complete_transitions_to_completed(self):
        budget = ExecutionBudget()
        budget.start()
        budget.complete()
        assert budget.state == ExecutionBudgetState.COMPLETED

    def test_exhaust_transitions_to_exhausted(self):
        budget = ExecutionBudget()
        budget.start()
        budget.exhaust("out of tokens")
        assert budget.state == ExecutionBudgetState.EXHAUSTED
        assert budget.is_exhausted


class TestExecutionBudgetConsumption:
    def test_consume_tokens(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(token_limit=1000))
        budget.start()
        budget.consume(300)
        assert budget.token_used == 300
        assert budget.token_remaining == 700

    def test_consume_negative_tokens(self):
        budget = ExecutionBudget()
        budget.start()
        budget.consume(-100)
        assert budget.token_used == 0  # negative clamped to 0

    def test_record_steps(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(step_limit=10))
        budget.start()
        budget.record_step()
        budget.record_step()
        budget.record_step()
        assert budget.steps_taken == 3
        assert budget.steps_remaining == 7


class TestExecutionBudgetLevels:
    def test_comfortable_when_under_threshold(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=1000, step_limit=100,
        ))
        budget.start()
        budget.consume(500)  # 50%
        status = budget.check()
        assert status.level == BudgetLevel.COMFORTABLE

    def test_warning_when_above_80_percent(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=1000, step_limit=100,
            warning_threshold=0.80,
        ))
        budget.start()
        budget.consume(850)  # 85%
        status = budget.check()
        assert status.level == BudgetLevel.WARNING
        assert "warning" in status.inject_message.lower()

    def test_critical_when_above_95_percent(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=1000,
            warning_threshold=0.80,
            critical_threshold=0.95,
        ))
        budget.start()
        budget.consume(970)  # 97%
        status = budget.check()
        assert status.level == BudgetLevel.CRITICAL
        assert "critical" in status.inject_message.lower()

    def test_exhausted_when_above_100_percent(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=1000,
        ))
        budget.start()
        budget.consume(1100)  # 110%
        status = budget.check()
        assert status.level == BudgetLevel.EXHAUSTED
        assert budget.is_exhausted

    def test_exhausted_by_steps(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=100_000, step_limit=10,
        ))
        budget.start()
        for _ in range(11):
            budget.record_step()
        status = budget.check()
        assert status.level == BudgetLevel.EXHAUSTED

    def test_exhausted_by_time(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=100_000, step_limit=100,
            time_limit_seconds=0.01,
        ))
        budget.start()
        time.sleep(0.02)  # exceed the time limit
        status = budget.check()
        assert status.level == BudgetLevel.EXHAUSTED


class TestExecutionBudgetMessages:
    def test_warning_only_injected_once(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=1000, warning_threshold=0.50,
        ))
        budget.start()
        budget.consume(600)
        s1 = budget.check()
        assert s1.inject_message  # first warning
        s2 = budget.check()
        assert not s2.inject_message  # already warned

    def test_critical_only_injected_once(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=1000, warning_threshold=0.80, critical_threshold=0.85,
        ))
        budget.start()
        budget.consume(900)
        s1 = budget.check()
        assert s1.inject_message
        assert "critical" in s1.inject_message.lower()
        s2 = budget.check()
        assert not s2.inject_message  # already warned

    def test_exhausted_only_injected_once(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(token_limit=1000))
        budget.start()
        budget.consume(1100)
        s1 = budget.check()
        assert s1.inject_message
        assert "exhausted" in s1.inject_message.lower()
        s2 = budget.check()
        assert not s2.inject_message  # already injected

    def test_force_finish_message(self):
        msg = ExecutionBudget.force_finish_message()
        assert "FORCE FINISH" in msg
        assert "tools" in msg.lower()


class TestExecutionBudgetDisabled:
    def test_disabled_always_comfortable(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(enabled=False))
        budget.start()
        budget.consume(100_000)
        status = budget.check()
        assert status.level == BudgetLevel.COMFORTABLE
        assert not status.inject_message


class TestExecutionBudgetSerialization:
    def test_to_summary(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=80_000, step_limit=40,
        ))
        budget.start()
        budget.consume(50_000)
        for _ in range(20):
            budget.record_step()
        s = budget.to_summary()
        assert s["token_used"] == 50_000
        assert s["token_limit"] == 80_000
        assert s["steps_taken"] == 20
        assert s["step_limit"] == 40
        assert s["state"] == "running"


class TestExecutionBudgetTimeTracking:
    def test_elapsed_time_increases(self):
        budget = ExecutionBudget()
        budget.start()
        time.sleep(0.01)
        assert budget.elapsed_seconds > 0

    def test_elapsed_time_stops_after_exhaust(self):
        budget = ExecutionBudget()
        budget.start()
        time.sleep(0.01)
        budget.exhaust()
        elapsed = budget.elapsed_seconds
        time.sleep(0.01)
        assert budget.elapsed_seconds == elapsed
