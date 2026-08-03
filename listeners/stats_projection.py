"""
G26: Stats Projection — explicit visitors, persistent model.

Each supported event type has an explicit handler.
Metrics are persisted, not just in-process list.
Every event is explicitly named in SUPPORTED_TYPES.
"""

from __future__ import annotations

import sqlite3

from eventing.subscriber import DeliveryReceipt


class StatsProjection:
    """Consumes run facts → persistent run_metrics.  Explicit event types."""

    NAME = "stats_projection"
    SUPPORTED_TYPES: frozenset[str] = frozenset({
        "run.submitted.v1", "run.started.v1", "run.completed.v1",
        "run.failed.v1", "run.cancelled.v1", "run.blocked.v1",
        "run.gave_up.v1",
    })

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._metrics: list[dict] = []  # in-process for tests without DB

    @staticmethod
    def install(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS run_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                aggregate_id TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

    def on_event(self, envelope) -> DeliveryReceipt:
        et = str(envelope.event_type)

        # G26: Explicit match — no catch-all
        if et not in self.SUPPORTED_TYPES:
            return DeliveryReceipt.ok(str(envelope.event_id), self.NAME)

        record = {
            "event_type": et,
            "session_id": str(envelope.scope.session_id) if envelope.scope.session_id else "",
            "aggregate_id": str(envelope.aggregate_id),
            "occurred_at": envelope.occurred_at.isoformat(),
            "event_id": str(envelope.event_id),
        }

        if self._db_path is not None:
            self._persist(record)

        self._metrics.append(record)
        return DeliveryReceipt.ok(str(envelope.event_id), self.NAME)

    def _persist(self, record: dict) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO run_metrics
                   (event_type, session_id, aggregate_id, occurred_at, event_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (record["event_type"], record["session_id"],
                 record["aggregate_id"], record["occurred_at"],
                 record["event_id"]),
            )
            conn.commit()
        finally:
            conn.close()

    @property
    def metrics(self) -> list[dict]:
        return list(self._metrics)
