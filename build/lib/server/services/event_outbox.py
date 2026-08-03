"""
R3.1: Transactional Event Outbox — at-least-once DomainEvent delivery.

Schema: event_outbox + event_projection_receipts tables.
Repository: claim_batch, mark_delivered, reschedule, dead_letter, release_expired.
Claim uses short-transaction lease; no locks held during WS delivery.

Decoupled from: DomainEvent definitions, EventBus transport, business logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── OutboxRecord ────────────────────────────────────────────────────────────

@dataclass
class OutboxRecord:
    event_id: str
    event_type: str
    event_version: int
    session_id: str
    aggregate_id: str
    aggregate_version: int
    payload_json: str
    occurred_at: str
    available_at: str
    status: str                     # pending | claimed | delivered | dead_letter
    attempts: int = 0
    claimed_by: str | None = None
    claimed_at: str | None = None
    delivered_at: str | None = None
    last_error: str | None = None

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


# ── OutboxStore ─────────────────────────────────────────────────────────────

class OutboxStore:
    """SQLite-backed outbox — insert in same transaction as business state."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def install(self) -> None:
        """Install schema outside any business transaction."""
        with self._connect() as conn:
            self.ensure_tables(conn)

    # ── schema ──────────────────────────────────────────────────────────

    @staticmethod
    def ensure_tables(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS event_outbox (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                event_version INTEGER NOT NULL DEFAULT 1,
                session_id TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                aggregate_version INTEGER NOT NULL DEFAULT 1,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT,
                claimed_at TEXT,
                delivered_at TEXT,
                last_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_event_outbox_delivery
            ON event_outbox(status, available_at, occurred_at)
            WHERE status IN ('pending', 'claimed');

            CREATE TABLE IF NOT EXISTS event_projection_receipts (
                consumer_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (consumer_name, event_id)
            );
        """)

    # ── write ───────────────────────────────────────────────────────────

    def append(self, conn: sqlite3.Connection, event: object) -> None:
        """Insert a DomainEvent into the outbox in the caller's transaction.

        *event* must be a DomainEvent with to_dict().
        """
        d = event.to_dict()
        self._insert_idempotent(conn, (
            d["event_id"], d["event_type"], d.get("event_version", 1),
            d["session_id"], d["aggregate_id"],
            d.get("aggregate_version", 1),
            _normalize_json(d.get("payload", {})),
            d.get("occurred_at", _utc_now()), _utc_now(),
        ))

    def _insert_idempotent(
        self, conn: sqlite3.Connection, values: tuple,
    ) -> None:
        try:
            conn.execute(
                """INSERT INTO event_outbox
               (event_id, event_type, event_version, session_id,
                aggregate_id, aggregate_version, payload_json,
                occurred_at, available_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
        except sqlite3.IntegrityError as exc:
            existing = conn.execute(
                """SELECT event_type, event_version, session_id, aggregate_id,
                          aggregate_version, payload_json
                   FROM event_outbox WHERE event_id=?""",
                (values[0],),
            ).fetchone()
            if existing is not None and tuple(existing) == tuple(values[1:7]):
                return
            raise ValueError(
                f"event_id {values[0]!r} reused with different event data"
            ) from exc

    def append_event(self, conn: sqlite3.Connection, event_id: str,
                     event_type: str, session_id: str, aggregate_id: str,
                     aggregate_version: int, payload_json: str) -> None:
        """Low-level append for callers without DomainEvent objects."""
        now = _utc_now()
        self._insert_idempotent(conn, (
            event_id, event_type, 1, session_id, aggregate_id,
            aggregate_version, _normalize_json(payload_json), now, now,
        ))

    # ── claim / deliver ─────────────────────────────────────────────────

    def claim_batch(self, worker_id: str, limit: int = 20,
                    lease_s: float = 60.0) -> list[OutboxRecord]:
        """Claim up to *limit* pending events for *worker_id*."""
        now = _utc_now()
        lease_expiry = _utc_now_offset(-lease_s)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Release expired claims first
            conn.execute(
                """UPDATE event_outbox SET status='pending',
                   claimed_by=NULL, claimed_at=NULL
                   WHERE status='claimed'
                   AND claimed_at IS NOT NULL
                   AND claimed_at < ?""",
                (lease_expiry,),
            )
            # Claim batch
            rows = conn.execute(
                """UPDATE event_outbox
                   SET status='claimed', claimed_by=?, claimed_at=?
                   WHERE event_id IN (
                       SELECT event_id FROM event_outbox
                       WHERE status='pending'
                       AND available_at <= ?
                       ORDER BY occurred_at
                       LIMIT ?
                   )
                   RETURNING *""",
                (worker_id, now, now, limit),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def mark_delivered(self, event_id: str, worker_id: str) -> bool:
        """Mark event as delivered. Returns True if successful."""
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE event_outbox SET status='delivered', delivered_at=?
                   WHERE event_id=? AND claimed_by=?""",
                (_utc_now(), event_id, worker_id),
            )
            return cursor.rowcount > 0

    def reschedule(self, event_id: str, worker_id: str, error: str,
                   available_at: str | None = None) -> bool:
        """Reschedule after failed delivery attempt. Returns True if updated."""
        at = available_at or _utc_now_offset(1.0)
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE event_outbox
                   SET status='pending', claimed_by=NULL, claimed_at=NULL,
                   attempts=attempts+1, last_error=?, available_at=?
                   WHERE event_id=? AND claimed_by=?""",
                (error, at, event_id, worker_id),
            )
            return cursor.rowcount > 0

    def dead_letter(self, event_id: str, worker_id: str, error: str) -> bool:
        """Move event to dead-letter queue. Returns True if updated."""
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE event_outbox
                   SET status='dead_letter', last_error=?
                   WHERE event_id=? AND claimed_by=?""",
                (error, event_id, worker_id),
            )
            return cursor.rowcount > 0

    def release_expired_claims(
        self, now: str | None = None, *, lease_s: float = 60.0,
    ) -> int:
        """Release claims older than *lease_s*. Returns count."""
        if now is None:
            expiry = _utc_now_offset(-lease_s)
        else:
            from datetime import datetime, timedelta
            expiry = (
                datetime.fromisoformat(now) - timedelta(seconds=lease_s)
            ).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE event_outbox SET status='pending',
                   claimed_by=NULL, claimed_at=NULL
                   WHERE status='claimed'
                   AND claimed_at IS NOT NULL
                   AND claimed_at < ?""",
                (expiry,),
            )
            return cursor.rowcount

    # ── projection receipts ─────────────────────────────────────────────

    def record_projection(self, consumer_name: str, event_id: str) -> bool:
        """Idempotently record that *consumer_name* processed *event_id*."""
        conn = self._connect()
        try:
            self.ensure_tables(conn)
            conn.execute(
                """INSERT OR IGNORE INTO event_projection_receipts
                   (consumer_name, event_id, processed_at)
                   VALUES (?, ?, ?)""",
                (consumer_name, event_id, _utc_now()),
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    @staticmethod
    def try_record_projection(
        conn: sqlite3.Connection, consumer_name: str, event_id: str,
    ) -> bool:
        """Insert a receipt in the caller's projection transaction."""
        cursor = conn.execute(
            """INSERT OR IGNORE INTO event_projection_receipts
               (consumer_name, event_id, processed_at)
               VALUES (?, ?, ?)""",
            (consumer_name, event_id, _utc_now()),
        )
        return cursor.rowcount == 1

    def count_undelivered(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM event_outbox
                   WHERE status IN ('pending', 'claimed')"""
            ).fetchone()
            return int(row[0]) if row else 0

    # ── internal ────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn


# ── OutboxRelay ─────────────────────────────────────────────────────────────

class OutboxRelay:
    """Polls outbox, delivers via callback, handles retry/backoff/dead-letter."""

    MAX_ATTEMPTS = 5
    POLL_INTERVAL_S = 1.0

    def __init__(self, store: OutboxStore,
                 deliver: object,
                 worker_id: str | None = None) -> None:
        self._store = store
        self._deliver = deliver  # callable(event_dict) -> None
        self._worker_id = worker_id or str(uuid.uuid4())[:8]
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._poll_loop())

    async def stop(self, drain_timeout_s: float = 10.0) -> int:
        self._running = False
        if self._task is not None:
            try:
                # Let an in-flight projection finish and acknowledge its
                # claim. Cancelling asyncio.to_thread does not stop the worker
                # thread and can otherwise create a false shutdown gap.
                await asyncio.wait_for(
                    asyncio.shield(self._task),
                    timeout=min(drain_timeout_s, self.POLL_INTERVAL_S + 5.0),
                )
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
        # Final drain of claimed events
        deadline = time.monotonic() + drain_timeout_s
        while time.monotonic() < deadline:
            batch = await asyncio.to_thread(
                self._store.claim_batch, self._worker_id, 50, 5.0,
            )
            if not batch:
                break
            for record in batch:
                await asyncio.to_thread(self._deliver_one, record)
        return await asyncio.to_thread(self._store.count_undelivered)

    async def _poll_loop(self) -> None:
        while self._running:
            batch = await asyncio.to_thread(
                self._store.claim_batch, self._worker_id, 20, 60.0,
            )
            for record in batch:
                await asyncio.to_thread(self._deliver_one, record)
            await asyncio.sleep(self.POLL_INTERVAL_S)

    def _deliver_one(self, record: OutboxRecord) -> None:
        try:
            self._deliver(record)
            if not self._store.mark_delivered(record.event_id, self._worker_id):
                raise RuntimeError("outbox claim was lost before acknowledgement")
        except Exception as exc:
            new_attempts = record.attempts + 1
            if new_attempts >= self.MAX_ATTEMPTS:
                self._store.dead_letter(record.event_id, self._worker_id,
                                        str(exc)[:500])
                logger.error("Outbox %s dead-letter after %d attempts",
                             record.event_id[:12], new_attempts)
            else:
                backoff = min(2 ** (new_attempts - 1), 30)
                self._store.reschedule(
                    record.event_id, self._worker_id, str(exc)[:500],
                    available_at=_utc_now_offset(backoff),
                )


# ── helpers ─────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _utc_now_offset(offset_s: float) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()

def _normalize_json(value: object) -> str:
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )

def _row_to_record(row) -> OutboxRecord:
    return OutboxRecord(
        event_id=row["event_id"], event_type=row["event_type"],
        session_id=row["session_id"], aggregate_id=row["aggregate_id"],
        event_version=row["event_version"], aggregate_version=row["aggregate_version"],
        payload_json=row["payload_json"], occurred_at=row["occurred_at"],
        available_at=row["available_at"], status=row["status"],
        attempts=row["attempts"], claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"], delivered_at=row["delivered_at"],
        last_error=row["last_error"],
    )
