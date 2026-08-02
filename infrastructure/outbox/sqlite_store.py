"""
P8: SQLite Outbox Adapter — fresh implementation, no old import.

Implements claim/lease/retry/DLQ/receipt on top of the event_outbox table.
DDL only at startup migration.  State + outbox same connection.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

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
    status: str  # pending | claimed | delivered | dead_letter
    attempts: int
    claimed_by: str | None

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


class SqliteOutboxStore:
    """SQLite-backed outbox — implements DurableFactWriter contract."""

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
                occurred_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT,
                claimed_at TEXT,
                delivered_at TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_claim
            ON event_outbox(status, occurred_at)
            WHERE status IN ('pending', 'claimed');
            CREATE TABLE IF NOT EXISTS event_projection_receipts (
                consumer_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (consumer_name, event_id)
            );
        """)

    # ── Write ───────────────────────────────────────────────────────────

    def append(self, conn: sqlite3.Connection, envelope: EventEnvelope) -> None:
        """Append envelope to outbox in caller's transaction.

        Validates payload against schema registry before writing.
        """
        event_type = str(envelope.event_type)
        if not self._registry.has(event_type):
            raise ValueError(f"Event type not registered: {event_type}")
        if not self._registry.validate_payload(event_type, envelope.payload):
            raise ValueError(
                f"Payload type mismatch: expected "
                f"{self._registry.get(event_type).payload_class.__name__}"
            )

        conn.execute(
            """INSERT OR IGNORE INTO event_outbox
               (event_id, event_type, session_id, aggregate_id,
                aggregate_version, payload_json, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(envelope.event_id), event_type,
             str(envelope.scope.session_id) if envelope.scope.session_id else "",
             str(envelope.aggregate_id),
             envelope.aggregate_version.value,
             envelope.canonical_json(),
             envelope.occurred_at.isoformat()),
        )

    # ── Claim ───────────────────────────────────────────────────────────

    def claim_batch(self, worker_id: str, limit: int = 20,
                    lease_s: float = 60.0) -> list[OutboxRecord]:
        now = _utc_now()
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Release expired claims first
            conn.execute(
                """UPDATE event_outbox SET status='pending',
                   claimed_by=NULL, claimed_at=NULL
                   WHERE status='claimed' AND claimed_at < ?""",
                (_utc_now_offset(-lease_s),),
            )
            rows = conn.execute(
                """UPDATE event_outbox SET status='claimed',
                   claimed_by=?, claimed_at=?
                   WHERE event_id IN (
                       SELECT event_id FROM event_outbox
                       WHERE status='pending' ORDER BY occurred_at LIMIT ?
                   ) RETURNING *""",
                (worker_id, now, limit),
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
        with self._connect() as conn:
            c = conn.execute(
                """UPDATE event_outbox SET status='pending',
                   claimed_by=NULL, claimed_at=NULL,
                   attempts=attempts+1, last_error=?
                   WHERE event_id=? AND claimed_by=?""",
                (error[:500], event_id, worker_id),
            )
            return c.rowcount > 0

    def dead_letter(self, event_id: str, worker_id: str, error: str) -> bool:
        with self._connect() as conn:
            c = conn.execute(
                "UPDATE event_outbox SET status='dead_letter', last_error=? "
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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_offset(offset_s: float) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def _row_to_record(row) -> OutboxRecord:
    return OutboxRecord(
        event_id=row["event_id"], event_type=row["event_type"],
        session_id=row["session_id"], aggregate_id=row["aggregate_id"],
        aggregate_version=row["aggregate_version"],
        payload_json=row["payload_json"], occurred_at=row["occurred_at"],
        status=row["status"], attempts=row["attempts"],
        claimed_by=row["claimed_by"],
    )
