"""P13: Runtime isolation — acceptance tests.

AC: Runtime does not write to DB (uses fake ports).
AC: StepLoop produces correct outcome.
AC: No mock listener — only fake ports.
"""

from __future__ import annotations

import pytest

from core.eventing.identifiers import SessionId, RunId
from runtime_core.ports import RuntimePorts
from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import RuntimeOutcome, RunStatus
from runtime_core.step_loop import StepLoop
from runtime_core.runtime import AgentRuntime


class FakeEventPort:
    def __init__(self):
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)


class FakeLLMPort:
    def invoke(self, messages, tools):
        return {"role": "assistant", "content": "done", "stop_reason": "end_turn"}


class FakeContextPort:
    def get_context(self, session_id):
        return {"messages": []}


class TestStepLoop:

    def test_completes(self):
        ports = RuntimePorts(
            llm=FakeLLMPort(),
            context=FakeContextPort(),
        )
        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=3,
        )
        loop = StepLoop(ports)
        outcome = loop.execute(ctx)
        assert outcome.status == RunStatus.COMPLETED
        assert len(loop.steps) > 0

    def test_stops_at_max_steps(self):
        ports = RuntimePorts(
            llm=FakeLLMPort(),
            context=FakeContextPort(),
        )
        ctx = RuntimeExecution(
            session_id=SessionId("s1"), run_id=RunId("r1"), max_steps=2,
        )
        loop = StepLoop(ports)
        outcome = loop.execute(ctx)
        assert len(loop.steps) <= 2


class TestAgentRuntime:

    def test_run_publishes_outcome(self):
        events = FakeEventPort()
        ports = RuntimePorts(
            events=events, llm=FakeLLMPort(), context=FakeContextPort(),
        )
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
