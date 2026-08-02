"""P12: Runtime ports — acceptance tests.

AC: RuntimePorts is frozen dataclass.
AC: RuntimeOutcome factory methods produce correct status.
AC: RuntimeExecution is frozen snapshot.
"""

from __future__ import annotations

import pytest

from core.eventing.identifiers import SessionId, RunId
from runtime_core.ports import RuntimePorts
from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import (
    RuntimeOutcome, RunStatus, CancellationReason,
)


class TestPorts:

    def test_ports_frozen(self):
        ports = RuntimePorts(web_mode=True)
        with pytest.raises(Exception):
            ports.web_mode = False  # type: ignore

    def test_ports_default_none(self):
        ports = RuntimePorts()
        assert ports.events is None
        assert ports.stats is None


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
