"""
G23: Run Repository — explicit state machine with CAS (Compare-And-Swap).

State transitions:
  queued → running
  running → cancel_requested
  active (running|cancel_requested) → terminal (completed|failed|cancelled|blocked|gave_up)

Each transition requires expected aggregate_version.
Losing transition → StaleAggregateVersion (no duplicate fact).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class StaleAggregateVersion(RuntimeError):
    """CAS failed — expected version doesn't match current."""
    def __init__(self, current: int, expected: int) -> None:
        super().__init__(f"Expected version {expected}, current is {current}")
        self.current = current
        self.expected = expected


class RunNotFoundError(RuntimeError):
    """Run does not exist."""


VALID_STATUSES = {
    "queued", "running", "cancel_requested",
    "completed", "failed", "cancelled", "blocked", "gave_up",
}

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked", "gave_up"}
ACTIVE_STATUSES = {"running", "cancel_requested"}


class RunRepository:
    """SQLite run repository with CAS state transitions."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def transition(self, conn: sqlite3.Connection, run_id: str,
                   from_status: str, to_status: str,
                   expected_version: int) -> int:
        """Transition run status with CAS.

        Returns new aggregate_version.
        Raises StaleAggregateVersion if CAS fails.
        """
        if to_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {to_status}")

        # CAS: UPDATE WHERE status = from_status AND aggregate_version = expected
        cur = conn.execute(
            """UPDATE runs SET status=?, aggregate_version=aggregate_version+1,
               updated_at=datetime('now')
               WHERE id=? AND status=? AND aggregate_version=?""",
            (to_status, run_id, from_status, expected_version),
        )
        if cur.rowcount == 0:
            # Check what the actual state is
            row = conn.execute(
                "SELECT status, aggregate_version FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"Run {run_id} not found")
            actual_status, actual_version = row[0], row[1]
            if actual_status != from_status:
                raise StaleAggregateVersion(
                    current=actual_version, expected=expected_version,
                )
            raise StaleAggregateVersion(
                current=actual_version, expected=expected_version,
            )

        new_version = expected_version + 1
        return new_version

    def get_status(self, run_id: str) -> str | None:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def get_version(self, run_id: str) -> int:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT aggregate_version FROM runs WHERE id=?", (run_id,),
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
