"""
R3: Transactional Event Outbox — guarantees at-least-once DomainEvent delivery.

Pattern:
  Business state change + outbox INSERT in same SQLite transaction.
  OutboxRelay polls the outbox table, delivers via EventBus, marks delivered.
  Idempotent by event_id — replaying the same outbox entry is safe.

Decoupled from: DomainEvent definitions, EventBus transport, business logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── OutboxRecord ────────────────────────────────────────────────────────────

@dataclass
class OutboxRecord:
    event_id: str
    event_type: str            # DomainEvent class name
    payload_json: str          # serialized DomainEvent.to_dict()
    session_id: str
    status: str = "pending"    # pending | delivered | dead_letter
    retry_count: int = 0
    created_at: str = ""
    delivered_at: str | None = None

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


# ── OutboxStore ─────────────────────────────────────────────────────────────

class OutboxStore:
    """SQLite-backed outbox — insert in same transaction as business state."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def ensure_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_outbox (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                delivered_at TEXT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON event_outbox(status, created_at)
            WHERE status = 'pending'
        """)

    def insert(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        event_type: str,
        payload_json: str,
        session_id: str,
    ) -> None:
        """Insert into outbox — caller owns the transaction."""
        self.ensure_table(conn)
        conn.execute(
            """INSERT OR IGNORE INTO event_outbox
               (event_id, event_type, payload_json, session_id, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (event_id, payload_json, event_type, session_id, _utc_now()),
        )

    def fetch_pending(self, limit: int = 20) -> list[OutboxRecord]:
        """Fetch pending records for delivery."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM event_outbox
                   WHERE status = 'pending'
                   ORDER BY created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [OutboxRecord(
            event_id=r["event_id"],
            event_type=r["event_type"],
            payload_json=r["payload_json"],
            session_id=r["session_id"],
            status=r["status"],
            retry_count=r["retry_count"],
            created_at=r["created_at"],
            delivered_at=r["delivered_at"],
        ) for r in rows]

    def mark_delivered(self, event_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE event_outbox SET status='delivered', delivered_at=?
                   WHERE event_id=?""",
                (_utc_now(), event_id),
            )

    def mark_dead_letter(self, event_id: str, retry_count: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE event_outbox SET status='dead_letter', retry_count=?
                   WHERE event_id=?""",
                (retry_count, event_id),
            )

    def increment_retry(self, event_id: str) -> int:
        with self._connect() as conn:
            conn.execute(
                "UPDATE event_outbox SET retry_count = retry_count + 1 WHERE event_id = ?",
                (event_id,),
            )
            row = conn.execute(
                "SELECT retry_count FROM event_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return row["retry_count"] if row else 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


# ── OutboxRelay ─────────────────────────────────────────────────────────────

class OutboxRelay:
    """Polls outbox, delivers via EventBus, handles retry/backoff/dead-letter.

    Delivery is idempotent by event_id — replaying an already-delivered
    outbox entry is safe (the INSERT uses OR IGNORE).
    """

    MAX_RETRIES = 5
    BACKOFF_BASE_S = 0.5
    BACKOFF_MAX_S = 30.0
    POLL_INTERVAL_S = 1.0
    DEAD_LETTER_THRESHOLD = 5

    def __init__(
        self,
        store: OutboxStore,
        publish: object,  # callable(event_dict, session_id)
        *,
        poll_interval: float | None = None,
    ) -> None:
        self._store = store
        self._publish = publish
        self._poll_interval = poll_interval or self.POLL_INTERVAL_S
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.ensure_future(self._poll_loop())

    async def stop(self, drain_timeout_s: float = 10.0) -> int:
        """Stop polling and drain remaining pending events. Returns count remaining."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final drain
        delivered = 0
        deadline = time.monotonic() + drain_timeout_s
        while time.monotonic() < deadline:
            batch = self._store.fetch_pending(limit=50)
            if not batch:
                break
            for record in batch:
                self._deliver_one(record)
                delivered += 1
        remaining = len(self._store.fetch_pending(limit=1))
        return remaining

    async def _poll_loop(self) -> None:
        while self._running:
            batch = self._store.fetch_pending(limit=20)
            for record in batch:
                self._deliver_one(record)
            await asyncio.sleep(self._poll_interval)

    def _deliver_one(self, record: OutboxRecord) -> None:
        try:
            self._publish(record.payload, record.session_id)
            self._store.mark_delivered(record.event_id)
        except Exception as exc:
            retry_count = self._store.increment_retry(record.event_id)
            logger.warning(
                "Outbox delivery failed for %s (retry %d/%d): %s",
                record.event_id[:12], retry_count, self.MAX_RETRIES, exc,
            )
            if retry_count >= self.DEAD_LETTER_THRESHOLD:
                self._store.mark_dead_letter(record.event_id, retry_count)
                logger.error(
                    "Outbox event %s moved to dead_letter after %d retries",
                    record.event_id[:12], retry_count,
                )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
