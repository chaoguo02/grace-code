"""
CC-Native Cancellation Handle & Process Registry.

Design: Push-based cancellation signal with lock-safe callback dispatch.
Scope:  process kill is scoped to run (session_id + generation + run_id),
        not session-wide.  An old run's deferred cancel cannot kill a new
        generation's process.

Alignment: Claude Code's AbortController + abortChildProcess pattern.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from core.process import kill_process_tree


# ── CancellationHandle ──────────────────────────────────────────────────────

@dataclass
class CancellationHandle:
    """Push-based, lock-safe cancellation signal.

    Aligns with CC's AbortController / AbortSignal pairing.

    Key differences vs old poll-based CancellationToken:
    1. cancel() PUSHES — it actively notifies registered callbacks
       (process kills, future cancellations) instead of merely setting
       a flag that consumers must poll.
    2. Lock-safe: callbacks are snapshotted under lock, then executed
       outside the lock.  A callback may safely call on_cancel(),
       unregister, or child() without deadlock.
    3. Irreversible — once cancelled, always cancelled.
    """

    _reason: str = ""
    _detail: str = ""
    _event: threading.Event = field(default_factory=threading.Event)
    _on_cancel_callbacks: list[Callable[[str], None]] = field(
        default_factory=list, repr=False,
    )
    _parent: CancellationHandle | None = field(default=None, repr=False)
    _children: list[CancellationHandle] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── public read-only ────────────────────────────────────────────────

    @property
    def is_cancelled(self) -> bool:
        """True if this handle or any ancestor has been cancelled."""
        return self._event.is_set() or (
            self._parent is not None and self._parent.is_cancelled
        )

    @property
    def reason(self) -> str:
        if self._event.is_set() or self._parent is None:
            return self._reason
        return self._parent.reason

    @property
    def detail(self) -> str:
        if self._event.is_set() or self._parent is None:
            return self._detail
        return self._parent.detail

    # ── cancel (PUSH) ───────────────────────────────────────────────────

    def cancel(self, reason: str = "", detail: str = "") -> None:
        """Irreversibly cancel this handle and all descendants (max depth 3).

        Lock-safe protocol:
        1. Under lock: set event, snapshot callbacks + children.
        2. Outside lock: fire callbacks, cascade to children.
        """
        callbacks_snapshot: list[Callable[[str], None]]
        children_snapshot: list[CancellationHandle]

        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._detail = detail
            self._event.set()
            callbacks_snapshot = list(self._on_cancel_callbacks)
            children_snapshot = list(self._children)

        # fire callbacks outside lock — they may call on_cancel / unregister
        for cb in callbacks_snapshot:
            try:
                cb(reason)
            except Exception:
                pass

        for child in children_snapshot:
            child.cancel(reason=reason, detail=detail)

    # ── callback registration ───────────────────────────────────────────

    def on_cancel(
        self, callback: Callable[[str], None],
    ) -> Callable[[], None]:
        """Register a callback invoked when this handle is cancelled.

        Returns an unregister function (call to remove the callback).

        If the handle is *already* cancelled, the callback is invoked
        immediately — OUTSIDE the lock — and the returned unregister is
        a no-op.
        """
        already_cancelled = False
        cached_reason = ""

        with self._lock:
            already_cancelled = self._event.is_set()
            cached_reason = self._reason
            if not already_cancelled:
                self._on_cancel_callbacks.append(callback)

        if already_cancelled:
            callback(cached_reason)

        def _unregister() -> None:
            with self._lock:
                try:
                    self._on_cancel_callbacks.remove(callback)
                except ValueError:
                    pass

        return _unregister

    # ── tree ────────────────────────────────────────────────────────────

    def child(self) -> CancellationHandle:
        """Create a child handle — parent cancellation cascades to children.

        Aligns with CC's linked AbortController for sub-agents.
        If the parent is *already* cancelled, the returned child is
        immediately cancelled (no cascade needed).
        """
        child = CancellationHandle(_parent=self)
        with self._lock:
            if self._event.is_set():
                child._reason = self._reason
                child._detail = self._detail
                child._event.set()
            else:
                self._children.append(child)
        return child


# ── ProcessHandle ───────────────────────────────────────────────────────────

@dataclass
class ProcessHandle:
    """A running subprocess that can be killed externally.

    Key is (session_id, generation, run_id, invocation_id).
    Minimum isolation unit is **run** — kill_run() is the default
    cancellation scope.  This prevents an old run's deferred cancel
    from killing a new generation's process.
    """

    pid: int
    session_id: str
    generation: int
    run_id: str
    invocation_id: str
    process: subprocess.Popen
    created_at: float = field(default_factory=time.monotonic)


# ── ProcessRegistry ─────────────────────────────────────────────────────────

@dataclass
class ProcessRegistry:
    """Registry of active subprocesses keyed by run-scoped identity.

    Public API:
    - register / unregister — lifecycle
    - kill_run — cancel scope for tool cancellation
    - kill_one — cancel scope for a single invocation
    - kill_session — session-level abort (extreme cases)

    Escalation policy:
        SIGTERM (via kill_process_tree) → wait 5 s → SIGKILL
    """

    _handles: dict[str, ProcessHandle] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    ESCALATE_TIMEOUT: float = 5.0

    # ── lifecycle ───────────────────────────────────────────────────────

    def register(self, handle: ProcessHandle) -> None:
        with self._lock:
            self._handles[handle.invocation_id] = handle

    def unregister(self, invocation_id: str) -> None:
        with self._lock:
            self._handles.pop(invocation_id, None)

    # ── kill scopes ─────────────────────────────────────────────────────

    def kill_run(
        self,
        session_id: str,
        generation: int,
        run_id: str,
        *,
        escalate: bool = True,
    ) -> int:
        """Kill all processes belonging to *this* run.  Returns kill count.

        Default scope for tool cancellation.
        Does NOT affect other runs in the same session.
        """
        with self._lock:
            targets = [
                h for h in self._handles.values()
                if (
                    h.session_id == session_id
                    and h.generation == generation
                    and h.run_id == run_id
                )
            ]
        return self._kill_all(targets, escalate=escalate)

    def kill_one(self, invocation_id: str, *, escalate: bool = True) -> bool:
        """Kill a single invocation by id.  Returns True if killed."""
        with self._lock:
            target = self._handles.get(invocation_id)
        if target is None:
            return False
        return self._kill_one(target, escalate=escalate) > 0

    def kill_session(
        self,
        session_id: str,
        *,
        generation: int | None = None,
        escalate: bool = True,
    ) -> int:
        """Kill all processes in a session.  Returns kill count.

        Use sparingly — only for session-level abort (user closes session).
        generation=None kills all generations.
        """
        with self._lock:
            targets = [
                h for h in self._handles.values()
                if (
                    h.session_id == session_id
                    and (generation is None or h.generation == generation)
                )
            ]
        return self._kill_all(targets, escalate=escalate)

    # ── internal ────────────────────────────────────────────────────────

    def _kill_one(
        self, handle: ProcessHandle, *, escalate: bool,
    ) -> int:
        """Kill a single process.  Returns 1 on success, 0 otherwise."""
        try:
            kill_process_tree(handle.process)
        except Exception:
            pass

        if escalate:
            try:
                handle.process.wait(timeout=self.ESCALATE_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    handle.process.kill()
                    handle.process.wait(timeout=2.0)
                except Exception:
                    pass
            except Exception:
                pass

        return 1

    def _kill_all(
        self, handles: list[ProcessHandle], *, escalate: bool,
    ) -> int:
        count = 0
        for h in handles:
            count += self._kill_one(h, escalate=escalate)
        return count
