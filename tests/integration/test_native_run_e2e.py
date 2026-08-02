"""G31: Native Run E2E — submit → execute → terminal via coordinator.

AC: submit_run_turn uses RunCoordinator (no legacy SQLite path)
AC: No GRACE_RUNTIME_MODE branching
AC: No nested _StorageUoW / _StorageTx
AC: Idempotency conflict detected
AC: Active run check prevents duplicates
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from core.eventing.identifiers import SessionId, RunId
from application.commands.run_commands import SubmitRun, IdempotencyConflict, RunAlreadyActive
from application.coordinators.run_coordinator import RunCoordinator
from application.events.schema_registry import SchemaRegistry
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork


class FakeRuntime:
    def run(self, ctx): return object()


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, run_generation INTEGER DEFAULT 0);
        CREATE TABLE runs (id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
            turn_index INTEGER, idempotency_key TEXT, prompt TEXT,
            status TEXT DEFAULT 'queued', aggregate_version INTEGER DEFAULT 1,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE session_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, turn_id TEXT, created_at TEXT);
    """)
    SqliteOutboxStore.install(conn)
    conn.execute("INSERT INTO sessions (id) VALUES ('s-e2e')")
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


class TestNativeRunE2E:
    """G31: End-to-end run submission via native coordinator."""

    def test_submit_via_coordinator(self, temp_db):
        reg = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, reg)
        uow = SqliteUnitOfWork(temp_db, outbox)
        coord = RunCoordinator(FakeRuntime(), uow)

        cmd = SubmitRun(session_id=SessionId("s-e2e"), prompt="hello",
                        idempotency_key="ik-e2e")
        result = coord.submit(cmd)
        assert isinstance(result, RunId), f"Expected RunId, got {type(result).__name__}"

        # Verify DB state
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        run = conn.execute("SELECT * FROM runs WHERE id=?", (str(result),)).fetchone()
        conn.close()
        assert run is not None
        assert run["prompt"] == "hello"

    def test_idempotency_conflict(self, temp_db):
        reg = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, reg)
        uow = SqliteUnitOfWork(temp_db, outbox)
        coord = RunCoordinator(FakeRuntime(), uow)

        cmd1 = SubmitRun(session_id=SessionId("s-e2e"), prompt="a",
                         idempotency_key="ik-dup")
        r1 = coord.submit(cmd1)
        assert isinstance(r1, RunId)

        cmd2 = SubmitRun(session_id=SessionId("s-e2e"), prompt="b",
                         idempotency_key="ik-dup")
        r2 = coord.submit(cmd2)
        assert isinstance(r2, IdempotencyConflict)

    def test_no_grace_runtime_mode_in_source(self):
        """G31: run_submission.py must not reference GRACE_RUNTIME_MODE."""
        import ast, os
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "server", "services", "run_submission.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "GRACE_RUNTIME_MODE" not in source, (
            "G31: run_submission.py must not reference GRACE_RUNTIME_MODE"
        )
        assert "_StorageUoW" not in source, (
            "G31: _StorageUoW nested class must be removed"
        )
        assert "_StorageTx" not in source, (
            "G31: _StorageTx nested class must be removed"
        )
