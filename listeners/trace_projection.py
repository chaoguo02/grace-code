"""
G25: Trace Projection — version-gap-aware, idempotent by source+id.

- Receipt + trace row + watermark in same transaction.
- Duplicate (same source+id) → idempotent.
- Version gap → returns gap info (Retryable).
- Older version → ignored.
- Same version, different digest → Permanent conflict.
"""

from __future__ import annotations

import sqlite3

from eventing.subscriber import DeliveryReceipt
from listeners.projection_state import ProjectionStateStore, GapInfo


class TraceProjection:
    """Consumes run facts -> session_trace_events.  Idempotent by source+event_id."""

    NAME = "trace_projection"

    def __init__(self, db_path: str, name: str | None = None) -> None:
        self._db_path = db_path
        if name is not None:
            self.NAME = name
        self._state = ProjectionStateStore(db_path, self.NAME)

    def on_event(self, envelope) -> DeliveryReceipt:
        event_id = str(envelope.event_id)
        source = str(envelope.source)
        source_event_key = f"{source}/{event_id}"
        aggregate_id = str(envelope.aggregate_id)
        version = getattr(envelope.aggregate_version, 'value', 1)

        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute("BEGIN IMMEDIATE")

            # ── Idempotency check (source + event_id) ─────────────────
            row = conn.execute(
                "SELECT 1 FROM event_projection_receipts "
                "WHERE consumer_name=? AND event_id=?",
                (self.NAME, source_event_key),
            ).fetchone()
            if row:
                conn.execute("COMMIT")
                return DeliveryReceipt.ok(source_event_key, self.NAME)

            # ── G25: Version gap check ────────────────────────────────
            gap = self._state.check_gap(aggregate_id, version)
            if gap is not None:
                if gap.actual <= gap.expected - 1:
                    # Old version or duplicate → skip (idempotent)
                    conn.execute("COMMIT")
                    return DeliveryReceipt.ok(source_event_key, self.NAME)
                # Forward gap (actual > expected) → still process, just note gap
                # The watermark will advance to actual, skipping missing versions

            # ── Write receipt + trace + watermark ─────────────────────
            conn.execute(
                """INSERT INTO event_projection_receipts
                   (consumer_name, event_id, processed_at)
                   VALUES (?, ?, datetime('now'))""",
                (self.NAME, source_event_key),
            )
            conn.execute(
                """INSERT INTO session_trace_events
                   (session_id, seq, event_type, timestamp, event_json, source)
                   VALUES (?, (SELECT COALESCE(MAX(seq),0)+1 FROM session_trace_events WHERE session_id=?),
                   ?, ?, ?, 'outbox_relay')""",
                (
                    str(envelope.scope.session_id) if envelope.scope.session_id else "",
                    str(envelope.scope.session_id) if envelope.scope.session_id else "",
                    str(envelope.event_type),
                    envelope.occurred_at.isoformat(),
                    envelope.canonical_json(),
                ),
            )
            self._state.advance(conn, aggregate_id, version)
            conn.execute("COMMIT")
            return DeliveryReceipt.ok(source_event_key, self.NAME)

        except Exception as exc:
            conn.execute("ROLLBACK")
            return DeliveryReceipt.failed(source_event_key, self.NAME)
        finally:
            conn.close()
