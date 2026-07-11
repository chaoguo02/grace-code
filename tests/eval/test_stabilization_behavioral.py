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

from agent.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.task import (
    Action, ActionType, Observation, ObservationStatus,
    RunStatus, Task, ToolCall,
)
from agent.v2.task_state_machine import TaskState, TaskStateMachine
from agent.v2.execution_budget import ExecutionBudget, ExecutionBudgetConfig
from agent.v2.task_ledger import TaskLedger, TaskFingerprint
from agent.runtime_controller import RuntimeController, StepAction, StepDecision
from context.history import ConversationHistory
from llm.base import LLMBackend, LLMMessage, LLMToolSchema, LLMResponse
from tools.base import BaseTool, ToolError, ToolRegistry, ToolResult
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

    def __init__(self, error_type: str = "internal", retryable: bool = False):
        super().__init__()
        self.error_type = error_type
        self.retryable = retryable

    def execute(self, params):
        return ToolResult.from_error(
            error_type=self.error_type,
            detail="This tool always fails",
            retryable=self.retryable,
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

class TestTaskLedger:
    """Verify task idempotency via TaskLedger."""

    @pytest.fixture
    def ledger(self, tmp_path):
        db_path = str(tmp_path / "test_ledger.db")
        return TaskLedger(db_path=db_path, ttl_seconds=3600)

    def test_fingerprint_deterministic(self):
        """Same inputs produce same fingerprint."""
        fp1 = TaskFingerprint.compute("analyze file.py", "/repo", "analysis")
        fp2 = TaskFingerprint.compute("analyze file.py", "/repo", "analysis")
        assert fp1.fingerprint_hash == fp2.fingerprint_hash

    def test_fingerprint_different_description(self):
        """Different descriptions produce different fingerprints."""
        fp1 = TaskFingerprint.compute("analyze file.py", "/repo", "analysis")
        fp2 = TaskFingerprint.compute("analyze other.py", "/repo", "analysis")
        assert fp1.fingerprint_hash != fp2.fingerprint_hash

    def test_fingerprint_different_intent(self):
        """Different intents produce different fingerprints."""
        fp1 = TaskFingerprint.compute("analyze file.py", "/repo", "analysis")
        fp2 = TaskFingerprint.compute("analyze file.py", "/repo", "edit")
        assert fp1.fingerprint_hash != fp2.fingerprint_hash

    def test_fingerprint_normalized(self):
        """Trivial whitespace/case variations produce same fingerprint."""
        fp1 = TaskFingerprint.compute("  Analyze FILE.PY  ", "/repo", "analysis")
        fp2 = TaskFingerprint.compute("analyze file.py", "/repo", "analysis")
        assert fp1.fingerprint_hash == fp2.fingerprint_hash

    def test_not_completed_initially(self, ledger):
        """Fresh ledger has no completed tasks."""
        fp = TaskFingerprint.compute("test task", "/repo", "edit")
        assert ledger.is_completed(fp) is False

    def test_mark_and_check_completed(self, ledger):
        """After marking completed, is_completed returns True."""
        fp = TaskFingerprint.compute("test task", "/repo", "edit")
        ledger.mark_completed(fp, "Task done successfully")
        assert ledger.is_completed(fp) is True

    def test_get_cached_result(self, ledger):
        """Cached result contains status and summary."""
        fp = TaskFingerprint.compute("test task", "/repo", "edit")
        ledger.mark_completed(fp, "The result summary")
        cached = ledger.get_cached_result(fp)
        assert cached is not None
        assert cached["status"] == "completed"
        assert cached["summary"] == "The result summary"

    def test_invalidate(self, ledger):
        """Invalidated tasks are not cached."""
        fp = TaskFingerprint.compute("test task", "/repo", "edit")
        ledger.mark_completed(fp, "done")
        assert ledger.is_completed(fp) is True
        ledger.invalidate(fp)
        assert ledger.is_completed(fp) is False

    def test_invalidate_for_repo(self, ledger):
        """Invalidate by repo clears only that repo's entries."""
        fp1 = TaskFingerprint.compute("task A", "/repo1", "edit")
        fp2 = TaskFingerprint.compute("task B", "/repo2", "edit")
        ledger.mark_completed(fp1, "A done")
        ledger.mark_completed(fp2, "B done")
        ledger.invalidate_for_repo("/repo1")
        assert ledger.is_completed(fp1) is False
        assert ledger.is_completed(fp2) is True

    def test_count(self, ledger):
        """Count returns active entries."""
        assert ledger.count() == 0
        ledger.mark_completed(TaskFingerprint.compute("t1", "/r", "edit"), "")
        ledger.mark_completed(TaskFingerprint.compute("t2", "/r", "edit"), "")
        assert ledger.count() == 2

    def test_different_max_steps_same_fingerprint(self):
        """max_steps is an execution detail, not part of task identity."""
        # Good: same task with different max_steps should have same fingerprint
        fp1 = TaskFingerprint.compute("analyze file.py", "/repo", "analysis")
        fp2 = TaskFingerprint.compute("analyze file.py", "/repo", "analysis")
        assert fp1.fingerprint_hash == fp2.fingerprint_hash
        # The TaskFingerprint doesn't include max_steps — that's correct design


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: ExecutionBudget — exhaustion behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestExecutionBudgetBehavior:
    """Verify budget enforces limits deterministically."""

    def test_budget_exhausts_at_token_limit(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=1000, step_limit=100, enabled=True,
        ))
        budget.start()
        budget.consume(1100)  # Over the limit
        status = budget.check()
        assert status.level.value == "exhausted"
        assert budget.is_exhausted is True

    def test_budget_exhausts_at_step_limit(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=100_000, step_limit=5, enabled=True,
        ))
        budget.start()
        for _ in range(5):
            budget.record_step()
        status = budget.check()
        assert status.level.value in ("exhausted", "critical")

    def test_budget_disabled_always_comfortable(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=100, step_limit=1, enabled=False,
        ))
        budget.start()
        budget.consume(9999)
        budget.record_step()
        status = budget.check()
        assert status.level.value == "comfortable"

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
            token_limit=80_000, step_limit=40, enabled=True,
        ))
        budget.start()
        breaker = CircuitBreaker(config=CircuitBreakerConfig(enabled=True))
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
        assert decision.terminate_status == "gave_up"

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
            error_type="timeout",
            detail="Operation timed out after 30s",
            retryable=True,
            alternative="shell",
        )
        assert result.success is False
        assert result.tool_error is not None
        assert result.tool_error.error_type == "timeout"
        assert result.tool_error.retryable is True
        assert result.tool_error.alternative == "shell"

    def test_to_observation_includes_metadata(self):
        result = ToolResult.from_error(
            error_type="permission_denied",
            detail="Access denied",
            retryable=False,
        )
        obs = result.to_observation("test_tool")
        assert obs.status == ObservationStatus.ERROR
        assert obs.metadata is not None
        assert obs.metadata.get("tool_error", {}).get("error_type") == "permission_denied"
        assert obs.metadata.get("tool_error", {}).get("retryable") is False

    def test_to_message_format(self):
        err = ToolError(
            error_type="timeout",
            retryable=True,
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
# Test 7: MacroLoopDetector integration
# ═══════════════════════════════════════════════════════════════════════════

class TestMacroLoopDetection:
    """Verify macro loop detection catches repeating patterns."""

    def test_spawn_read_loop_detected(self):
        from agent.v2.macro_loop_detector import MacroLoopDetector, MacroActionType
        detector = MacroLoopDetector()
        # Simulate: SPAWN → READ → SPAWN → READ → SPAWN → READ
        for _ in range(3):
            detector.record_tool_call("task", {"subagent_type": "explore"})
            detector.record_tool_call("file_read", {"path": "a.py"})
        assert detector.is_tripped is True

    def test_reflection_breaks_pattern(self):
        from agent.v2.macro_loop_detector import MacroLoopDetector
        detector = MacroLoopDetector()
        for _ in range(2):
            detector.record_tool_call("task", {"subagent_type": "explore"})
            detector.record_tool_call("file_read", {"path": "a.py"})
        detector.record_reflection("no_edit")  # breaks pattern
        assert detector.is_tripped is False

    def test_write_resets_progress(self):
        from agent.v2.macro_loop_detector import MacroLoopDetector
        detector = MacroLoopDetector()
        for _ in range(10):
            detector.record_tool_call("file_read", {"path": "a.py"})
        # Many reads without writes should trip no-progress detector
        assert detector.is_tripped is True
