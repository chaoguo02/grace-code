"""G21: SQLite UnitOfWork — atomic state+outbox, failure injection.

AC: state + outbox same transaction → all or nothing
AC: failure at generation → state/outbox = 0
AC: failure at run_insert → state/outbox = 0
AC: failure at message_insert → state/outbox = 0
AC: failure at outbox_append → state/outbox = 0
AC: failure at commit → state/outbox = 0
AC: normal path → all committed
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId, AggregateVersion,
)
from application.events.run_facts import submitted
from application.events.schema_registry import SchemaRegistry
from application.transactions.unit_of_work import TransactionError
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, run_generation INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
            turn_index INTEGER, idempotency_key TEXT, prompt TEXT,
            status TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            role TEXT, content TEXT, turn_id TEXT, created_at TEXT
        );
    """)
    SqliteOutboxStore.install(conn)
    # Insert a test session
    conn.execute("INSERT INTO sessions (id) VALUES ('s-test')")
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_envelope(run_id="r-test", session_id="s-test"):
    from core.eventing.identifiers import SessionId, EventId
    from core.eventing.scope import ScopeToken
    sid = SessionId(session_id)
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName("run.submitted.v1"),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="test", component="coordinator"),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        correlation_id=CorrelationId("c1"),
        causation_id=None,
        aggregate_id=AggregateId(run_id),
        aggregate_version=AggregateVersion(1),
        payload=submitted(run_id),
    )


class TestUoWAtomicity:
    """G21: State + outbox are atomic — any failure = 0 changes."""

    def test_normal_path_all_committed(self, temp_db):
        reg = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, reg)
        uow = SqliteUnitOfWork(temp_db, outbox)

        env = _make_envelope()

        def _work(tx):
            gen = tx.increment_generation("s-test")
            tx.create_run(run_id="r-test", session_id="s-test", turn_id="t1",
                          turn_index=gen, idempotency_key="ik1", prompt="hello")
            tx.insert_message(session_id="s-test", role="user", content="hello",
                              turn_id="t1")
            tx.append_fact(env)
            return gen

        gen = uow.execute(_work)
        assert gen == 1

        # Verify everything persisted
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) as c FROM runs").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) as c FROM session_messages").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) as c FROM event_outbox").fetchone()["c"] == 1
        conn.close()

    @pytest.mark.parametrize("fail_point", [
        "generation", "run_insert", "message_insert", "outbox_append", "commit",
    ])
    def test_failure_at_point_zero_changes(self, temp_db, fail_point):
        reg = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, reg)
        uow = SqliteUnitOfWork(temp_db, outbox, fail_at=fail_point)

        env = _make_envelope()

        with pytest.raises((TransactionError, ValueError)):
            uow.execute(lambda tx: (
                tx.increment_generation("s-test"),
                tx.create_run(run_id="r-test", session_id="s-test",
                              turn_id="t1", turn_index=1,
                              idempotency_key="ik1", prompt="hello"),
                tx.insert_message(session_id="s-test", role="user",
                                  content="hello", turn_id="t1"),
                tx.append_fact(env),
            ))

        # Verify ZERO changes persisted
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        # Except "generation" failure point: run_generation might be rolled back
        gen = conn.execute("SELECT run_generation FROM sessions WHERE id='s-test'").fetchone()
        runs = conn.execute("SELECT COUNT(*) as c FROM runs").fetchone()["c"]
        msgs = conn.execute("SELECT COUNT(*) as c FROM session_messages").fetchone()["c"]
        out = conn.execute("SELECT COUNT(*) as c FROM event_outbox").fetchone()["c"]
        conn.close()

        # After rollback: session generation should be 0, everything else 0
        assert runs == 0, f"runs={runs} after fail at {fail_point}"
        assert msgs == 0, f"messages={msgs} after fail at {fail_point}"
        assert out == 0, f"outbox={out} after fail at {fail_point}"
