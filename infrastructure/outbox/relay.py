"""
G10: Outbox Relay — OwnerLease, heartbeat, safe takeover.

G9 features retained:
  - DeliveryOutcome routing (Delivered→ACK, Retryable→reschedule, Permanent→DLQ)
  - Exponential backoff with jitter
  - Aggregate ordering via store.claim_batch()

G10 additions:
  - Durable DB lease (OwnerLease) — survives process restarts
  - Heartbeat in poll loop — failure stops claiming immediately
  - acquire lease before start, release on stop
  - Crashed owner → lease expires → new owner can takeover
"""

from __future__ import annotations

import logging
import random
import threading
import time as _time
import uuid

from infrastructure.outbox.owner_lease import (
    OwnerLease,
    LeaseConflictError,
    HEARTBEAT_INTERVAL_S,
)
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from listeners.delivery import (
    Delivered,
    RetryableDeliveryFailure,
    PermanentDeliveryFailure,
)

logger = logging.getLogger(__name__)


class OutboxRelay:
    """Threaded relay with durable owner lease.

    G10: Must acquire() before start().  Heartbeat in poll loop.
         Release on stop().  Crash safe via lease expiry.
    """

    MAX_ATTEMPTS = 5
    POLL_INTERVAL_S = 0.5
    MAX_BACKOFF_S = 60

    def __init__(self, store: SqliteOutboxStore,
                 deliver,  # callable(OutboxRecord) -> DeliveryOutcome
                 lease: OwnerLease | None = None,
                 worker_id: str | None = None) -> None:
        self._store = store
        self._deliver = deliver
        self._lease = lease
        self._worker_id = worker_id or str(uuid.uuid4())[:8]
        self._running = False
        self._thread: threading.Thread | None = None
        self._inflight: int = 0
        self._lock = threading.Lock()
        self._last_heartbeat: float = 0.0
        self._lease_active = False

    def acquire_lease(self) -> None:
        """Acquire the owner lease.  Must be called before start().  Idempotent."""
        if self._lease_active:
            return
        if self._lease is None:
            self._lease_active = True
            return
        self._lease.acquire()
        self._lease_active = True
        logger.info("Lease '%s' acquired by process %d",
                     self._lease.owner_id, self._lease.process_id)

    def start(self) -> None:
        """Start the relay.  Lease must already be acquired."""
        if not self._lease_active:
            raise RuntimeError("Must acquire_lease() before start()")
        self._running = True
        self._last_heartbeat = _time.monotonic()
        self._thread = threading.Thread(
            target=self._run, daemon=False, name="outbox-relay",
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> int:
        """Signal stop, wait for thread, release lease.  Returns pending count."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
        # G10: Release lease on normal shutdown
        if self._lease is not None and self._lease_active:
            try:
                self._lease.release("normal_shutdown")
                self._lease_active = False
            except Exception:
                logger.debug("Lease release failed (may already be released)")
        return self._store.count_pending()

    def _run(self) -> None:
        while self._running:
            # G10: Heartbeat check
            if not self._check_lease():
                logger.error("Lease lost — stopping relay")
                self._running = False
                break

            batch = self._store.claim_batch(
                self._worker_id, limit=10, lease_s=30.0,
            )
            if not batch:
                _time.sleep(self.POLL_INTERVAL_S)
                continue

            with self._lock:
                self._inflight += len(batch)

            for record in batch:
                try:
                    outcome = self._deliver(record)

                    if isinstance(outcome, Delivered):
                        self._store.mark_delivered(
                            record.event_id, self._worker_id,
                        )
                    elif isinstance(outcome, RetryableDeliveryFailure):
                        new_attempts = record.attempts + 1
                        if new_attempts >= self.MAX_ATTEMPTS:
                            self._store.dead_letter(
                                record.event_id, self._worker_id,
                                outcome.reason[:500],
                            )
                        else:
                            delay = min(
                                2 ** (new_attempts - 1), self.MAX_BACKOFF_S,
                            )
                            jitter = random.uniform(0.5, 1.5)
                            self._store.reschedule(
                                record.event_id, self._worker_id,
                                outcome.reason[:500], delay_s=delay * jitter,
                            )
                    elif isinstance(outcome, PermanentDeliveryFailure):
                        self._store.dead_letter(
                            record.event_id, self._worker_id,
                            outcome.reason[:500],
                        )
                    else:
                        self._store.reschedule(
                            record.event_id, self._worker_id,
                            f"Unknown outcome: {type(outcome).__name__}",
                            delay_s=1.0,
                        )

                except Exception as exc:
                    new_attempts = record.attempts + 1
                    error_msg = f"{type(exc).__name__}: {exc}"
                    if new_attempts >= self.MAX_ATTEMPTS:
                        self._store.dead_letter(
                            record.event_id, self._worker_id,
                            error_msg[:500],
                        )
                    else:
                        delay = min(
                            2 ** (new_attempts - 1), self.MAX_BACKOFF_S,
                        ) * random.uniform(0.5, 1.5)
                        self._store.reschedule(
                            record.event_id, self._worker_id,
                            error_msg[:500], delay_s=delay,
                        )
                finally:
                    with self._lock:
                        self._inflight -= 1

    def _check_lease(self) -> bool:
        """Heartbeat if due.  Returns True if lease is still valid."""
        if self._lease is None:
            return True  # no lease configured → always OK

        now = _time.monotonic()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL_S:
            return True

        self._last_heartbeat = now
        ok = self._lease.heartbeat()
        if not ok:
            logger.error("Lease heartbeat failed — lost ownership")
        return ok

    @property
    def inflight(self) -> int:
        return self._inflight
