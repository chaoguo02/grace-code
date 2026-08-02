"""
R3.4: Trace Projection — idempotent outbox→trace_events consumer.

P0-2 fixed: receipt and trace insert are in the SAME transaction.
Projection failure throws to Relay — the relay handles retry/dead-letter.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from server.services.event_outbox import OutboxStore
from server.ws.event_mapper import map_domain_to_ws

logger = logging.getLogger(__name__)


class TraceProjection:
    """Consumes DomainEvents and projects them to session_trace_events.

    Idempotent: the same event_id projected twice produces only one trace row.
    Receipt + trace INSERT in the same transaction — no gap.
    """

    CONSUMER_NAME = "trace_projection"

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._outbox = OutboxStore(db_path)
        self._outbox.install()

    def project(self, record) -> bool:
        """Project one outbox record → session_trace_events.

        Returns True on success.  Throws on failure — Relay handles retry.

        P0-2: receipt insert happens INSIDE the same transaction as the
        trace insert.  If either fails, both roll back.
        """
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Check if already projected (idempotency gate)
            if not self._outbox.try_record_projection(
                conn, self.CONSUMER_NAME, record.event_id,
            ):
                conn.execute("COMMIT")
                return False  # Already done

            # Get next sequence
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_trace_events WHERE session_id = ?",
                (record.session_id,),
            ).fetchone()
            sequence = row[0] if row else 1

            payload = _safe_loads(record.payload_json)
            projected = map_domain_to_ws(record) or {
                "type": record.event_type,
                "event_id": record.event_id,
                "session_id": record.session_id,
                "aggregate_id": record.aggregate_id,
                "payload": payload,
                "timestamp": record.occurred_at,
            }
            stored = {**projected, "seq": sequence, "sequence": sequence}

            # Insert trace
            conn.execute(
                """INSERT INTO session_trace_events
                   (session_id, seq, event_type, timestamp, event_json, source, child_session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record.session_id, sequence,
                 str(projected.get("type") or record.event_type), record.occurred_at,
                 json.dumps(stored, ensure_ascii=False),
                 "outbox_relay", ""),
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
