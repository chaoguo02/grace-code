"""P12: Runtime ports — acceptance tests.

AC: RuntimePorts is frozen dataclass.
AC: RuntimeOutcome factory methods produce correct status.
AC: RuntimeExecution is frozen snapshot.
"""

from __future__ import annotations

import pytest

from core.eventing.identifiers import SessionId, RunId
from runtime_core.model_actions import ModelAction
from runtime_core.ports import (
    RuntimePorts, ToolSuccess, HookGateResult,
)
from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import (
    RuntimeOutcome, RunStatus, CancellationReason,
)


# ── Fake ports (G15: all six ports are required) ─────────────────────────────

class _FakeLLM:
    def invoke(self, messages, tools=None, tool_choice=None):
        return ModelAction.stop(reason="test")


class _FakeTools:
    def execute(self, tool_name, params, invocation_id=""):
        return ToolSuccess(tool_name=tool_name)


class _FakeHooks:
    def check(self, event_type, hook_input, tool_name=""):
        return HookGateResult(allowed=True)


class _FakeLiveEvents:
    def publish(self, event_type, payload, scope=None):
        pass


class _FakeClock:
    def now(self):
        return 0.0

    def deadline(self, timeout_s):
        return timeout_s


class _FakeTokenUsage:
    def record(self, run_id, input_tokens, output_tokens):
        pass


def _ports() -> RuntimePorts:
    return RuntimePorts(
        llm=_FakeLLM(), tools=_FakeTools(), hooks=_FakeHooks(),
        live_events=_FakeLiveEvents(), clock=_FakeClock(),
        token_usage=_FakeTokenUsage(),
    )


class TestPorts:

    def test_ports_frozen(self):
        ports = _ports()
        with pytest.raises(Exception):
            ports.llm = None  # type: ignore

    def test_ports_require_all_six(self):
        """G15: all six ports are required — no Optional shortcuts."""
        with pytest.raises(TypeError):
            RuntimePorts()
        with pytest.raises(TypeError):
            RuntimePorts(llm=_FakeLLM())

    def test_ports_have_no_ui_concerns(self):
        """G15: web_mode / events / stats / cancellation were removed."""
        ports = _ports()
        for field in ("web_mode", "events", "stats", "cancellation"):
            assert not hasattr(ports, field)


class TestExecution:

    def test_execution_frozen(self):
        sid = SessionId("s1")
        rid = RunId("r1")
        ctx = RuntimeExecution(session_id=sid, run_id=rid)
        with pytest.raises(Exception):
            ctx.turn_index = 5  # type: ignore


class TestOutcome:

    def test_completed_factory(self):
        rid = RunId("r1")
        o = RuntimeOutcome.completed(rid, steps=10, tokens=500)
        assert o.status == RunStatus.COMPLETED
        assert o.steps_taken == 10

    def test_cancelled_factory(self):
        rid = RunId("r1")
        o = RuntimeOutcome.cancelled(rid, reason=CancellationReason.TIMEOUT)
        assert o.status == RunStatus.CANCELLED
        assert o.cancellation_reason == CancellationReason.TIMEOUT
