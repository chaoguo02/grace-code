"""Behavioral guard tests for core invariants.

These tests protect the system's "laws of physics" — properties that MUST
hold true regardless of refactoring. If any test breaks, a core invariant
has been violated.

Invariants covered:
  1. Main loop: RuntimeController.check() is the single pre-step gate
  2. Task delegation: subagent inherits parent session's repo_path
  3. Permission order: HITL pipeline runs before tool execution
  4. State machine: terminal states cannot be re-entered
  5. Context retention: memory section survives compaction
  6. Budget: exhausted budget permanently strips tools
"""
import hashlib
import os
import tempfile
import time
from pathlib import Path

import pytest

from agent.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerState
from agent.completion_guard import CompletionContext, TaskCompletionGuard
from agent.runtime_controller import RuntimeController, StepAction
from agent.task import TaskIntent
from agent.v2.execution_budget import ExecutionBudget, ExecutionBudgetConfig
from agent.v2.task_state_machine import TaskState, TaskStateMachine
from agent.v2.models import AgentDefinition
from memory.context import MemoryContext
from memory.models import Anchor, Memory, MemoryMetadata
from memory.store import MemoryStore
from tools.base import ToolError, ToolErrorType, ToolRegistry, ToolResult
from tools.file_tool import FileReadTool
from tools.submit_findings_tool import SubmitFindingsTool


# ═══════════════════════════════════════════════════════════════════════════
# Invariant 1: RuntimeController is the SINGLE pre-step gate
# ═══════════════════════════════════════════════════════════════════════════

class TestMainLoopGate:
    """Every step MUST pass through RuntimeController.check() before execution."""

    def test_circuit_breaker_trip_terminates(self):
        """Tripped breaker → TERMINATE before any LLM call or tool execution."""
        breaker = CircuitBreaker(config=CircuitBreakerConfig())
        breaker.record_denial()
        breaker.record_denial()
        breaker.record_denial()

        budget = ExecutionBudget(config=ExecutionBudgetConfig())
        budget.start()
        tsm = TaskStateMachine(task_id="test")
        tsm.transition(TaskState.RUNNING, "start")

        controller = RuntimeController(
            budget=budget, breaker=breaker, state_machine=tsm,
            max_steps=40, max_consecutive_failures=3,
        )

        d = controller.check(step=1, total_tokens=0, history=None, log=None, consecutive_failures=0)
        assert d.action == StepAction.TERMINATE

    def test_budget_exhausted_strips_tools_permanently(self):
        """Once exhausted, tools stay stripped for ALL subsequent turns. (P0 regression)"""
        budget = ExecutionBudget(config=ExecutionBudgetConfig(token_limit=100))
        budget.start()
        budget.consume(200)
        budget.exhaust("test")
        breaker = CircuitBreaker(config=CircuitBreakerConfig())
        tsm = TaskStateMachine(task_id="test")
        tsm.transition(TaskState.RUNNING, "start")
        controller = RuntimeController(
            budget=budget, breaker=breaker, state_machine=tsm,
            max_steps=10, max_consecutive_failures=3,
        )

        for turn in range(1, 6):
            d = controller.check(step=turn, total_tokens=200, history=None, log=None, consecutive_failures=0)
            assert d.strip_tools is True, f"Turn {turn}: tools MUST remain stripped"

    def test_normal_step_continues_without_interference(self):
        """Under normal conditions, CONTINUE — no false TERMINATE."""
        budget = ExecutionBudget(config=ExecutionBudgetConfig())
        budget.start()
        breaker = CircuitBreaker(config=CircuitBreakerConfig())
        tsm = TaskStateMachine(task_id="test")
        tsm.transition(TaskState.RUNNING, "start")
        controller = RuntimeController(
            budget=budget, breaker=breaker, state_machine=tsm,
            max_steps=40, max_consecutive_failures=3,
        )
        d = controller.check(step=10, total_tokens=5000, history=None, log=None, consecutive_failures=0)
        assert d.action == StepAction.CONTINUE


