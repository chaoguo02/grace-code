"""
P23: Standalone memory maintenance job — extracted from AgentService.

Supports both asyncio (web) and threading (CLI) runtimes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

logger = logging.getLogger(__name__)


class MemoryMaintenanceJob:
    """Periodic memory pruning + decay.  Independent of AgentService lifecycle."""

    DEFAULT_INTERVAL_S = 6 * 3600  # 6 hours

    def __init__(self, memory_store, interval_s: int | None = None) -> None:
        self._store = memory_store
        self._interval = interval_s or int(
            os.environ.get("GRACE_MEMORY_MAINTENANCE_SECONDS", str(self.DEFAULT_INTERVAL_S))
        )
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._async_task: asyncio.Task | None = None
        self._async_stop: asyncio.Event | None = None

    # ── Threaded mode (CLI) ────────────────────────────────────────────

    def start_thread(self) -> None:
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="mem-maint")
        self._thread.start()

    def stop_thread(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run_thread(self) -> None:
        while not (self._stop_event and self._stop_event.wait(self._interval)):
            self._run_one_cycle()

    # ── Async mode (Web) ───────────────────────────────────────────────

    async def start_async(self) -> asyncio.Task:
        self._async_stop = asyncio.Event()
        self._async_task = asyncio.ensure_future(self._run_async())
        return self._async_task

    async def stop_async(self) -> None:
        if self._async_stop:
            self._async_stop.set()
        if self._async_task:
            self._async_task.cancel()
            try:
                await self._async_task
            except asyncio.CancelledError:
                pass

    async def _run_async(self) -> None:
        while not (self._async_stop and self._async_stop.is_set()):
            try:
                await asyncio.wait_for(self._async_stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                self._run_one_cycle()

    # ── Core logic ─────────────────────────────────────────────────────

    def _run_one_cycle(self) -> None:
        if self._store is None:
            return
        try:
            pruned = self._store.prune_expired()
            if pruned:
                logger.info("Memory maintenance: %d entries pruned", pruned)
        except Exception:
            logger.debug("Memory maintenance cycle skipped", exc_info=True)
