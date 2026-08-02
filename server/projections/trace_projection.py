"""
R3.4: Trace Projection — idempotent outbox→trace_events consumer.

Listens to OutboxRelay, maps DomainEvents to session_trace_events rows.
Idempotent by event_id via event_projection_receipts.
"""

from __future__ import annotations

import json
import logging

from server.services.event_outbox import OutboxStore

logger = logging.getLogger(__name__)


class TraceProjection:
    """Consumes DomainEvents and projects them to session_trace_events.

    Idempotent: the same event_id projected twice produces only one trace row.
    """

    CONSUMER_NAME = "trace_projection"

    def __init__(self, db_path: str) -> None:
        self._store = OutboxStore(db_path)

    def project(self, record) -> bool:
        """Project one outbox record → session_trace_events. Idempotent."""
        import sqlite3

        if self._store.record_projection(self.CONSUMER_NAME, record.event_id):
            # Already projected — skip
            return False

        conn = sqlite3.connect(self._store._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Get next sequence
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_trace_events WHERE session_id = ?",
                (record.session_id,),
            ).fetchone()
            sequence = row[0] if row else 1

            payload = _safe_loads(record.payload_json)
            conn.execute(
                """INSERT INTO session_trace_events
                   (session_id, seq, event_type, timestamp, event_json, source, child_session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record.session_id, sequence,
                 record.event_type,
                 record.occurred_at,
                 json.dumps({
                     "type": record.event_type,
                     "event_id": record.event_id,
                     "session_id": record.session_id,
                     "aggregate_id": record.aggregate_id,
                     "payload": payload,
                     "seq": sequence,
                 }, ensure_ascii=False),
                 "outbox_relay",
                 ""),
            )

            conn.execute("COMMIT")
            return True
        except Exception as exc:
            conn.execute("ROLLBACK")
            logger.warning("Trace projection failed for %s: %s", record.event_id[:12], exc)
            return False
        finally:
            conn.close()


def _safe_loads(s: str) -> dict:
    try:
        return json.loads(s)
    except Exception:
        return {}
