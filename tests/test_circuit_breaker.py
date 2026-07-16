"""Unit tests for agent/circuit_breaker.py — Runtime-level circuit breaker."""

import pytest
from agent.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerState,
    CircuitBreakerTripped,
)


class TestCircuitBreakerConfig:
    def test_defaults(self):
        cfg = CircuitBreakerConfig()
        assert cfg.max_consecutive_tool_denials == 3
        assert cfg.max_session_tool_denials == 20
        assert cfg.max_consecutive_subagent_failures == 2
        assert cfg.max_consecutive_tool_errors == 3

    def test_custom_thresholds(self):
        cfg = CircuitBreakerConfig(
            max_consecutive_tool_denials=5,
            max_session_tool_denials=50,
        )
        assert cfg.max_consecutive_tool_denials == 5


class TestCircuitBreakerDenialTracking:
    def test_consecutive_denials_trip(self):
        cb = CircuitBreaker()
        for _ in range(3):
            assert not cb.is_tripped
            cb.record_denial()
        assert cb.is_tripped
        assert "consecutive" in cb.trip_reason.lower()

    def test_approval_resets_consecutive_denials(self):
        cb = CircuitBreaker()
        cb.record_denial()
        cb.record_denial()
        assert not cb.is_tripped
        cb.record_approval()  # reset
        cb.record_denial()
        cb.record_denial()
        assert not cb.is_tripped

    def test_session_denials_trip(self):
        cb = CircuitBreaker(config=CircuitBreakerConfig(max_session_tool_denials=5))
        for _ in range(5):
            cb.record_denial()
        assert cb.check()
        assert cb.is_tripped

    def test_session_denials_below_threshold(self):
        cb = CircuitBreaker(config=CircuitBreakerConfig(
            max_session_tool_denials=10,
            max_consecutive_tool_denials=10,  # also raise to avoid tripping consecutive
        ))
        for _ in range(5):
            cb.record_denial()
        assert not cb.is_tripped


class TestCircuitBreakerSubagentFailures:
    def test_consecutive_subagent_failures_trip(self):
        cb = CircuitBreaker()
        cb.record_subagent_failure()
        assert not cb.is_tripped
        cb.record_subagent_failure()
        assert cb.is_tripped

    def test_success_resets_subagent_failures(self):
        cb = CircuitBreaker()
        cb.record_subagent_failure()
        cb.record_subagent_success()
        cb.record_subagent_failure()
        assert not cb.is_tripped

    def test_default_threshold_is_two(self):
        cb = CircuitBreaker()
        cb.record_subagent_failure()
        assert not cb.check()
        cb.record_subagent_failure()
        assert cb.check()


class TestCircuitBreakerToolErrors:
    def test_consecutive_tool_errors_trip(self):
        cb = CircuitBreaker()
        for _ in range(3):
            assert not cb.is_tripped
            cb.record_tool_error()
        assert cb.is_tripped

    def test_success_resets_tool_errors(self):
        cb = CircuitBreaker()
        cb.record_tool_error()
        cb.record_tool_error()
        cb.record_tool_success()
        cb.record_tool_error()
        assert not cb.is_tripped


class TestCircuitBreakerState:
    def test_initial_state_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitBreakerState.CLOSED
        assert not cb.is_tripped

    def test_trip_transitions_to_open(self):
        cb = CircuitBreaker()
        for _ in range(3):
            cb.record_denial()
        assert cb.state == CircuitBreakerState.OPEN
        assert cb.is_tripped

    def test_check_returns_true_when_tripped(self):
        cb = CircuitBreaker()
        cb.record_denial()
        cb.record_denial()
        cb.record_denial()
        assert cb.check() is True
        assert cb.check() is True  # stays tripped


class TestCircuitBreakerSummary:
    def test_to_summary_includes_all_counters(self):
        cb = CircuitBreaker()
        cb.record_denial()
        cb.record_subagent_failure()
        cb.record_tool_error()
        cb.record_approval()
        cb.record_subagent_success()
        s = cb.to_summary()
        assert s["state"] == "closed"
        assert s["session_denials"] == 1
        assert s["consecutive_denials"] == 0  # reset by approval
        assert s["consecutive_subagent_failures"] == 0  # reset by success
        assert s["consecutive_tool_errors"] == 1


class TestCircuitBreakerTrippedException:
    def test_exception_carries_state(self):
        exc = CircuitBreakerTripped("test reason", CircuitBreakerState.OPEN)
        assert str(exc) == "test reason"
        assert exc.state == CircuitBreakerState.OPEN


class TestMultipleMetricsIndependent:
    """Each metric trips independently — denials don't reset tool errors etc."""

    def test_denial_trip_does_not_reset_tool_errors(self):
        cb = CircuitBreaker()
        # Build up tool errors
        cb.record_tool_error()
        cb.record_tool_error()
        # Trip via denials
        for _ in range(3):
            cb.record_denial()
        assert cb.is_tripped
        # Check tool errors still at 2 (not reset)
        assert cb.to_summary()["consecutive_tool_errors"] == 2

    def test_subagent_failure_trips_independently(self):
        cb = CircuitBreaker(config=CircuitBreakerConfig(
            max_consecutive_subagent_failures=2,
            max_consecutive_tool_denials=5,
        ))
        cb.record_subagent_failure()
        cb.record_subagent_failure()
        assert cb.is_tripped
        assert "subagent" in cb.trip_reason.lower()
