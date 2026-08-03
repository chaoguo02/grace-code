"""P13: Runtime isolation — acceptance tests.

AC: Runtime does not write to DB (uses fake ports).
AC: StepLoop produces correct outcome.
AC: No mock listener — only fake ports.
"""

from __future__ import annotations

import pytest

from core.eventing.identifiers import SessionId, RunId
from runtime_core.model_actions import AssistantText
from runtime_core.ports import (
    RuntimePorts, HookGateResult, ToolSuccess,
)
from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import RuntimeOutcome, RunStatus
from runtime_core.step_loop import StepLoop
from runtime_core.runtime import AgentRuntime


# ── Fake ports (G15: all six ports required) ─────────────────────────────────

class FakeLiveEvents:
    def __init__(self):
        self.published: list = []

    def publish(self, event_type, payload, scope=None) -> None:
        self.published.append((event_type, payload))


class FakeLLMPort:
    def invoke(self, messages, tools=None, tool_choice=None):
        return AssistantText(text="done")

    def stream(self, messages, tools=None, tool_choice=None):
        async def _s():
            return AssistantText(text="done")
        return _s()


class FakeTools:
    def execute(self, tool_name, params, invocation_id=""):
        return ToolSuccess(tool_name=tool_name)


class FakeHooks:
    def check(self, event_type, hook_input, tool_name=""):
        return HookGateResult(allowed=True)


class FakeClock:
    def now(self):
        return 0.0

    def deadline(self, timeout_s):
        return timeout_s


class FakeTokenUsage:
    def __init__(self):
        self.records: list = []

    def record(self, run_id, input_tokens, output_tokens):
        self.records.append((run_id, input_tokens, output_tokens))


def _ports(live_events=None):
    return RuntimePorts(
        llm=FakeLLMPort(),
        tools=FakeTools(),
        hooks=FakeHooks(),
        live_events=live_events or FakeLiveEvents(),
        clock=FakeClock(),
        token_usage=FakeTokenUsage(),
    )


class TestStepLoop:

    def test_completes(self):
        ports = _ports()
        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=3,
        )
        loop = StepLoop(ports)
        outcome = loop.execute(ctx)
        assert outcome.status == RunStatus.COMPLETED
        assert outcome.steps_taken > 0

    def test_stops_at_max_steps(self):
        ports = _ports()
        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=2,
        )
        loop = StepLoop(ports)
        outcome = loop.execute(ctx)
        assert outcome.steps_taken <= 2


class TestAgentRuntime:

    def test_run_publishes_outcome(self):
        events = FakeLiveEvents()
        ports = _ports(live_events=events)
        rt = AgentRuntime(ports)
        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=1,
        )
        outcome = rt.run(ctx)
        assert outcome.status == RunStatus.COMPLETED
        assert len(events.published) == 1

    def test_no_db_in_runtime(self):
        """Runtime module must not import sqlite3 or any DB adapter."""
        import ast
        for mod_name in ["runtime_core.runtime", "runtime_core.step_loop"]:
            mod = __import__(mod_name, fromlist=[""])
            with open(mod.__file__, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, 'module', '') or ''
                    if 'sqlite' in module or 'session_store' in module:
                        pytest.fail(f"{mod_name} imports {module}")
