"""P15: Trace Projection — idempotent subscriber, receipt in same tx."""

from __future__ import annotations

import json
import sqlite3

from eventing.subscriber import DeliveryReceipt


class TraceProjection:
    """Consumes DomainEvents → session_trace_events. Idempotent by event_id."""
    NAME = "trace_projection"

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def on_event(self, envelope) -> DeliveryReceipt:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT 1 FROM event_projection_receipts WHERE consumer_name=? AND event_id=?",
                (self.NAME, str(envelope.event_id)),
            ).fetchone()
            if row:
                conn.execute("COMMIT")
                return DeliveryReceipt.ok(str(envelope.event_id), self.NAME)

            conn.execute(
                """INSERT INTO event_projection_receipts (consumer_name,event_id,processed_at)
                   VALUES (?,?,datetime('now'))""",
                (self.NAME, str(envelope.event_id)),
            )
            conn.execute(
                """INSERT INTO session_trace_events
                   (session_id,seq,event_type,timestamp,event_json,source)
                   VALUES (?,(SELECT COALESCE(MAX(seq),0)+1 FROM session_trace_events WHERE session_id=?),?,?,?,'outbox_relay')""",
                (str(envelope.scope.session_id) if envelope.scope.session_id else "",
                 str(envelope.scope.session_id) if envelope.scope.session_id else "",
                 str(envelope.event_type), envelope.occurred_at.isoformat(),
                 envelope.canonical_json()),
            )
            conn.execute("COMMIT")
            return DeliveryReceipt.ok(str(envelope.event_id), self.NAME)
        except Exception as exc:
            conn.execute("ROLLBACK")
            return DeliveryReceipt.failed(str(envelope.event_id), self.NAME)
        finally:
            conn.close()
