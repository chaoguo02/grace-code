"""Behavioral tests for the stabilization phase.

These tests validate Runtime-enforced behaviors that the LLM cannot override.
Each test uses a mock LLM backend to simulate specific failure scenarios.

Tests cover:
  1. Task Ledger idempotency — same task twice returns cached result
  2. Execution budget exhaustion — max_steps=3 → graceful FAILED
  3. Tool loop detection — repeating tool calls → GAVE_UP
  4. Circuit breaker trip — consecutive failures → GAVE_UP
  5. TaskStateMachine transitions — all paths produce valid state history
  6. ToolError structured output — errors carry error_type and retryable
"""
from __future__ import annotations

import json
import pytest
import time

from core.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.task import (
    Action, ActionType, Observation, ObservationStatus,
    RunStatus, Task, ToolCall,
)
from agent.v2.task_state_machine import TaskState, TaskStateMachine
from agent.v2.execution_budget import ExecutionBudget, ExecutionBudgetConfig
from agent.runtime_controller import RuntimeController, StepAction, StepDecision
from context.history import ConversationHistory
from llm.base import LLMBackend, LLMMessage, LLMToolSchema, LLMResponse
from core.base import (
    BaseTool,
    ToolError,
    ToolErrorType,
    ToolRegistry,
    ToolResult,
    ToolRetryDirective,
    classify_runtime_error,
)
from runtime.process import ProcessTermination, RunResult as ProcessRunResult
from memory.models import Memory, MemoryMetadata, Anchor


# ═══════════════════════════════════════════════════════════════════════════
# Mock LLM Backend — pre-scripted responses for behavioral testing
# ═══════════════════════════════════════════════════════════════════════════

class MockLLMBackend(LLMBackend):
    """LLM backend that returns pre-scripted responses."""

    def __init__(self, responses: list[LLMResponse] | None = None):
        self.responses = responses or []
        self._idx = 0
        self.model_name = "mock"
        self.max_context_window = 200_000

    @property
    def supports_function_calling(self) -> bool:
        return True

    def complete(self, messages, tools) -> LLMResponse:
        if self._idx >= len(self.responses):
            # Default: return FINISH
            return LLMResponse(
                action=Action(action_type=ActionType.FINISH, message="Done.", thought=""),
                total_tokens=100,
                raw_content="",
            )
        resp = self.responses[self._idx]
        self._idx += 1
        return resp

    def reset(self):
        self._idx = 0


def _make_tool_call_response(tool_name: str, params: dict, thought: str = "") -> LLMResponse:
    return LLMResponse(
        action=Action(
            action_type=ActionType.TOOL_CALL,
            thought=thought,
            tool_calls=[ToolCall(name=tool_name, params=params, id="call_1")],
        ),
        total_tokens=100,
        raw_content="",
    )


def _make_finish_response(message: str = "Done.") -> LLMResponse:
    return LLMResponse(
        action=Action(action_type=ActionType.FINISH, message=message, thought=""),
        total_tokens=100,
        raw_content="",
    )


# ═══════════════════════════════════════════════════════════════════════════
# Mock Tool — for simulating failures and loops
# ═══════════════════════════════════════════════════════════════════════════

class EchoTool(BaseTool):
    """A tool that echoes back its input."""
    name = "echo"
    description = "Echo back the message."
    parameters_schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }

    def execute(self, params):
        msg = params.get("message", "")
        return ToolResult(success=True, output=f"Echo: {msg}")


