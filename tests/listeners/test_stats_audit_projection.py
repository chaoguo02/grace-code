"""G26: Stats + Audit Projection — explicit visitors, persistent, idempotent.

AC: Stats uses explicit event types (no startswith catch-all)
AC: Stats persists to DB (not just in-process list)
AC: Audit stores source/id/correlation/causation/scope/version/digest
AC: Both idempotent by event_id
AC: Neither imports publisher/command/coordinator
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.eventing.identifiers import SessionId, EventId, AggregateVersion
from core.eventing.scope import ScopeToken
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import completed, submitted
from listeners.stats_projection import StatsProjection
from listeners.audit_projection import AuditProjection


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    conn = sqlite3.connect(db)
    StatsProjection.install(conn)
    AuditProjection.install(conn)
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_envelope(event_type="run.completed.v1", run_id="r-test",
                   session_id="s-test", **kw):
    sid = SessionId(session_id)
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName(event_type),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="test", component="runtime"),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        correlation_id=CorrelationId("corr-1"),
        causation_id=None,
        aggregate_id=AggregateId(run_id),
        aggregate_version=AggregateVersion(1),
        payload=completed(run_id) if "completed" in event_type else submitted(run_id),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# G26.1 — Stats: explicit visitors, no catch-all
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatsExplicit:
    """G26: Stats uses explicit SUPPORTED_TYPES, no startswith catch-all."""

    def test_known_event_recorded(self):
        sp = StatsProjection()
        env = _make_envelope("run.completed.v1")
        r = sp.on_event(env)
        assert r.success
        assert len(sp.metrics) == 1

    def test_unknown_event_not_recorded(self):
        sp = StatsProjection()
        env = _make_envelope("tool.executed.v1")
        r = sp.on_event(env)
        assert r.success  # not an error, just skipped
        assert len(sp.metrics) == 0, (
            "G26: stats must not record unregistered event types"
        )

    def test_no_startswith_catch_all(self):
        """Verify StatsProjection uses explicit matching (no startswith calls)."""
        import ast, os
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "listeners", "stats_projection.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == "startswith":
                        pytest.fail("G26: stats must not use .startswith() catch-all")


    def test_persists_to_db(self, temp_db):
        sp = StatsProjection(db_path=temp_db)
        env = _make_envelope("run.completed.v1")
        sp.on_event(env)

        conn = sqlite3.connect(temp_db)
        count = conn.execute("SELECT COUNT(*) FROM run_metrics").fetchone()[0]
        conn.close()
        assert count == 1, "Stats must persist to DB"


# ═══════════════════════════════════════════════════════════════════════════════
# G26.2 — Audit: full envelope fields
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditFields:
    """G26: Audit stores source/id/correlation/causation/scope/version/digest."""

    def test_audit_stores_all_fields(self, temp_db):
        ap = AuditProjection(temp_db)
        env = _make_envelope("run.completed.v1")
        r = ap.on_event(env)
        assert r.success

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        conn.close()

        assert row["event_id"] == str(env.event_id)
        assert row["source"] == str(env.source)
        assert row["correlation_id"] == str(env.correlation_id)
        assert row["scope_kind"] == "session"
        assert row["aggregate_id"] == str(env.aggregate_id)
        assert row["aggregate_version"] == 1
        assert len(row["payload_digest"]) == 64  # SHA-256

    def test_audit_idempotent(self, temp_db):
        ap = AuditProjection(temp_db)
        env = _make_envelope("run.completed.v1")
        r1 = ap.on_event(env)
        assert r1.success
        r2 = ap.on_event(env)  # same event
        assert r2.success

        conn = sqlite3.connect(temp_db)
        count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        conn.close()
        assert count == 1, "Duplicate must not create second audit row"


# ═══════════════════════════════════════════════════════════════════════════════
# G26.3 — No forbidden imports
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoForbiddenImports:
    """G26: Projections don't import publisher/command/coordinator."""

    @pytest.mark.parametrize("module_name", [
        "listeners.stats_projection",
        "listeners.audit_projection",
    ])
    def test_no_forbidden_imports(self, module_name):
        import ast, importlib
        mod = importlib.import_module(module_name)
        with open(mod.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                m = getattr(node, 'module', '') or ''
                if any(f in m for f in ('publisher', 'command', 'coordinator')):
                    pytest.fail(f"{module_name} imports {m}")
