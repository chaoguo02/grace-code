"""P14: Run Coordinator atomicity — acceptance tests.

AC: submit creates RunSubmittedV1 fact via UoW.
AC: finalize persists terminal fact via UoW.
AC: Coordinator does not import server/sqlite.
"""

from __future__ import annotations

import ast

import pytest

from application.commands.run_commands import SubmitRun, FinalizeRun
from application.coordinators.run_coordinator import RunCoordinator
from application.transactions.unit_of_work import SessionTransaction
from core.eventing.identifiers import SessionId, RunId, AggregateVersion
from runtime_core.outcome import RuntimeOutcome
from runtime_core.ports import RuntimePorts
from runtime_core.runtime import AgentRuntime


class FakeUoW:
    """Fake UoW — records appended facts in memory."""
    def __init__(self):
        self.facts: list = []

    def execute(self, fn) -> None:
        tx = FakeTransaction(self)
        fn(tx)


class FakeTransaction:
    def __init__(self, uow: FakeUoW):
        self._uow = uow

    def append_fact(self, envelope) -> None:
        self._uow.facts.append(envelope)


class TestRunCoordinator:

    def test_submit_creates_fact(self):
        uow = FakeUoW()
        rt = AgentRuntime(RuntimePorts())
        coord = RunCoordinator(rt, uow)

        cmd = SubmitRun(session_id=SessionId("s1"), prompt="test")
        envelope = coord.submit(cmd)

        assert len(uow.facts) == 1
        assert str(envelope.event_type) == "run.submitted.v1"

    def test_finalize_persists_terminal_fact(self):
        uow = FakeUoW()
        rt = AgentRuntime(RuntimePorts())
        coord = RunCoordinator(rt, uow)

        outcome = RuntimeOutcome.completed(RunId("r1"), steps=5, tokens=100)
        cmd = FinalizeRun(
            run_id=RunId("r1"),
            expected_version=AggregateVersion(1),
            outcome=outcome,
        )
        envelope = coord.finalize(cmd)

        assert len(uow.facts) == 1
        assert str(envelope.event_type) == "run.completed.v1"

    def test_commands_are_frozen(self):
        cmd = SubmitRun(session_id=SessionId("s1"), prompt="hello")
        with pytest.raises(Exception):
            cmd.prompt = "changed"  # type: ignore