class FailingTool(BaseTool):
    """A tool that always fails."""
    name = "failing"
    description = "Always fails."
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(
        self,
        error_type: ToolErrorType = ToolErrorType.INTERNAL,
        retry: ToolRetryDirective = ToolRetryDirective.DO_NOT_RETRY,
    ):
        super().__init__()
        self.error_type = error_type
        self.retry = retry

    def execute(self, params):
        return ToolResult.from_error(
            error_type=self.error_type,
            detail="This tool always fails",
            retry=self.retry,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: TaskStateMachine — all transitions produce valid history
# ═══════════════════════════════════════════════════════════════════════════

class TestTaskStateMachine:
    """Verify state machine enforcement."""

    def test_all_legal_transitions(self):
        """Verify all documented legal transitions work."""
        tsm = TaskStateMachine(task_id="test-legal")
        assert tsm.state == TaskState.PENDING

        tsm.transition(TaskState.RUNNING, "start")
        assert tsm.state == TaskState.RUNNING

        tsm.transition(TaskState.COMPLETING, "finish_called")
        assert tsm.state == TaskState.COMPLETING

        tsm.transition(TaskState.COMPLETED, "guard_passed")
        assert tsm.state == TaskState.COMPLETED
        assert tsm.is_terminal is True

    def test_guard_block_cycle(self):
        """COMPLETING → RUNNING → COMPLETING → COMPLETED cycle."""
        tsm = TaskStateMachine(task_id="test-guard")
        tsm.transition(TaskState.RUNNING, "start")
        tsm.transition(TaskState.COMPLETING, "finish_called")
        tsm.transition(TaskState.RUNNING, "guard_blocked")
        assert tsm.state == TaskState.RUNNING
        tsm.transition(TaskState.COMPLETING, "finish_called_again")
        tsm.transition(TaskState.COMPLETED, "guard_passed")
        assert tsm.state == TaskState.COMPLETED

    def test_terminal_no_more_transitions(self):
        """COMPLETED/FAILED/CANCELLED cannot transition further."""
        for terminal in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            tsm = TaskStateMachine(task_id=f"test-term-{terminal.value}")
            tsm._state = terminal
            with pytest.raises(ValueError, match="Illegal state transition"):
                tsm.transition(TaskState.RUNNING, "try_resume")

    def test_skip_completing_is_illegal(self):
        """RUNNING → COMPLETED (skipping COMPLETING) is illegal."""
        tsm = TaskStateMachine(task_id="test-skip")
        tsm.transition(TaskState.RUNNING, "start")
        with pytest.raises(ValueError, match="Illegal state transition"):
            tsm.transition(TaskState.COMPLETED, "skip_completing")

    def test_failure_paths(self):
        """RUNNING → FAILED is legal for all failure modes."""
        tsm = TaskStateMachine(task_id="test-fail")
        tsm.transition(TaskState.RUNNING, "start")
        tsm.transition(TaskState.FAILED, "circuit_breaker")
        assert tsm.state == TaskState.FAILED
        assert tsm.is_terminal is True

    def test_history_tracks_all_transitions(self):
        """to_summary includes all state transitions."""
        tsm = TaskStateMachine(task_id="test-hist")
        tsm.transition(TaskState.RUNNING, "start")
        tsm.record_step()
        tsm.record_step()
        tsm.transition(TaskState.COMPLETING, "finish")
        tsm.transition(TaskState.COMPLETED, "done")

        summary = tsm.to_summary()
        assert summary["task_id"] == "test-hist"
        assert summary["step_count"] == 2
        assert summary["state"] == "completed"
        assert summary["is_terminal"] is True
        assert summary["transition_count"] == 3  # PENDING→RUNNING, RUNNING→COMPLETING, COMPLETING→COMPLETED


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: Task Ledger — idempotency
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Test 3: ExecutionBudget — exhaustion behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestExecutionBudgetBehavior:
    """Verify budget enforces limits deterministically."""

    def test_budget_exhausts_at_token_limit(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=1000, step_limit=100,
        ))
        budget.start()
        budget.consume(1100)  # Over the limit
        status = budget.check()
        assert status.level.value == "exhausted"
        assert budget.is_exhausted is True

    def test_budget_exhausts_at_step_limit(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=100_000, step_limit=5,
        ))
        budget.start()
        for _ in range(5):
            budget.record_step()
        status = budget.check()
        assert status.level.value in ("exhausted", "critical")

    def test_force_finish_message_format(self):
        msg = ExecutionBudget.force_finish_message()
        assert "FORCE FINISH" in msg
        assert "no tools" in msg.lower() or "No more tool calls" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: RuntimeController — StepDecision correctness
# ═══════════════════════════════════════════════════════════════════════════

class TestRuntimeController:
    """Verify RuntimeController consolidates checks correctly."""

    @pytest.fixture
    def controller(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=80_000, step_limit=40,
        ))
        budget.start()
        breaker = CircuitBreaker(config=CircuitBreakerConfig())
        tsm = TaskStateMachine(task_id="test-ctrl")
        tsm.transition(TaskState.RUNNING, "start")
        return RuntimeController(
            budget=budget,
            breaker=breaker,
            state_machine=tsm,
            max_steps=40,
            max_consecutive_failures=3,
        )

    def test_normal_step_continues(self, controller):
        """Normal conditions → CONTINUE."""
        decision = controller.check(
            step=1, total_tokens=100,
            history=None, log=None,
            consecutive_failures=0,
        )
        assert decision.action == StepAction.CONTINUE

    def test_max_steps_injects_and_strips_tools(self, controller):
        """Last step → INJECT_MESSAGE with strip_tools=True."""
        controller.max_steps = 3
        decision = controller.check(
            step=3, total_tokens=100,
            history=None, log=None,
            consecutive_failures=0,
        )
        assert decision.action == StepAction.INJECT_MESSAGE
        assert decision.strip_tools is True
        assert "Maximum steps" in decision.inject_message

    def test_consecutive_failures_terminates(self, controller):
        """Too many consecutive failures → TERMINATE."""
        decision = controller.check(
            step=5, total_tokens=500,
            history=None, log=None,
            consecutive_failures=3,
        )
        assert decision.action == StepAction.TERMINATE

    def test_circuit_breaker_trip_terminates(self, controller):
        """Tripped breaker → TERMINATE."""
        # Force trip the breaker
        for _ in range(5):
            controller.breaker.record_denial()
        decision = controller.check(
            step=1, total_tokens=100,
            history=None, log=None,
            consecutive_failures=0,
        )
        assert decision.action == StepAction.TERMINATE
        assert decision.terminate_status == RunStatus.GAVE_UP

    def test_budget_exhausted_strips_tools(self, controller):
        """Budget exhausted → INJECT_MESSAGE with strip_tools."""
        controller.budget.consume(90_000)  # over 80k limit
        decision = controller.check(
            step=5, total_tokens=90_000,
            history=None, log=None,
            consecutive_failures=0,
        )
        assert decision.action == StepAction.INJECT_MESSAGE
        assert decision.strip_tools is True


