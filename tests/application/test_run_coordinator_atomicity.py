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
    """Fake UoW — records appended facts and state mutations in memory."""
    def __init__(self):
        self.facts: list = []
        self.runs: list[dict] = []
        self.messages: list[dict] = []
        self.generations: dict[str, int] = {}

    def execute(self, fn):
        tx = FakeTransaction(self)
        return fn(tx)


class FakeTransaction:
    def __init__(self, uow: FakeUoW):
        self._uow = uow

    def increment_generation(self, session_id) -> int:
        sid = str(session_id)
        current = self._uow.generations.get(sid, 0)
        new_val = current + 1
        self._uow.generations[sid] = new_val
        return new_val

    def create_run(self, *, run_id, session_id, turn_id, turn_index,
                   idempotency_key: str = "", prompt: str = "") -> None:
        self._uow.runs.append({
            "run_id": str(run_id), "session_id": str(session_id),
            "turn_id": turn_id, "turn_index": turn_index,
            "idempotency_key": idempotency_key, "prompt": prompt,
            "status": "queued",
        })

    def insert_message(self, *, session_id, role: str, content: str,
                       turn_id: str) -> None:
        self._uow.messages.append({
            "session_id": str(session_id), "role": role,
            "content": content, "turn_id": turn_id,
        })

    def append_fact(self, envelope) -> None:
        self._uow.facts.append(envelope)


class TestRunCoordinator:

    def test_submit_creates_fact_and_state(self):
        uow = FakeUoW()
        rt = AgentRuntime(RuntimePorts())
        coord = RunCoordinator(rt, uow)

        cmd = SubmitRun(session_id=SessionId("s1"), prompt="test")
        envelope = coord.submit(cmd)

        # Fact must be appended
        assert len(uow.facts) == 1
        assert str(envelope.event_type) == "run.submitted.v1"

        # State mutations must occur in same transaction
        assert len(uow.runs) == 1
        assert uow.runs[0]["session_id"] == "s1"
        assert uow.runs[0]["prompt"] == "test"
        assert uow.runs[0]["status"] == "queued"

        assert len(uow.messages) == 1
        assert uow.messages[0]["role"] == "user"
        assert uow.messages[0]["content"] == "test"

        assert uow.generations.get("s1", 0) >= 1

        # Envelope payload must carry real metadata
        assert envelope.payload.turn_index >= 1
        assert envelope.payload.turn_id  # must be non-empty

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
