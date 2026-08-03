"""
G9: SQLite Outbox — identity conflict detection, aggregate ordering, true backoff.

- INSERT OR IGNORE replaced: same digest → idempotent, different digest → conflict.
- claim_batch respects aggregate ordering (v1 before v2 for same aggregate).
- reschedule with available_at for exponential backoff + jitter.
- count_pending for exact shutdown check (not claim-based).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from application.events.envelope import EventEnvelope
from application.events.schema_registry import SchemaRegistry


@dataclass
class OutboxRecord:
    event_id: str
    event_type: str
    session_id: str
    aggregate_id: str
    aggregate_version: int
    payload_json: str
    occurred_at: str
    status: str
    attempts: int
    claimed_by: str | None
    available_at: str | None = None

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


class SqliteOutboxStore:
    """SQLite-backed outbox — identity-safe, aggregate-ordered."""

    def __init__(self, db_path: str, registry: SchemaRegistry) -> None:
        self._db_path = db_path
        self._registry = registry

    # ── DDL ─────────────────────────────────────────────────────────────

    @staticmethod
    def install(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS event_outbox (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                aggregate_version INTEGER NOT NULL DEFAULT 1,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT,
                claimed_at TEXT,
                available_at TEXT,
                delivered_at TEXT,
                last_error TEXT
            );
            -- G9 migration: add columns if upgrading from old schema
            -- (wrapped in try/catch via separate calls — see install_with_migration)
            CREATE INDEX IF NOT EXISTS idx_outbox_claim
            ON event_outbox(status, available_at, occurred_at)
            WHERE status IN ('pending');
            CREATE INDEX IF NOT EXISTS idx_outbox_aggregate
            ON event_outbox(aggregate_id, aggregate_version);
            CREATE TABLE IF NOT EXISTS event_projection_receipts (
                consumer_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (consumer_name, event_id)
            );
        """)

    # ── Write ───────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_event_type(raw: str) -> str:
        """Strip version suffix for backward compat with old outbox schema.

        "run.submitted.v1" → "run.submitted"
        "run.completed.v1" → "run.completed"
        """
        if raw.endswith((".v1", ".v2", ".v3")):
            return raw.rsplit(".", 1)[0]
        return raw

    def append(self, conn: sqlite3.Connection, envelope: EventEnvelope) -> None:
        """Append envelope to outbox in caller's transaction.

        G9: Checks identity conflict (same source+id, different digest).
        Same digest → idempotent (skip).  Different digest → conflict error.
        """
        event_type = str(envelope.event_type)
        if not self._registry.has(event_type):
            raise ValueError(f"Event type not registered: {event_type}")
        if not self._registry.validate_payload(event_type, envelope.payload):
            raise ValueError(
                f"Payload type mismatch: expected "
                f"{self._registry.get(event_type).payload_class.__name__}"
            )

        # Normalize to unversioned type for backward compat with old consumers
        stored_event_type = self._normalize_event_type(event_type)

        payload_json = envelope.canonical_json()
        payload_digest = hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest()

        event_id = str(envelope.event_id)

        # Check for existing event with same ID
        existing = conn.execute(
            "SELECT event_id, payload_digest FROM event_outbox WHERE event_id=?",
            (event_id,),
        ).fetchone()

        if existing:
            # Handle both sqlite3.Row and plain tuple
            if hasattr(existing, "keys"):
                existing_digest = existing["payload_digest"]
            else:
                existing_digest = existing[1]  # second column
            if existing_digest == payload_digest:
                # Idempotent — same event already written
                return
            else:
                # Conflict — same event_id but different content
                raise ValueError(
                    f"EventIdentityConflict: event_id={event_id} "
                    f"already exists with different digest. "
                    f"existing_digest={existing_digest[:16]}... "
                    f"incoming_digest={payload_digest[:16]}..."
                )

        conn.execute(
            """INSERT INTO event_outbox
               (event_id, event_type, session_id, aggregate_id,
                aggregate_version, payload_json, payload_digest,
                occurred_at, available_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, stored_event_type,
             str(envelope.scope.session_id) if envelope.scope.session_id else "",
             str(envelope.aggregate_id),
             envelope.aggregate_version.value,
             payload_json,
             payload_digest,
             envelope.occurred_at.isoformat(),
             envelope.occurred_at.isoformat()),  # available_at = occurred_at
        )

    # ── Claim (aggregate-ordered) ───────────────────────────────────────

    def claim_batch(self, worker_id: str, limit: int = 20,
                    lease_s: float = 60.0) -> list[OutboxRecord]:
        """Claim up to *limit* pending events, respecting aggregate ordering.

        G9: does NOT claim event at version N+1 if version N of the same
        aggregate is still pending.
        """
        now = _utc_now()
        now_dt = datetime.now(timezone.utc)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Release expired claims
            expiry = (now_dt - timedelta(seconds=lease_s)).isoformat()
            conn.execute(
                """UPDATE event_outbox SET status='pending',
                   claimed_by=NULL, claimed_at=NULL
                   WHERE status='claimed' AND claimed_at < ?""",
                (expiry,),
            )

            # Claim only events whose previous version (same aggregate) is delivered
            # AND which are available now (available_at <= now)
            rows = conn.execute(
                """UPDATE event_outbox SET status='claimed',
                   claimed_by=?, claimed_at=?
                   WHERE event_id IN (
                       SELECT e.event_id FROM event_outbox e
                       WHERE e.status = 'pending'
                         AND (e.available_at IS NULL OR e.available_at <= ?)
                         AND NOT EXISTS (
                           SELECT 1 FROM event_outbox prev
                           WHERE prev.aggregate_id = e.aggregate_id
                             AND prev.aggregate_version < e.aggregate_version
                             AND prev.status != 'delivered'
                         )
                       ORDER BY e.occurred_at
                       LIMIT ?
                   ) RETURNING *""",
                (worker_id, now, now, limit),
            ).fetchall()
            conn.commit()
            return [_row_to_record(r) for r in rows]
        finally:
            conn.close()

    def mark_delivered(self, event_id: str, worker_id: str) -> bool:
        with self._connect() as conn:
            c = conn.execute(
                "UPDATE event_outbox SET status='delivered', delivered_at=? "
                "WHERE event_id=? AND claimed_by=?",
                (_utc_now(), event_id, worker_id),
            )
            return c.rowcount > 0

    def reschedule(self, event_id: str, worker_id: str, error: str,
                   delay_s: float = 1.0) -> bool:
        """Reschedule with persisted available_at for true backoff.

        G9: available_at = now + delay_s.  The claim query skips events
        whose available_at is in the future.
        """
        available_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_s)
        ).isoformat()
        with self._connect() as conn:
            c = conn.execute(
                """UPDATE event_outbox SET status='pending',
                   claimed_by=NULL, claimed_at=NULL,
                   attempts=attempts+1, last_error=?,
                   available_at=?
                   WHERE event_id=? AND claimed_by=?""",
                (error[:500], available_at, event_id, worker_id),
            )
            return c.rowcount > 0

    def dead_letter(self, event_id: str, worker_id: str, error: str) -> bool:
        with self._connect() as conn:
            c = conn.execute(
                "UPDATE event_outbox SET status='dead_letter', "
                "attempts=attempts+1, last_error=?, "
                "available_at=NULL "
                "WHERE event_id=? AND claimed_by=?",
                (error[:500], event_id, worker_id),
            )
            return c.rowcount > 0

    def record_receipt(self, consumer_name: str, event_id: str) -> bool:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO event_projection_receipts
                   (consumer_name, event_id, processed_at) VALUES (?, ?, ?)""",
                (consumer_name, event_id, _utc_now()),
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    # ── Count ───────────────────────────────────────────────────────────

    def count_pending(self) -> int:
        """Exact count of pending events (not claimed).  For shutdown check."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM event_outbox WHERE status='pending'"
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    @staticmethod
    def migrate_add_columns(conn: sqlite3.Connection) -> None:
        """Add available_at and payload_digest columns to existing tables.

        Idempotent — skips if columns already exist.
        Called at startup after install().
        """
        existing_cols = {row[1] for row in conn.execute(
            "PRAGMA table_info('event_outbox')"
        ).fetchall()}
        if 'available_at' not in existing_cols:
            conn.execute("ALTER TABLE event_outbox ADD COLUMN available_at TEXT")
        if 'payload_digest' not in existing_cols:
            conn.execute(
                "ALTER TABLE event_outbox ADD COLUMN payload_digest TEXT NOT NULL DEFAULT ''"
            )

    def count_by_status(self, status: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM event_outbox WHERE status=?",
                (status,),
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


# ── Helpers ─────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row) -> OutboxRecord:
    return OutboxRecord(
        event_id=row["event_id"], event_type=row["event_type"],
        session_id=row["session_id"], aggregate_id=row["aggregate_id"],
        aggregate_version=row["aggregate_version"],
        payload_json=row["payload_json"], occurred_at=row["occurred_at"],
        status=row["status"], attempts=row["attempts"],
        claimed_by=row["claimed_by"],
        available_at=row["available_at"] if "available_at" in row.keys() else None,
    )