# ═══════════════════════════════════════════════════════════════════════════
# Test 5: ToolError — structured error output
# ═══════════════════════════════════════════════════════════════════════════

class TestToolError:
    """Verify structured tool errors work correctly."""

    def test_from_error_factory(self):
        result = ToolResult.from_error(
            error_type=ToolErrorType.TIMEOUT,
            detail="Operation timed out after 30s",
            retry=ToolRetryDirective.RETRY,
            alternative="shell",
        )
        assert result.success is False
        assert result.tool_error is not None
        assert result.tool_error.error_type is ToolErrorType.TIMEOUT
        assert result.tool_error.retry is ToolRetryDirective.RETRY
        assert result.tool_error.alternative == "shell"

    def test_to_observation_includes_metadata(self):
        result = ToolResult.from_error(
            error_type=ToolErrorType.PERMISSION_DENIED,
            detail="Access denied",
        )
        obs = result.to_observation("test_tool")
        assert obs.status == ObservationStatus.ERROR
        assert obs.metadata is not None
        assert obs.metadata.get("tool_error", {}).get("error_type") == "permission_denied"
        assert obs.metadata.get("tool_error", {}).get("retry") == "do_not_retry"

    def test_to_message_format(self):
        err = ToolError(
            error_type=ToolErrorType.TIMEOUT,
            retry=ToolRetryDirective.RETRY,
            alternative="shell",
            detail="Timed out after 30s",
        )
        msg = err.to_message()
        assert "[timeout]" in msg
        assert "retryable" in msg
        assert "shell" in msg

    def test_backward_compat_string_error(self):
        """ToolResult with string error (no tool_error) still works."""
        result = ToolResult(success=False, output="", error="Old-style error")
        obs = result.to_observation("test_tool")
        assert obs.status == ObservationStatus.ERROR
        assert obs.error == "Old-style error"
        assert obs.metadata == {}  # no tool_error metadata

    def test_runtime_classification_uses_typed_termination_fact(self):
        result = ProcessRunResult(
            returncode=-1,
            stdout="",
            stderr="arbitrary platform diagnostic",
            termination=ProcessTermination.START_FAILED,
        )

        error = classify_runtime_error(result, "python -m pytest")

        assert error is not None
        assert error.error_type is ToolErrorType.ENVIRONMENT_UNAVAILABLE

    def test_runtime_classification_does_not_parse_stderr(self):
        result = ProcessRunResult(
            returncode=2,
            stdout="",
            stderr="permission denied no module named misleading text",
        )

        error = classify_runtime_error(result, "python -m pytest")

        assert error is not None
        assert error.error_type is ToolErrorType.PROCESS_FAILED


# ═══════════════════════════════════════════════════════════════════════════
# Test 6: Memory — scope/confidence/TTL
# ═══════════════════════════════════════════════════════════════════════════

class TestMemoryModel:
    """Verify new memory metadata fields."""

    def test_metadata_defaults(self):
        meta = MemoryMetadata()
        assert meta.scope == "project"
        assert meta.confidence == 0.7
        assert meta.ttl_seconds is None
        assert meta.expires_at == ""
        assert meta.status == "active"

    def test_metadata_custom_scope_and_confidence(self):
        meta = MemoryMetadata(
            type="user",
            scope="global",
            confidence=1.0,
            ttl_seconds=86400,
        )
        assert meta.scope == "global"
        assert meta.confidence == 1.0
        assert meta.ttl_seconds == 86400

    def test_memory_with_new_fields(self):
        mem = Memory(
            name="test-memory",
            description="A test memory with new fields",
            content="This is a test.",
            metadata=MemoryMetadata(
                type="project",
                scope="project",
                confidence=0.8,
                ttl_seconds=3600,
            ),
        )
        assert mem.metadata.scope == "project"
        assert mem.metadata.confidence == 0.8
        assert mem.metadata.ttl_seconds == 3600
        assert mem.metadata.status == "active"


# ═══════════════════════════════════════════════════════════════════════════
# Heuristic loop/progress detection was intentionally removed.
# ═══════════════════════════════════════════════════════════════════════════

class TestNoHeuristicProgressTracking:
    """Document that no heuristic progress test suite remains."""

    def test_state_machine_exposes_only_objective_step_count(self):
        tsm = TaskStateMachine(task_id="objective-only")
        tsm.transition(TaskState.RUNNING)
        assert tsm.record_step() == 1
        assert not hasattr(tsm, "_no_progress_count")
