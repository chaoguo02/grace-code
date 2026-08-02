"""
G26: Audit Projection — immutable audit log of all events.

Stores: source/id/correlation/causation/scope/version/digest.
Idempotent by event_id.  Does NOT import publisher/command/coordinator.
"""

from __future__ import annotations

import hashlib
import sqlite3

from eventing.subscriber import DeliveryReceipt


class AuditProjection:
    """Consumes all durable facts → immutable audit_log."""

    NAME = "audit_projection"

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    @staticmethod
    def install(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                event_id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                correlation_id TEXT NOT NULL DEFAULT '',
                causation_id TEXT,
                scope_kind TEXT NOT NULL DEFAULT '',
                scope_session_id TEXT,
                scope_task_id TEXT,
                scope_generation INTEGER DEFAULT 0,
                aggregate_id TEXT NOT NULL DEFAULT '',
                aggregate_version INTEGER NOT NULL DEFAULT 1,
                payload_digest TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

    def on_event(self, envelope) -> DeliveryReceipt:
        event_id = str(envelope.event_id)

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Idempotent by event_id
            row = conn.execute(
                "SELECT 1 FROM audit_log WHERE event_id=?", (event_id,),
            ).fetchone()
            if row:
                conn.execute("COMMIT")
                return DeliveryReceipt.ok(event_id, self.NAME)

            # Compute payload digest
            payload_json = envelope.canonical_json()
            digest = hashlib.sha256(payload_json.encode()).hexdigest()

            scope = envelope.scope
            conn.execute(
                """INSERT INTO audit_log
                   (event_id, source, event_type, correlation_id, causation_id,
                    scope_kind, scope_session_id, scope_task_id, scope_generation,
                    aggregate_id, aggregate_version, payload_digest, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    str(envelope.source),
                    str(envelope.event_type),
                    str(envelope.correlation_id),
                    str(envelope.causation_id) if envelope.causation_id else None,
                    scope.kind.value,
                    str(scope.session_id) if scope.session_id else None,
                    str(scope.task_id) if scope.task_id else None,
                    scope.generation,
                    str(envelope.aggregate_id),
                    envelope.aggregate_version.value,
                    digest,
                    envelope.occurred_at.isoformat(),
                ),
            )
            conn.execute("COMMIT")
            return DeliveryReceipt.ok(event_id, self.NAME)
        except Exception:
            conn.execute("ROLLBACK")
            return DeliveryReceipt.failed(event_id, self.NAME)
        finally:
            conn.close()
