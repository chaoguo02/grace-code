"""P17: Stats Projection — acceptance tests."""

import uuid
from datetime import datetime, timezone

from application.events.envelope import EventEnvelope, EventTypeName, SchemaVersion, EventSource, CorrelationId, AggregateId
from application.events.run_facts import RunSubmittedV1, RunCompletedV1
from application.events.tool_facts import ToolExecutedV1
from core.eventing.identifiers import EventId, RunId, AggregateVersion, SessionId
from core.eventing.scope import ScopeToken
from listeners.stats_projection import StatsProjection


def _env(etype: str):
    sid = SessionId("s1")
    return EventEnvelope(
        event_id=EventId.generate(), event_type=EventTypeName(etype),
        schema_version=SchemaVersion(1), occurred_at=datetime.now(timezone.utc),
        source=EventSource("test", "runtime"), correlation_id=CorrelationId("c1"),
        causation_id=None, aggregate_id=AggregateId("r1"),
        aggregate_version=AggregateVersion(1),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        payload=RunSubmittedV1(run_id=RunId("r1")),
    )


class TestStatsProjection:

    def test_run_events_recorded(self):
        proj = StatsProjection()
        proj.on_event(_env("run.submitted.v1"))
        proj.on_event(_env("run.completed.v1"))
        assert len(proj.metrics) == 2

    def test_tool_events_not_recorded(self):
        proj = StatsProjection()
        proj.on_event(_env("tool.executed.v1"))
        # tool.* events don't start with "run." — should not be recorded
        assert len(proj.metrics) == 0
