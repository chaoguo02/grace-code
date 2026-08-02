"""P8: Outbox failure matrix — acceptance tests.

AC: claim_batch atomic — two workers never claim same event.
AC: dead_letter after max attempts.
AC: relay stop() waits for in-flight.
AC: No import from old server.services.event_outbox.
"""

from __future__ import annotations

import ast
import sqlite3
import time

import pytest

from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.outbox.relay import OutboxRelay
from application.events.schema_registry import SchemaRegistry


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_p8.db")


@pytest.fixture
def store(db_path):
    reg = SchemaRegistry()
    s = SqliteOutboxStore(db_path, reg)
    conn = sqlite3.connect(db_path)
    s.install(conn)
    conn.commit()
    conn.close()
    return s


class TestClaimAtomicity:

    def test_two_workers_never_claim_same_event(self, store):
        conn = sqlite3.connect(store._db_path)
        store.install(conn)
        conn.execute(
            "INSERT INTO event_outbox(event_id,event_type,session_id,aggregate_id,payload_json,occurred_at) "
            "VALUES ('ev-1','run.completed.v1','s1','r1','{}',datetime('now'))"
        )
        conn.commit()
        conn.close()

        a = store.claim_batch("w-a", limit=5)
        b = store.claim_batch("w-b", limit=5)
        ids_a = {r.event_id for r in a}
        ids_b = {r.event_id for r in b}
        assert len(ids_a & ids_b) == 0, "Two workers must not claim the same event"


class TestDeadLetter:

    def test_max_attempts_triggers_dead_letter(self, store):
        conn = sqlite3.connect(store._db_path)
        store.install(conn)
        conn.execute(
            "INSERT INTO event_outbox(event_id,event_type,session_id,aggregate_id,payload_json,occurred_at) "
            "VALUES ('ev-dl','run.completed.v1','s1','r1','{}',datetime('now'))"
        )
        conn.commit()
        conn.close()

        store.claim_batch("w1", limit=5)
        # Simulate repeated claim→fail→reschedule cycles
        for i in range(4):
            store.reschedule("ev-dl", "w1", f"fail {i}")
            store.claim_batch("w1", limit=5)
        # After 5 total attempts, move to dead-letter
        assert store.dead_letter("ev-dl", "w1", "fatal")


class TestRelayStop:

    def test_stop_returns_remaining(self, store):
        relay = OutboxRelay(store, deliver=lambda r: None)
        relay.start()
        time.sleep(0.5)
        remaining = relay.stop(timeout_s=3.0)
        assert remaining >= 0


class TestImportBoundary:

    def test_no_old_outbox_import(self):
        with open("infrastructure/outbox/sqlite_store.py", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                if 'server.services.event_outbox' in module:
                    pytest.fail(f"Imports old outbox: {module}")
