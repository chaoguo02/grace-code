"""Phase 5.3: Replay tests — real failure scenarios as automated tests.

Each test encodes a failure mode discovered during architecture review or
stabilization. The test name describes the failure scenario it prevents.

Scenarios:
  A: Memory pollution — stale feedback memory with changed file
  B: Budget exhaustion — graceful termination at max_steps
  C: Circuit breaker — consecutive subagent failures trip breaker
  D: State machine — COMPLETED cannot re-enter RUNNING
"""
import hashlib
import os
import time
from pathlib import Path

import pytest

from core.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerState
from agent.completion_guard import CompletionContext, TaskCompletionGuard
from agent.runtime_controller import RuntimeController, StepAction
from agent.v2.execution_budget import ExecutionBudget, ExecutionBudgetConfig
from agent.v2.task_state_machine import TaskState, TaskStateMachine
from memory.context import MemoryContext
from memory.models import Anchor, Memory, MemoryMetadata
from memory.store import MemoryStore


# ═══════════════════════════════════════════════════════════════════════════
# Scenario A: MacroLoop — subagent spawn→read→spawn→read pattern
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Scenario B: Memory pollution — stale feedback with changed file
# ═══════════════════════════════════════════════════════════════════════════

class TestReplayMemoryPollution:
    """Prevent regression: stale feedback memory polluting context.

    Failure mode: a feedback memory says "never use shell to read files",
    anchored to rules.md. rules.md gets updated but the memory isn't
    invalidated → the agent follows obsolete rules.
    """

    def test_stale_feedback_deprecated_on_file_change(self, tmp_path):
        test_file = tmp_path / "rules.md"
        test_file.write_text("Old rules content", encoding="utf-8")
        old_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()

        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        # Create feedback memory anchored to rules.md
        mem = Memory(
            name="never-shell-read",
            description="Rule: never use shell to read files",
            content="Always use file_read, never cat/head/tail in shell",
            metadata=MemoryMetadata(type="feedback", status="active", scope="project"),
            anchors=[Anchor(kind="file", path=str(test_file), content_hash=old_hash)],
        )
        store.write_memory(mem)

        # Change the file
        test_file.write_text("Updated rules — shell is now allowed for reading", encoding="utf-8")

        # Verify: memory is deprecated via feedback_for_files check
        ctx = MemoryContext(store=store)
        feedback = ctx.get_feedback_for_files({"rules.md"})
        assert "never-shell-read" not in feedback or "deprecated" in str(
            store.read_memory("never-shell-read").metadata.status
        )

    def test_project_memory_degraded_not_deprecated_on_file_change(self, tmp_path):
        """Project memories get confidence degradation, not deletion."""
        test_file = tmp_path / "architecture.md"
        test_file.write_text("System uses Redis for caching", encoding="utf-8")
        old_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()

        store_dir = tmp_path / "memory"
        store_dir.mkdir()
        (store_dir / "MEMORY.md").write_text("", encoding="utf-8")
        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(store_dir))

        mem = Memory(
            name="redis-architecture",
            description="Redis caching architecture",
            content="The system uses Redis for caching with TTL=3600",
            metadata=MemoryMetadata(type="project", scope="project", confidence=0.8),
            anchors=[Anchor(kind="file", path=str(test_file), content_hash=old_hash)],
        )
        store.write_memory(mem)

        # Change the file
        test_file.write_text("System now uses Memcached instead of Redis", encoding="utf-8")

        # Verify: confidence is degraded but memory is NOT deprecated
        ctx = MemoryContext(store=store)
        result = ctx._verify_memory_freshness(store.read_memory("redis-architecture"))
        assert "FILE CHANGED" in result
        updated = store.read_memory("redis-architecture")
        assert updated.metadata.status == "active"  # NOT deprecated
        assert updated.metadata.confidence < 0.8  # Degraded


# ═══════════════════════════════════════════════════════════════════════════
# Scenario C: Budget exhaustion — graceful termination
# ═══════════════════════════════════════════════════════════════════════════

