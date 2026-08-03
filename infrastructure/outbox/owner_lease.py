"""
G10: Relay Owner Lease — durable DB lease, cross-process safety.

Replaces G0's process-level lock.  Survives process restarts.

Table: relay_owner_lease
  - owner_id TEXT PRIMARY KEY
  - process_id INTEGER
  - acquired_at TEXT (UTC ISO)
  - heartbeat_at TEXT (UTC ISO)
  - lease_expires_at TEXT (UTC ISO)
  - release_reason TEXT

Lifecycle:
  acquire → heartbeat loop → release on shutdown
  Crashed owner: lease expires → new owner can takeover
  Second owner while lease active → LeaseConflictError
"""

from __future__ import annotations

import os
import sqlite3
import time as _time
from datetime import datetime, timezone, timedelta

LEASE_TIMEOUT_S = 30.0       # How long before lease expires without heartbeat
HEARTBEAT_INTERVAL_S = 10.0  # How often to refresh
OWNER_ID = "outbox_relay"    # Single-owner design


class LeaseConflictError(RuntimeError):
    """Another relay owner holds an active lease."""


class LeaseExpiredError(RuntimeError):
    """The lease has expired (heartbeat failed too long)."""


class OwnerLease:
    """Durable owner lease stored in the same SQLite DB as the outbox."""

    def __init__(self, db_path: str, owner_id: str = OWNER_ID) -> None:
        self._db_path = db_path
        self._owner_id = owner_id
        self._process_id = os.getpid()
        self._acquired = False
        self._heartbeat_task: object = None  # reserved for async G10+

    # ── DDL ─────────────────────────────────────────────────────────────

    @staticmethod
    def install(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS relay_owner_lease (
                owner_id TEXT PRIMARY KEY,
                process_id INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                release_reason TEXT
            )
        """)

    # ── Acquire ─────────────────────────────────────────────────────────

    def acquire(self) -> None:
        """Acquire the owner lease.  Raises LeaseConflictError if taken.

        Idempotent — second call is a no-op.
        """
        if self._acquired:
            return

        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=LEASE_TIMEOUT_S)

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_id, process_id, lease_expires_at "
                "FROM relay_owner_lease WHERE owner_id=?",
                (self._owner_id,),
            ).fetchone()

            if row is not None:
                # Check if existing lease is expired
                if hasattr(row, "keys"):
                    existing_pid = row["process_id"]
                    expires_str = row["lease_expires_at"]
                else:
                    existing_pid = row[1]
                    expires_str = row[2]

                expires_dt = datetime.fromisoformat(expires_str)
                if expires_dt > now:
                    raise LeaseConflictError(
                        f"Lease '{self._owner_id}' held by "
                        f"process {existing_pid} until {expires_str} "
                        f"(expires in {(expires_dt - now).total_seconds():.0f}s)"
                    )
                # Lease expired — takeover
                conn.execute(
                    "UPDATE relay_owner_lease SET process_id=?, acquired_at=?, "
                    "heartbeat_at=?, lease_expires_at=?, release_reason=NULL "
                    "WHERE owner_id=?",
                    (self._process_id, now.isoformat(), now.isoformat(),
                     expires.isoformat(), self._owner_id),
                )
            else:
                conn.execute(
                    "INSERT INTO relay_owner_lease "
                    "(owner_id, process_id, acquired_at, heartbeat_at, "
                    "lease_expires_at) VALUES (?, ?, ?, ?, ?)",
                    (self._owner_id, self._process_id, now.isoformat(),
                     now.isoformat(), expires.isoformat()),
                )

            conn.commit()
            self._acquired = True
        finally:
            conn.close()

    # ── Heartbeat ───────────────────────────────────────────────────────

    def heartbeat(self) -> bool:
        """Refresh the lease.  Returns True if successful.

        Call this periodically (every HEARTBEAT_INTERVAL_S seconds).
        If this returns False, stop the relay immediately.
        """
        if not self._acquired:
            return False

        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=LEASE_TIMEOUT_S)

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            c = conn.execute(
                "UPDATE relay_owner_lease SET heartbeat_at=?, "
                "lease_expires_at=? "
                "WHERE owner_id=? AND process_id=?",
                (now.isoformat(), expires.isoformat(),
                 self._owner_id, self._process_id),
            )
            conn.commit()
            if c.rowcount == 0:
                # Another process took over or lease was released
                self._acquired = False
                return False
            return True
        except Exception:
            self._acquired = False
            return False
        finally:
            conn.close()

    # ── Release ─────────────────────────────────────────────────────────

    def release(self, reason: str = "normal_shutdown") -> None:
        """Release the lease (normal shutdown)."""
        if not self._acquired:
            return

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE relay_owner_lease SET release_reason=?, "
                "lease_expires_at=? WHERE owner_id=? AND process_id=?",
                (reason, datetime.now(timezone.utc).isoformat(),
                 self._owner_id, self._process_id),
            )
            conn.commit()
        finally:
            conn.close()
        self._acquired = False

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def process_id(self) -> int:
        return self._process_id
