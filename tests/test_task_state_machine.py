"""Unit tests for agent/v2/task_state_machine.py — Explicit Task State Machine."""

import pytest
from agent.v2.task_state_machine import (
    TaskState,
    TaskStateMachine,
    _ALLOWED_TRANSITIONS,
    _TERMINAL_STATES,
)


class TestTaskStateEnum:
    def test_all_states_defined(self):
        assert TaskState.PENDING.value == "pending"
        assert TaskState.RUNNING.value == "running"
        assert TaskState.COMPLETING.value == "completing"
        assert TaskState.COMPLETED.value == "completed"
        assert TaskState.FAILED.value == "failed"
        assert TaskState.CANCELLED.value == "cancelled"

    def test_terminal_states(self):
        assert TaskState.COMPLETED in _TERMINAL_STATES
        assert TaskState.FAILED in _TERMINAL_STATES
        assert TaskState.CANCELLED in _TERMINAL_STATES
        assert TaskState.RUNNING not in _TERMINAL_STATES
        assert TaskState.PENDING not in _TERMINAL_STATES


class TestTaskStateMachineTransitions:
    def test_initial_state_is_pending(self):
        tsm = TaskStateMachine(task_id="t1")
        assert tsm.state == TaskState.PENDING
        assert not tsm.is_terminal

    def test_pending_to_running(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        assert tsm.state == TaskState.RUNNING
        assert tsm.is_running
        assert not tsm.is_terminal

    def test_running_to_completing(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.COMPLETING, "model called FINISH")
        assert tsm.state == TaskState.COMPLETING

    def test_completing_to_completed(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.COMPLETING)
        tsm.transition(TaskState.COMPLETED, "all guards passed")
        assert tsm.state == TaskState.COMPLETED
        assert tsm.is_terminal

    def test_completing_back_to_running(self):
        """Guard blocks: model must continue working."""
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.COMPLETING)
        tsm.transition(TaskState.RUNNING, "completion blocked by guard")
        assert tsm.state == TaskState.RUNNING
        assert not tsm.is_terminal

    def test_running_to_failed(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.FAILED, "circuit breaker tripped")
        assert tsm.state == TaskState.FAILED
        assert tsm.is_terminal

    def test_running_to_cancelled(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.CANCELLED, "user interrupted")
        assert tsm.state == TaskState.CANCELLED
        assert tsm.is_terminal


class TestTaskStateMachineIllegalTransitions:
    def test_pending_to_completed_illegal(self):
        tsm = TaskStateMachine(task_id="t1")
        with pytest.raises(ValueError, match="Illegal state transition"):
            tsm.transition(TaskState.COMPLETED)

    def test_pending_to_failed_illegal(self):
        tsm = TaskStateMachine(task_id="t1")
        with pytest.raises(ValueError, match="Illegal state transition"):
            tsm.transition(TaskState.FAILED)

    def test_completed_any_transition_illegal(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.COMPLETING)
        tsm.transition(TaskState.COMPLETED)
        with pytest.raises(ValueError, match="Illegal state transition"):
            tsm.transition(TaskState.RUNNING)

    def test_failed_any_transition_illegal(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.FAILED)
        with pytest.raises(ValueError, match="Illegal state transition"):
            tsm.transition(TaskState.COMPLETED)

    def test_running_to_completed_illegal(self):
        """Cannot jump directly from RUNNING to COMPLETED — must go through COMPLETING."""
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        with pytest.raises(ValueError, match="Illegal state transition"):
            tsm.transition(TaskState.COMPLETED)


class TestTaskStateMachineStepTracking:
    def test_record_step_increments(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        assert tsm.record_step() == 1
        assert tsm.record_step() == 2
        assert tsm.record_step() == 3
        assert tsm.step_count == 3

    def test_record_step_in_pending_warns_but_works(self):
        tsm = TaskStateMachine(task_id="t1")
        # Should still count even in non-RUNNING state (warns but doesn't crash)
        tsm.record_step()
        assert tsm.step_count == 1


class TestTaskStateMachineTiming:
    def test_elapsed_time_tracks_from_start(self):
        import time
        tsm = TaskStateMachine(task_id="t1")
        assert tsm.elapsed_seconds == 0.0
        tsm.transition(TaskState.RUNNING)
        time.sleep(0.01)
        assert tsm.elapsed_seconds > 0

    def test_elapsed_time_stops_at_terminal(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.COMPLETING)
        tsm.transition(TaskState.COMPLETED)
        elapsed = tsm.elapsed_seconds
        import time
        time.sleep(0.01)
        # Should not increase after terminal state
        assert tsm.elapsed_seconds == elapsed


class TestTaskStateMachineHistory:
    def test_history_records_all_transitions(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING, "start task")
        tsm.transition(TaskState.COMPLETING, "model called FINISH")
        tsm.transition(TaskState.COMPLETED, "guards passed")
        assert len(tsm.history) == 3
        assert tsm.history[0][0] == TaskState.RUNNING
        assert tsm.history[1][0] == TaskState.COMPLETING
        assert tsm.history[2][0] == TaskState.COMPLETED

    def test_history_includes_reason_and_timestamp(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING, "start test")
        state, timestamp, reason = tsm.history[0]
        assert state == TaskState.RUNNING
        assert reason == "start test"
        assert isinstance(timestamp, float)


class TestTaskStateMachineHooks:
    def test_on_transition_callback(self):
        transitions = []

        def on_transition(prev, to, reason):
            transitions.append((prev, to, reason))

        tsm = TaskStateMachine(task_id="t1")
        tsm._on_transition = on_transition
        tsm.transition(TaskState.RUNNING, "start")
        tsm.transition(TaskState.COMPLETING, "finish")
        assert len(transitions) == 2
        assert transitions[0] == (TaskState.PENDING, TaskState.RUNNING, "start")
        assert transitions[1] == (TaskState.RUNNING, TaskState.COMPLETING, "finish")


class TestTaskStateMachineSerialization:
    def test_to_summary(self):
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.record_step()
        s = tsm.to_summary()
        assert s["task_id"] == "t1"
        assert s["state"] == "running"
        assert s["step_count"] == 1
        assert not s["is_terminal"]

    def test_to_summary_terminal(self):
        tsm = TaskStateMachine(task_id="t2")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.FAILED)
        s = tsm.to_summary()
        assert s["is_terminal"]


class TestTaskStateMachineToRunStatus:
    def test_completed_to_success(self):
        from agent.task import RunStatus
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.COMPLETING)
        tsm.transition(TaskState.COMPLETED)
        assert tsm.to_run_status() == RunStatus.SUCCESS

    def test_failed_to_failed(self):
        from agent.task import RunStatus
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.FAILED)
        assert tsm.to_run_status() == RunStatus.FAILED

    def test_cancelled_to_gave_up(self):
        from agent.task import RunStatus
        tsm = TaskStateMachine(task_id="t1")
        tsm.transition(TaskState.RUNNING)
        tsm.transition(TaskState.CANCELLED)
        assert tsm.to_run_status() == RunStatus.GAVE_UP
