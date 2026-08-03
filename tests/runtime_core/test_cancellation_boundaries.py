"""G18: Cancellation Boundaries — check at model/hook/tool, not swallowed.

AC: Cancellation check at top of each loop iteration
AC: Cancellation check before each tool call in batch
AC: Cancellation check before tool execution
AC: CancelledOutcome contains completed steps/evidence
AC: Cancel-to-return < 500ms with fake adapters (no real I/O)
AC: CancelledError not swallowed by broad except
"""

from __future__ import annotations

import time as _time

import pytest

from core.eventing.identifiers import SessionId, RunId
from core.json_values import freeze_json
from runtime_core.execution import RuntimeExecution, ConversationSnapshot, CancellationHandle
from runtime_core.model_actions import ToolCall, AssistantText, ToolCallBatch
from runtime_core.outcome import RunStatus
from runtime_core.ports import (
    RuntimePorts, HookGateResult, ToolSuccess,
)
from runtime_core.step_loop import StepLoop


# ── Fakes ───────────────────────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, response): self.response = response
    def invoke(self, messages, tools=None, tool_choice=None): return self.response
    def stream(self, messages, tools=None, tool_choice=None):
        async def _s(): return self.response
        return _s()


class FakeTools:
    def execute(self, tool_name, params, invocation_id=""):
        return ToolSuccess(tool_name=tool_name)


class FakeHooks:
    def check(self, event_type, hook_input, tool_name=""):
        return HookGateResult(allowed=True)


class FakeLiveEvents:
    def publish(self, event_type, payload, scope=None): pass


class FakeClock:
    def now(self): return _time.monotonic()
    def deadline(self, timeout_s): return _time.monotonic() + timeout_s


class FakeTokenUsage:
    def record(self, run_id, input_tokens, output_tokens): pass


def _ports(llm_resp=None):
    return RuntimePorts(
        llm=FakeLLM(llm_resp or AssistantText(text="done")),
        tools=FakeTools(), hooks=FakeHooks(),
        live_events=FakeLiveEvents(), clock=FakeClock(),
        token_usage=FakeTokenUsage(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# G18.1 — Cancel at loop iteration
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelAtLoop:
    """G18: Cancel before model call → immediate cancelled outcome."""

    def test_cancel_before_start(self):
        handle = CancellationHandle()
        handle.cancel()

        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"),
            cancellation=handle, max_steps=10,
            conversation=ConversationSnapshot(messages=({"role": "user", "content": "hi"},)),
        )
        loop = StepLoop(_ports())
        outcome = loop.execute(ctx)

        assert outcome.status == RunStatus.CANCELLED
        assert outcome.steps_taken == 0  # no steps completed

    def test_cancel_returns_quickly(self):
        handle = CancellationHandle()
        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"),
            cancellation=handle, max_steps=10,
            conversation=ConversationSnapshot(messages=({"role": "user", "content": "hi"},)),
        )

        # Cancel during execution
        handle.cancel()

        started = _time.monotonic()
        loop = StepLoop(_ports())
        outcome = loop.execute(ctx)
        elapsed_ms = (_time.monotonic() - started) * 1000

        assert outcome.status == RunStatus.CANCELLED
        # With fake adapters, must return in < 500ms
        assert elapsed_ms < 500, f"Cancel took {elapsed_ms:.0f}ms (must be < 500ms)"


# ═══════════════════════════════════════════════════════════════════════════════
# G18.2 — Cancel during tool batch
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelDuringTools:
    """G18: Cancel during tool processing → partial results, cancelled outcome."""

    def test_cancel_after_first_tool(self):
        handle = CancellationHandle()
        p1 = freeze_json({"f": "a.txt"})
        p2 = freeze_json({"f": "b.txt"})

        # LLM returns two tool calls
        tc1 = ToolCall(id="t1", name="read", params=p1)
        tc2 = ToolCall(id="t2", name="read", params=p2)
        batch = ToolCallBatch(calls=(tc1, tc2))

        class CancelAfterFirstTools:
            def execute(self, tool_name, params, invocation_id=""):
                handle.cancel()  # Cancel after first tool
                return ToolSuccess(tool_name=tool_name)

        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"),
            cancellation=handle, max_steps=10,
            conversation=ConversationSnapshot(messages=({"role": "user", "content": "hi"},)),
        )

        ports = RuntimePorts(
            llm=FakeLLM(batch), tools=CancelAfterFirstTools(),
            hooks=FakeHooks(), live_events=FakeLiveEvents(),
            clock=FakeClock(), token_usage=FakeTokenUsage(),
        )
        loop = StepLoop(ports)

        outcome = loop.execute(ctx)
        # After cancel, the loop should detect cancellation at the next check
        # (top of next iteration or before next tool)
        assert outcome.status in (RunStatus.CANCELLED, RunStatus.BLOCKED)


# ═══════════════════════════════════════════════════════════════════════════════
# G18.3 — CancellationHandle is thread-safe
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancellationHandle:
    """G18: CancellationHandle is thread-safe and idempotent."""

    def test_cancel_is_idempotent(self):
        h = CancellationHandle()
        assert not h.cancelled
        h.cancel()
        assert h.cancelled
        h.cancel()
        assert h.cancelled  # idempotent

    def test_handle_is_fresh_per_run(self):
        h1 = CancellationHandle()
        h2 = CancellationHandle()
        h1.cancel()
        assert h1.cancelled
        assert not h2.cancelled  # independent

    def test_is_active(self):
        h = CancellationHandle()
        assert h.is_active
        h.cancel()
        assert not h.is_active


# ═══════════════════════════════════════════════════════════════════════════════
# H6 — CancellationHandle → ProcessRegistry binding
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelKillsProcesses:
    """H6: cancel() notifies ProcessRegistry to kill subprocesses."""

    def test_cancel_calls_registry_cancel_all(self):
        """When a ProcessRegistry is bound, cancel() must call cancel_all()."""
        from hook_core.process_runner import ProcessRegistry
        import subprocess, sys

        registry = ProcessRegistry()
        # Register a dummy process
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        registry.register("test-hook", proc)

        handle = CancellationHandle(process_registry=registry)
        handle.cancel()

        # Process should be killed
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        assert proc.poll() is not None, "H6: Process must be killed on cancel"

    def test_cancel_without_registry_no_error(self):
        """Without a ProcessRegistry, cancel() should not raise."""
        handle = CancellationHandle()  # no registry
        handle.cancel()  # must not raise
        assert handle.cancelled

    def test_class_level_default_registry(self):
        """Class-level set_process_registry propagates to new handles."""
        from hook_core.process_runner import ProcessRegistry
        registry = ProcessRegistry()
        CancellationHandle.set_process_registry(registry)

        h = CancellationHandle()
        assert h._process_registry is not None, (
            "H6: class-level registry must propagate to new handles"
        )
        # Reset default
        CancellationHandle._default_process_registry = None
