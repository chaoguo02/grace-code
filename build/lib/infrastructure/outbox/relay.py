"""
P8: Outbox Relay — threaded poller, structured stop.

stop() waits for in-flight batch to complete.  No daemon fire-and-forget.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from infrastructure.outbox.sqlite_store import SqliteOutboxStore

logger = logging.getLogger(__name__)


class OutboxRelay:
    """Threaded relay: poll → claim → deliver → ack.  Structured close."""

    MAX_ATTEMPTS = 5
    POLL_INTERVAL_S = 0.5

    def __init__(self, store: SqliteOutboxStore,
                 deliver: object,  # callable(OutboxRecord) -> None
                 worker_id: str | None = None) -> None:
        self._store = store
        self._deliver = deliver
        self._worker_id = worker_id or str(uuid.uuid4())[:8]
        self._running = False
        self._thread: threading.Thread | None = None
        self._inflight: int = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=False, name="outbox-relay")
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> int:
        """Signal stop and wait for thread.  Returns remaining pending count."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
        # Count remaining pending
        batch = self._store.claim_batch(self._worker_id, limit=1, lease_s=1.0)
        return len(batch)

    def _run(self) -> None:
        while self._running:
            batch = self._store.claim_batch(self._worker_id, limit=10, lease_s=30.0)
            if not batch:
                time.sleep(self.POLL_INTERVAL_S)
                continue
            with self._lock:
                self._inflight += len(batch)
            for record in batch:
                try:
                    self._deliver(record)
                    self._store.mark_delivered(record.event_id, self._worker_id)
                except Exception as exc:
                    new_attempts = record.attempts + 1
                    if new_attempts >= self.MAX_ATTEMPTS:
                        self._store.dead_letter(record.event_id, self._worker_id, str(exc)[:500])
                    else:
                        delay = min(2 ** (new_attempts - 1), 30)
                        self._store.reschedule(record.event_id, self._worker_id, str(exc)[:500], delay_s=delay)
                finally:
                    with self._lock:
                        self._inflight -= 1

    @property
    def inflight(self) -> int:
        return self._inflight