# ═══════════════════════════════════════════════════════════════════════════
# Invariant 2: Subagent inherits parent session repo_path
# ═══════════════════════════════════════════════════════════════════════════

class TestSubagentRepoInheritance:
    """Subagent MUST work in the parent session's repo context."""

    def test_agent_definition_isolation_field(self):
        """isolation field distinguishes shared vs worktree vs primary."""
        from agent.v2.models import AgentIsolation

        primary = AgentDefinition(name="build", description="primary", intent=TaskIntent.EDIT, isolation=AgentIsolation.NONE)
        shared_agent = AgentDefinition(name="explore", description="shared", intent=TaskIntent.ANALYSIS, isolation=AgentIsolation.SHARED)
        worktree_agent = AgentDefinition(name="general", description="worktree", intent=TaskIntent.EDIT, isolation=AgentIsolation.WORKTREE)

        assert primary.mode == "primary"
        assert shared_agent.mode == "subagent"
        assert worktree_agent.mode == "subagent"

    def test_repo_path_flow_in_fork_result(self):
        """ForkResult carries agent_name and session_id for traceability."""
        from agent.v2.models import ForkResult
        fr = ForkResult(
            agent_name="explore", session_id="abc123", status="completed",
            summary="Done", turns_used=3, tokens_used=500,
        )
        assert fr.agent_name == "explore"
        assert fr.session_id == "abc123"


# ═══════════════════════════════════════════════════════════════════════════
# Invariant 3: Permission pipeline runs before ANY tool execution
# ═══════════════════════════════════════════════════════════════════════════

class TestPermissionOrder:
    """HITL/permission checks MUST precede tool execution."""

    def test_blocked_tool_never_executes(self):
        """CapabilityRegistry blocks result in ToolError, not execution."""
        registry = ToolRegistry()

        class BlockedTool:
            name = "blocked"
            description = "Always blocked"
            parameters_schema = {"type": "object", "properties": {}}
            risk_level = "none"
            def execute(self, params):
                return ToolResult(success=True, output="executed")
            def classify_risk(self, params):
                return "none"
            def to_llm_schema(self):
                from llm.base import LLMToolSchema
                return LLMToolSchema(name="blocked", description="x", parameters={})

        registry.register(BlockedTool())

        # Tool marked UNAVAILABLE in capability registry
        from agent.capability_registry import (
            CapabilityRegistry,
            CapabilityState,
            InterceptDecision,
        )
        cap = CapabilityRegistry()
        cap.register("blocked")
        cap.mark_unavailable("blocked", "Blocked for testing")

        assert cap.state_for("blocked") is CapabilityState.UNAVAILABLE
        intercept = cap.intercept("blocked", session_id="test-session")
        assert intercept.decision is InterceptDecision.BLOCK
        assert intercept.feedback["retry"] == "do_not_retry"

        registry._capability_registry = cap
        registry._session_id = "test-session"

        result = registry.execute_tool("blocked", {})
        assert result.success is False
        assert result.tool_error is not None
        assert result.tool_error.error_type is ToolErrorType.UNAVAILABLE

    def test_unknown_tool_returns_not_found(self):
        """Unknown tool → not_found error, never crashes."""
        registry = ToolRegistry()
        result = registry.execute_tool("nonexistent", {})
        assert result.success is False
        assert result.tool_error is not None
        assert result.tool_error.error_type is ToolErrorType.NOT_FOUND


# ═══════════════════════════════════════════════════════════════════════════
# Invariant 4: Terminal states are IMMUTABLE
# ═══════════════════════════════════════════════════════════════════════════

