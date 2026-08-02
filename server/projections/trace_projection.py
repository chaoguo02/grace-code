"""
R3.4: Trace Projection — idempotent outbox→trace_events consumer.

P0-2 fixed: receipt and trace insert are in the SAME transaction.
Projection failure throws to Relay — the relay handles retry/dead-letter.
"""

from __future__ import annotations

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)


class TraceProjection:
    """Consumes DomainEvents and projects them to session_trace_events.

    Idempotent: the same event_id projected twice produces only one trace row.
    Receipt + trace INSERT in the same transaction — no gap.
    """

    CONSUMER_NAME = "trace_projection"

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def project(self, record) -> bool:
        """Project one outbox record → session_trace_events.

        Returns True on success.  Throws on failure — Relay handles retry.

        P0-2: receipt insert happens INSIDE the same transaction as the
        trace insert.  If either fails, both roll back.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Check if already projected (idempotency gate)
            existing = conn.execute(
                "SELECT 1 FROM event_projection_receipts WHERE consumer_name=? AND event_id=?",
                (self.CONSUMER_NAME, record.event_id),
            ).fetchone()
            if existing:
                conn.execute("ROLLBACK")
                return False  # Already done

            # Get next sequence
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_trace_events WHERE session_id = ?",
                (record.session_id,),
            ).fetchone()
            sequence = row[0] if row else 1

            payload = _safe_loads(record.payload_json)

            # Insert trace
            conn.execute(
                """INSERT INTO session_trace_events
                   (session_id, seq, event_type, timestamp, event_json, source, child_session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record.session_id, sequence,
                 record.event_type, record.occurred_at,
                 json.dumps({
                     "type": record.event_type,
                     "event_id": record.event_id,
                     "session_id": record.session_id,
                     "aggregate_id": record.aggregate_id,
                     "payload": payload,
                     "seq": sequence,
                 }, ensure_ascii=False),
                 "outbox_relay", ""),
            )

            # P0-2: Receipt in SAME transaction
            conn.execute(
                """INSERT INTO event_projection_receipts
                   (consumer_name, event_id, processed_at)
                   VALUES (?, ?, ?)""",
                (self.CONSUMER_NAME, record.event_id, _utc_now()),
            )

            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise  # P0-2: throw to Relay — don't swallow
        finally:
            conn.close()


def _safe_loads(s: str) -> dict:
    try:
        return json.loads(s)
    except Exception:
        return {}


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
