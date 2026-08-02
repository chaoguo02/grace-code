"""
G35: Memory Scheduler — bootstrap, prune, maintain. Not in AgentService/Runtime.

- Explicit start/stop with owned task (non-daemon thread).
- bootstrap(): initial memory load on session start.
- prune(): remove expired/irrelevant entries.
- maintain(): periodic upkeep.
- shutdown waits for active maintenance cycle to complete.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MemoryTask:
    """A single memory maintenance operation."""
    name: str
    fn: Callable[[], None]
    interval_s: float = 300.0
    last_run: float = 0.0


class MemoryScheduler:
    """Scheduled memory maintenance with owned lifecycle.

    Not a daemon — stop() blocks until current cycle completes.
    """

    def __init__(self) -> None:
        self._tasks: list[MemoryTask] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._active_cycle = threading.Event()
        self._active_cycle.set()  # initially not in a cycle

    # ── Task registration ──────────────────────────────────────────────

    def register(self, name: str, fn: Callable[[], None],
                 interval_s: float = 300.0) -> None:
        self._tasks.append(MemoryTask(name=name, fn=fn, interval_s=interval_s))

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the scheduler.  Idempotent."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="memory-scheduler", daemon=False,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 10.0) -> None:
        """Signal stop and wait for current cycle to finish.

        G35: Shutdown waits — no fire-and-forget.
        """
        self._running = False
        if self._thread is not None:
            # Wait for any active cycle to complete
            self._active_cycle.wait(timeout=timeout_s)
            self._thread.join(timeout=timeout_s)
            self._thread = None

    # ── Bootstrap / Prune / Maintain ───────────────────────────────────

    def bootstrap(self, session_id: str) -> None:
        """Initial memory load for a session.  Synchronous."""
        for task in self._tasks:
            if "bootstrap" in task.name:
                task.fn()
                task.last_run = _time.monotonic()

    def prune(self) -> int:
        """Remove expired entries.  Returns count pruned."""
        count = 0
        for task in self._tasks:
            if "prune" in task.name:
                task.fn()
                task.last_run = _time.monotonic()
                count += 1
        return count

    # ── Internal run loop ──────────────────────────────────────────────

    def _run(self) -> None:
        while self._running:
            self._active_cycle.clear()
            now = _time.monotonic()
            for task in self._tasks:
                if now - task.last_run >= task.interval_s:
                    try:
                        task.fn()
                    except Exception:
                        pass  # single task failure doesn't kill scheduler
                    task.last_run = now
            self._active_cycle.set()
            _time.sleep(1.0)  # check every second

    @property
    def running(self) -> bool:
        return self._running

    @property
    def task_count(self) -> int:
        return len(self._tasks)