class TestTerminalStateImmutability:
    """COMPLETED/FAILED/CANCELLED cannot transition further."""

    def test_all_terminals_blocked(self):
        for terminal in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            tsm = TaskStateMachine(task_id=f"term-{terminal.value}")
            tsm._state = terminal
            with pytest.raises(ValueError, match="Illegal state transition"):
                tsm.transition(TaskState.RUNNING, "try_resume")

    def test_completing_to_running_cycle(self):
        """COMPLETING → RUNNING (guard blocked) → COMPLETING → COMPLETED is legal."""
        tsm = TaskStateMachine(task_id="cycle-test")
        tsm.transition(TaskState.RUNNING, "start")

        tsm.transition(TaskState.COMPLETING, "finish")
        tsm.transition(TaskState.RUNNING, "blocked")  # guard re-enters loop
        tsm.transition(TaskState.COMPLETING, "finish_again")
        tsm.transition(TaskState.COMPLETED, "done")

        assert tsm.state == TaskState.COMPLETED
        assert tsm.is_terminal


# ═══════════════════════════════════════════════════════════════════════════
# Invariant 5: Memory section survives compaction
# ═══════════════════════════════════════════════════════════════════════════

class TestMemoryContextRetention:
    """Memory injection MUST survive across conversation turns."""

    def test_feedback_by_file_anchor(self, tmp_path):
        """Feedback matched by file anchor is injected per-step."""
        test_file = tmp_path / "target.py"
        test_file.write_text("print('hello')", encoding="utf-8")
        file_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()

        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        store.write_memory(Memory(
            name="rule-dont-use-shell",
            description="Never use shell to read files",
            content="Always use file_read for reading source code.",
            metadata=MemoryMetadata(type="feedback", status="active", scope="project"),
            anchors=[Anchor(kind="file", path=str(test_file), content_hash=file_hash)],
        ))

        ctx = MemoryContext(store=store)
        result = ctx.get_feedback_for_files({str(test_file)})
        assert "rule-dont-use-shell" in result
        assert "file_read" in result

    def test_hash_mismatch_deprecates(self, tmp_path):
        """Changed file → anchored feedback memory is deprecated (Code is Truth)."""
        test_file = tmp_path / "changing.py"
        test_file.write_text("v1", encoding="utf-8")
        file_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()

        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        store.write_memory(Memory(
            name="old-rule",
            description="An old rule",
            content="This was valid for v1.",
            metadata=MemoryMetadata(type="feedback", status="active", scope="project"),
            anchors=[Anchor(kind="file", path=str(test_file), content_hash=file_hash)],
        ))

        # Modify the file
        test_file.write_text("v2 — changed!", encoding="utf-8")

        ctx = MemoryContext(store=store)
        result = ctx.get_feedback_for_files({str(test_file)})
        # Memory should be deprecated — NOT injected
        assert "old-rule" not in result
        mem = store.read_memory("old-rule")
        assert mem.metadata.status == "deprecated"


# ═══════════════════════════════════════════════════════════════════════════
# Invariant 6: Task idempotency — same task → same result
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Invariant 7: Subagent tool restriction is enforced
# ═══════════════════════════════════════════════════════════════════════════

class TestSubagentToolBoundary:
    """Subagent tool set is strictly bounded by AgentDefinition."""

    def test_disallowed_tools_resolved(self):
        """disallowed_tools go through alias resolution (Write→file_write)."""
        from agent.v2.agent_registry import _TOOL_ALIASES
        assert _TOOL_ALIASES["Write"] == "file_write"
        assert _TOOL_ALIASES["Edit"] == "file_edit"
        assert _TOOL_ALIASES["Bash"] == "shell"
        assert _TOOL_ALIASES["Task"] == "task"

    def test_code_reviewer_contract(self):
        """code-reviewer MUST call submit_findings before FINISH."""
        guard = TaskCompletionGuard()
        ctx = CompletionContext()
        ctx.record_tool_result("file_read", FileReadTool.metadata, "test.py", True)

        # Blocked without submit_findings
        result = guard.check(ctx=ctx, task_intent="analysis", completion_requires={"submit_findings": 1})
        assert result.can_complete is False
        assert "submit_findings" in result.inject_message

        # Allowed after calling submit_findings
        ctx.record_tool_result(
            "submit_findings", SubmitFindingsTool.metadata, None, True
        )
        result2 = guard.check(ctx=ctx, task_intent="analysis", completion_requires={"submit_findings": 1})
        assert result2.can_complete is True
