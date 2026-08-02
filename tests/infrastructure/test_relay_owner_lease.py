"""G10: Relay Owner Lease — durable DB lease, takeover, heartbeat.

AC: acquire succeeds when no active lease
AC: second acquire while lease active → LeaseConflictError
AC: crashed owner (expired lease) → new owner can acquire (takeover)
AC: heartbeat keeps lease alive
AC: heartbeat failure → returns False
AC: release marks lease as released
AC: start requires prior acquire
AC: stop releases lease
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from infrastructure.outbox.owner_lease import (
    OwnerLease,
    LeaseConflictError,
    LEASE_TIMEOUT_S,
)
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.outbox.relay import OutboxRelay
from application.events.schema_registry import SchemaRegistry


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    # Install tables
    conn = sqlite3.connect(db)
    OwnerLease.install(conn)
    SqliteOutboxStore.install(conn)
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════
# G10.1 — Acquire / Conflict / Takeover
# ═══════════════════════════════════════════════════════════════════════════════

class TestAcquireConflict:
    """G10: Acquire succeeds alone; second acquire fails."""

    def test_acquire_succeeds_when_no_lease(self, temp_db):
        lease = OwnerLease(temp_db)
        lease.acquire()
        assert lease.is_acquired
        lease.release()

    def test_second_acquire_raises_conflict(self, temp_db):
        lease1 = OwnerLease(temp_db)
        lease1.acquire()
        try:
            lease2 = OwnerLease(temp_db)
            with pytest.raises(LeaseConflictError, match="held by"):
                lease2.acquire()
        finally:
            lease1.release()

    def test_takeover_after_expiry(self, temp_db):
        """Crashed owner — expired lease can be taken over."""
        lease1 = OwnerLease(temp_db)
        lease1.acquire()

        # Manually expire the lease
        conn = sqlite3.connect(temp_db)
        expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        conn.execute(
            "UPDATE relay_owner_lease SET lease_expires_at=? WHERE owner_id=?",
            (expired, lease1.owner_id),
        )
        conn.commit()
        conn.close()

        # New owner can acquire
        lease2 = OwnerLease(temp_db)
        lease2.acquire()  # must succeed (takeover)
        assert lease2.is_acquired
        lease2.release()

    def test_takeover_not_possible_while_active(self, temp_db):
        """While lease is active, takeover must fail."""
        lease1 = OwnerLease(temp_db)
        lease1.acquire()

        # Lease is still active — set expiry far in future
        conn = sqlite3.connect(temp_db)
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        conn.execute(
            "UPDATE relay_owner_lease SET lease_expires_at=? WHERE owner_id=?",
            (future, lease1.owner_id),
        )
        conn.commit()
        conn.close()

        lease2 = OwnerLease(temp_db)
        with pytest.raises(LeaseConflictError):
            lease2.acquire()

        lease1.release()


# ═══════════════════════════════════════════════════════════════════════════════
# G10.2 — Heartbeat
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeartbeat:
    """G10: Heartbeat refreshes lease; failure returns False."""

    def test_heartbeat_succeeds_while_acquired(self, temp_db):
        lease = OwnerLease(temp_db)
        lease.acquire()
        assert lease.heartbeat(), "Heartbeat should succeed while lease is active"
        lease.release()

    def test_heartbeat_fails_after_release(self, temp_db):
        lease = OwnerLease(temp_db)
        lease.acquire()
        lease.release()
        assert not lease.heartbeat(), "Heartbeat should fail after release"

    def test_heartbeat_fails_after_takeover(self, temp_db):
        """If another process takes over, heartbeat must fail."""
        lease1 = OwnerLease(temp_db)
        lease1.acquire()

        # Simulate takeover by another process
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "UPDATE relay_owner_lease SET process_id=99999 WHERE owner_id=?",
            (lease1.owner_id,),
        )
        conn.commit()
        conn.close()

        # Original owner's heartbeat must fail (wrong process_id)
        assert not lease1.heartbeat(), (
            "G10: heartbeat must fail after another process takes over"
        )

    def test_heartbeat_extends_lease(self, temp_db):
        lease = OwnerLease(temp_db)
        lease.acquire()

        # Read current expiry
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        row_before = conn.execute(
            "SELECT lease_expires_at FROM relay_owner_lease WHERE owner_id=?",
            (lease.owner_id,),
        ).fetchone()
        conn.close()
        before = datetime.fromisoformat(row_before["lease_expires_at"])

        # Heartbeat should extend
        lease.heartbeat()

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        row_after = conn.execute(
            "SELECT lease_expires_at FROM relay_owner_lease WHERE owner_id=?",
            (lease.owner_id,),
        ).fetchone()
        conn.close()
        after = datetime.fromisoformat(row_after["lease_expires_at"])

        assert after > before, "G10: heartbeat must extend lease expiry"

        lease.release()


# ═══════════════════════════════════════════════════════════════════════════════
# G10.3 — Release
# ═══════════════════════════════════════════════════════════════════════════════

class TestRelease:
    """G10: Release marks lease; re-acquire possible after."""

    def test_release_allows_reacquire(self, temp_db):
        lease1 = OwnerLease(temp_db)
        lease1.acquire()
        lease1.release("test")

        # After release, another owner can acquire
        lease2 = OwnerLease(temp_db)
        lease2.acquire()  # must succeed
        assert lease2.is_acquired
        lease2.release()

    def test_release_sets_reason(self, temp_db):
        lease = OwnerLease(temp_db)
        lease.acquire()
        lease.release("maintenance")

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT release_reason FROM relay_owner_lease WHERE owner_id=?",
            (lease.owner_id,),
        ).fetchone()
        conn.close()
        assert row["release_reason"] == "maintenance"

    def test_double_release_is_safe(self, temp_db):
        lease = OwnerLease(temp_db)
        lease.acquire()
        lease.release()
        lease.release()  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# G10.4 — Relay integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestRelayIntegration:
    """G10: Relay must acquire before start; stop releases."""

    def test_start_without_acquire_raises(self, temp_db):
        registry = SchemaRegistry()
        store = SqliteOutboxStore(temp_db, registry)

        relay = OutboxRelay(store, lambda r: None)
        with pytest.raises(RuntimeError, match="acquire_lease"):
            relay.start()

    def test_start_with_acquire_ok(self, temp_db):
        registry = SchemaRegistry()
        store = SqliteOutboxStore(temp_db, registry)
        lease = OwnerLease(temp_db)
        relay = OutboxRelay(store, lambda r: None, lease=lease)
        relay.acquire_lease()
        relay.start()
        pending = relay.stop(timeout_s=2.0)
        assert pending >= 0  # no events, no error

    def test_lease_released_on_stop(self, temp_db):
        registry = SchemaRegistry()
        store = SqliteOutboxStore(temp_db, registry)
        lease = OwnerLease(temp_db)

        relay = OutboxRelay(store, lambda r: None, lease=lease)
        relay.acquire_lease()
        relay.start()
        relay.stop(timeout_s=2.0)

        # After relay stop, another owner can acquire
        lease2 = OwnerLease(temp_db)
        lease2.acquire()  # must succeed
        assert lease2.is_acquired
        lease2.release()