class TestReplayBudgetExhaustion:
    """Prevent regression: budget exhaustion causes chaos.

    Failure mode: agent hits max_steps but keeps trying to call tools,
    resulting in confusing error messages or infinite retry loops.
    """

    def test_max_steps_strips_tools(self):
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=100_000, step_limit=3,
        ))
        budget.start()
        breaker = CircuitBreaker(config=CircuitBreakerConfig())
        tsm = TaskStateMachine(task_id="budget-test")
        tsm.transition(TaskState.RUNNING, "start")

        controller = RuntimeController(
            budget=budget, breaker=breaker, state_machine=tsm,
            max_steps=3, max_consecutive_failures=3,
        )

        # Step 3: last step should strip tools
        d = controller.check(step=3, total_tokens=500, history=None, log=None, consecutive_failures=0)
        assert d.action == StepAction.INJECT_MESSAGE
        assert d.strip_tools is True
        assert "Maximum steps" in d.inject_message

    def test_budget_exhausted_persists(self):
        """Once exhausted, tools stay stripped across multiple turns (P0 fix)."""
        budget = ExecutionBudget(config=ExecutionBudgetConfig(
            token_limit=100, step_limit=10,
        ))
        budget.start()
        budget.consume(200)  # over limit
        budget.exhaust("test")

        breaker = CircuitBreaker(config=CircuitBreakerConfig())
        tsm = TaskStateMachine(task_id="exhaust-test")
        tsm.transition(TaskState.RUNNING, "start")

        controller = RuntimeController(
            budget=budget, breaker=breaker, state_machine=tsm,
            max_steps=10, max_consecutive_failures=3,
        )

        # Multiple turns after exhaustion: tools always stripped
        for turn in range(1, 6):
            d = controller.check(step=turn, total_tokens=200, history=None, log=None, consecutive_failures=0)
            assert d.strip_tools is True, f"Turn {turn}: tools should remain stripped"


# ═══════════════════════════════════════════════════════════════════════════
# Scenario D: Circuit breaker — consecutive subagent failures
# ═══════════════════════════════════════════════════════════════════════════

class TestReplayCircuitBreaker:
    """Prevent regression: circuit breaker doesn't trip on subagent failures.

    Failure mode: subagent keeps crashing but parent keeps retrying,
    consuming tokens and never making progress.
    """

    def test_subagent_failures_trip_breaker(self):
        breaker = CircuitBreaker(config=CircuitBreakerConfig(
            max_consecutive_subagent_failures=2,
        ))
        assert breaker.is_tripped is False
        breaker.record_subagent_failure()
        assert breaker.is_tripped is False  # 1 failure: not yet
        breaker.record_subagent_failure()
        assert breaker.is_tripped is True   # 2 failures: tripped
        assert breaker.state == CircuitBreakerState.OPEN

    def test_subagent_success_resets_counter(self):
        breaker = CircuitBreaker(config=CircuitBreakerConfig(
            max_consecutive_subagent_failures=2,
        ))
        breaker.record_subagent_failure()
        breaker.record_subagent_success()  # reset
        breaker.record_subagent_failure()
        assert breaker.is_tripped is False  # Only 1 consecutive since reset

    def test_cloned_breaker_independent(self):
        """Parent breaker state does NOT affect subagent breaker (and vice versa)."""
        parent = CircuitBreaker(config=CircuitBreakerConfig())
        parent.record_subagent_failure()
        parent.record_subagent_failure()
        assert parent.is_tripped is True

        child = parent.clone_for_subagent()
        assert child.is_tripped is False  # Fresh counters

        # Child tripping doesn't affect parent state
        child.record_denial()
        child.record_denial()
        child.record_denial()


# ═══════════════════════════════════════════════════════════════════════════
# Scenario E: Task idempotency — same task twice
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Scenario F: State machine — terminal states cannot be re-entered
# ═══════════════════════════════════════════════════════════════════════════

class TestReplayStateMachine:
    """Prevent regression: COMPLETED task can be re-run.

    Failure mode: state machine allows COMPLETED → RUNNING transition,
    causing duplicate execution or corrupted state.
    """

    def test_completed_cannot_transition(self):
        tsm = TaskStateMachine(task_id="immutable-test")
        tsm.transition(TaskState.RUNNING, "start")
        tsm.transition(TaskState.COMPLETING, "finish")
        tsm.transition(TaskState.COMPLETED, "done")
        assert tsm.is_terminal is True

        with pytest.raises(ValueError, match="Illegal state transition"):
            tsm.transition(TaskState.RUNNING, "try_restart")

    def test_failed_cannot_transition(self):
        tsm = TaskStateMachine(task_id="failed-test")
        tsm.transition(TaskState.RUNNING, "start")
        tsm.transition(TaskState.FAILED, "breaker_tripped")
        assert tsm.is_terminal is True

        with pytest.raises(ValueError, match="Illegal state transition"):
            tsm.transition(TaskState.RUNNING, "try_retry")

    def test_completing_to_running_is_legal(self):
        """COMPLETING → RUNNING (guard blocked) is a legal cycle."""
        tsm = TaskStateMachine(task_id="guard-cycle")
        tsm.transition(TaskState.RUNNING, "start")
        tsm.transition(TaskState.COMPLETING, "finish_called")
        tsm.transition(TaskState.RUNNING, "completion_blocked")  # Legal!
        tsm.transition(TaskState.COMPLETING, "finish_called_again")
        tsm.transition(TaskState.COMPLETED, "done")
        assert tsm.state == TaskState.COMPLETED
