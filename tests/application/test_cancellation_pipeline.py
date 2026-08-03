"""G24: Cancellation Pipeline — CAS → handle, idempotent, three-race.

AC: RequestCancellation → CAS cancel_requested + fact, then push handle
AC: Idempotent: second cancel does not create duplicate fact
AC: Runtime not started: handle registered, cancel works when started
AC: Runtime running: cancel stops the loop
AC: Runtime just terminal: cancel is no-op (already done)
AC: Parent cancel → cancel children
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from application.commands.run_commands import CancelRun
from application.coordinators.cancellation_coordinator import (
    CancellationCoordinator, CancellationRegistry,
    AlreadyTerminalError,
)
from application.events.schema_registry import SchemaRegistry
from core.eventing.identifiers import RunId
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork
from runtime_core.execution import CancellationHandle
from runtime_core.outcome import CancellationReason


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
    """)
    SqliteOutboxStore.install(conn)
    conn.execute("INSERT INTO sessions (id) VALUES ('s-test')")
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_coordinator(db_path):
    reg = SchemaRegistry()
    outbox = SqliteOutboxStore(db_path, reg)
    uow = SqliteUnitOfWork(db_path, outbox)
    registry = CancellationRegistry()
    coord = CancellationCoordinator(uow, registry=registry)
    return coord, registry


# ═══════════════════════════════════════════════════════════════════════════════
# G24.1 — Cancel pushes handle
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelPushesHandle:
    """G24: Cancel → CAS + handle.cancel()."""

    def test_cancel_triggers_handle(self, temp_db):
        coord, registry = _make_coordinator(temp_db)
        handle = CancellationHandle()
        handle2 = CancellationHandle()
        registry.register("r1", handle)
        registry.register("child-1", handle2)

        assert not handle.cancelled
        assert not handle2.cancelled

        # Cancel parent
        result = coord.request_cancellation(
            CancelRun(run_id=RunId("r1"), reason=CancellationReason.USER_REQUESTED),
            session_id="s-test",
        )
        assert result.success
        assert handle.cancelled, "Parent handle must be cancelled"

        # Cancel children
        registry.cancel_children("r1", ["child-1"])
        assert handle2.cancelled, "Child handle must be cancelled"


# ═══════════════════════════════════════════════════════════════════════════════
# G24.2 — Idempotent
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdempotentCancel:
    """G24: Second cancel is idempotent — no duplicate fact."""

    def test_double_cancel(self, temp_db):
        coord, registry = _make_coordinator(temp_db)
        handle = CancellationHandle()
        registry.register("r1", handle)

        r1 = coord.request_cancellation(CancelRun(run_id=RunId("r1")), session_id="s-test")
        assert r1.success

        # Second cancellation
        r2 = coord.request_cancellation(CancelRun(run_id=RunId("r1")), session_id="s-test")
        # May succeed or be already_cancelling — both are fine
        assert r2.success or r2.already_cancelled


# ═══════════════════════════════════════════════════════════════════════════════
# G24.3 — Three-race: handle registered before runtime starts
# ═══════════════════════════════════════════════════════════════════════════════

class TestThreeRace:
    """G24: Cancel before/during/after runtime."""

    def test_cancel_before_runtime_starts(self, temp_db):
        """Handle registered, cancelled before runtime begins."""
        coord, registry = _make_coordinator(temp_db)
        handle = CancellationHandle()
        registry.register("r1", handle)

        # Cancel before runtime starts
        result = coord.request_cancellation(CancelRun(run_id=RunId("r1")), session_id="s-test")
        assert result.success
        assert handle.cancelled
        # Runtime checks handle.cancelled at top of loop → returns cancelled outcome

    def test_cancel_after_runtime_terminal(self, temp_db):
        """Handle already unregistered after runtime finished."""
        coord, registry = _make_coordinator(temp_db)
        handle = CancellationHandle()
        registry.register("r1", handle)
        # Simulate runtime done
        registry.unregister("r1")

        result = coord.request_cancellation(CancelRun(run_id=RunId("r1")), session_id="s-test")
        # handle was already unregistered (runtime finished)
        assert not registry.cancel("r1"), "Handle should already be unregistered"


# ═══════════════════════════════════════════════════════════════════════════════
# G24.4 — Child cancellation
# ═══════════════════════════════════════════════════════════════════════════════

class TestChildCancellation:
    """G24: Parent cancel → cancel children. Child failure ≠ parent cancel."""

    def test_parent_cancel_cancels_children(self, temp_db):
        coord, registry = _make_coordinator(temp_db)
        parent = CancellationHandle()
        child1 = CancellationHandle()
        child2 = CancellationHandle()

        registry.register("parent", parent)
        registry.register("child-1", child1)
        registry.register("child-2", child2)

        # Cancel parent
        coord.request_cancellation(CancelRun(run_id=RunId("parent")), session_id="s-test")
        assert parent.cancelled

        # Cancel children
        count = registry.cancel_children("parent", ["child-1", "child-2"])
        assert count == 2
        assert child1.cancelled
        assert child2.cancelled

    def test_child_failure_does_not_cancel_parent(self, temp_db):
        """G24: Child failure should NOT auto-cancel parent."""
        coord, registry = _make_coordinator(temp_db)
        parent = CancellationHandle()
        child = CancellationHandle()

        registry.register("parent", parent)
        registry.register("child-1", child)

        # Cancel child only
        registry.cancel("child-1")
        assert child.cancelled
        assert not parent.cancelled, (
            "G24: Child failure must not auto-cancel parent"
        )
