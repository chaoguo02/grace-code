"""Thread-safe resource admission, accounting, queueing, and reconciliation.

The governor deliberately separates two resource semantics:

* renewable capacity (workers, worktrees, provider/tool/LLM slots) is reserved
  while a lease is active and becomes available again on release;
* consumable quota (tokens, event/db/disk budgets) is reconciled to actual
  usage and remains consumed.

All capacity checks and reservations happen under one condition lock.  This
prevents check-then-reserve races and also provides the wake-up mechanism used
by synchronous runtime callers waiting in the admission queue.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


class ResourceKind(str, Enum):
    WORKER_SLOT = "worker_slot"
    LLM_SLOT = "llm_slot"
    PROVIDER_RPM = "provider_rpm"
    PROVIDER_TPM = "provider_tpm"
    TOOL_SLOT = "tool_slot"
    TOKEN_BUDGET = "token_budget"
    EVENT_CAPACITY = "event_capacity"
    WORKTREE_SLOT = "worktree_slot"
    DISK_BYTES = "disk_bytes"
    DB_WRITE_CAPACITY = "db_write_capacity"


class AdmissionOutcome(str, Enum):
    GRANTED = "granted"
    QUEUED = "queued"
    CANCELLED = "cancelled"
    CAPACITY_TIMEOUT = "capacity_timeout"
    IMPOSSIBLE = "impossible"
    SHUTDOWN = "shutdown"


class ResourcePressure(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    DRAINING = "draining"


class ResourceAdmissionError(ValueError):
    """Typed fail-closed error raised when required capacity is unavailable."""

    def __init__(
        self,
        outcome: AdmissionOutcome,
        kind: ResourceKind,
        reason: str = "",
    ) -> None:
        self.outcome = AdmissionOutcome(outcome)
        self.kind = ResourceKind(kind)
        self.reason = reason
        super().__init__(
            f"Resource governance: {self.outcome.value}"
            + (f" — {reason}" if reason else "")
        )


@dataclass(frozen=True)
class ResourceRequest:
    request_id: str
    root_session_id: str
    session_id: str
    run_id: str = ""
    task_id: str = ""
    resources: dict[ResourceKind, int] = field(default_factory=dict)
    priority: int = 0
    timeout_s: float = 120.0
    cancel_token: Any = None


@dataclass
class AdmissionResult:
    outcome: AdmissionOutcome
    lease: "ResourceLease | None" = None
    queue_position: int = 0
    wait_time_s: float = 0.0
    reason: str = ""
    pressure: ResourcePressure = ResourcePressure.NORMAL


@dataclass
class ResourceSnapshot:
    kind: ResourceKind
    limit: int
    consumed: int = 0
    reserved: int = 0
    available: int = 0
    queued: int = 0
    queue_wait_max_s: float = 0.0
    pressure: ResourcePressure = ResourcePressure.NORMAL


@dataclass
class ResourceGovernorSnapshot:
    timestamp_s: float
    mode: str
    snapshots: dict[ResourceKind, ResourceSnapshot] = field(default_factory=dict)
    total_grants: int = 0
    total_rejections: int = 0
    total_timeouts: int = 0
    active_leases: int = 0


_RENEWABLE_KINDS = frozenset({
    ResourceKind.WORKER_SLOT,
    ResourceKind.LLM_SLOT,
    ResourceKind.TOOL_SLOT,
    ResourceKind.WORKTREE_SLOT,
})


class ResourceLease:
    """An idempotently releasable lease covering an atomic resource bundle."""

    def __init__(
        self,
        lease_id: str,
        root_session_id: str,
        resources: Mapping[ResourceKind, int],
        governor: "ResourceGovernor",
        *,
        request: ResourceRequest | None = None,
    ) -> None:
        self.lease_id = lease_id
        self.root_session_id = root_session_id
        self.resources = dict(resources)
        self.request = request
        self._governor = governor
        self._released = False
        self._release_lock = threading.Lock()

    @property
    def kind(self) -> ResourceKind:
        """Compatibility view: worker when present, otherwise first resource."""
        if ResourceKind.WORKER_SLOT in self.resources:
            return ResourceKind.WORKER_SLOT
        return next(iter(self.resources), ResourceKind.WORKER_SLOT)

    @property
    def reserved(self) -> int:
        """Compatibility view for callers that previously used one resource."""
        return self.resources.get(self.kind, 0)

    def release(
        self,
        actual_used: int | Mapping[ResourceKind, int] = 0,
    ) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._governor._release_lease(self, actual_used)

    def is_released(self) -> bool:
        with self._release_lock:
            return self._released

    def __enter__(self) -> "ResourceLease":
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


@dataclass
class _QueuedRequest:
    request: ResourceRequest
    enqueued_at: float
    event: threading.Event = field(default_factory=threading.Event)
    result: AdmissionResult | None = None


class ResourceGovernor:
    """Central, thread-safe resource owner for all multi-agent execution."""

    def __init__(self, config: object) -> None:
        self._config = config
        self._mode = _validate_mode(str(getattr(config, "mode", "observe")))
        self._condition = threading.Condition(threading.RLock())
        self._limits: dict[ResourceKind, int] = {}
        self._root_limits: dict[ResourceKind, int] = {}
        self._reserved: dict[ResourceKind, int] = {
            kind: 0 for kind in ResourceKind
        }
        self._consumed: dict[ResourceKind, int] = {
            kind: 0 for kind in ResourceKind
        }
        self._root_reserved: dict[str, dict[ResourceKind, int]] = {}
        # Per-run capacity — set by ModeExecutionPolicy for serial modes
        self._run_limits: dict[str, dict[ResourceKind, int]] = {}
        self._run_reserved: dict[str, dict[ResourceKind, int]] = {}
        self._leases: dict[str, ResourceLease] = {}
        self._queue: list[_QueuedRequest] = []
        self._queued_by_id: dict[str, _QueuedRequest] = {}
        self._queue_max_size = 64
        self._queue_timeout_s = 120.0
        self._shutting_down = False
        self._total_grants = 0
        self._total_rejections = 0
        self._total_timeouts = 0
        self._total_cancellations = 0
        self._total_impossibles = 0
        self._blocked_would_be: dict[ResourceKind, int] = {
            kind: 0 for kind in ResourceKind
        }
        self._pressure_callbacks: list[
            Callable[[ResourcePressure, ResourcePressure, ResourceKind], None]
        ] = []
        self._event_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._last_pressure: dict[ResourceKind, ResourcePressure] = {
            kind: ResourcePressure.NORMAL for kind in ResourceKind
        }
        # Rotate grants across roots when multiple roots can run. FIFO is
        # still preserved within each root.
        self._last_granted_root_id = ""
        self._init_limits()

        # Compatibility aliases for code/tests that inspect the old locks.
        self._accounting_lock = self._condition
        self._root_lock = self._condition
        self._leases_lock = self._condition
        self._queue_lock = self._condition
        self._metrics_lock = self._condition
        self._shutdown_lock = self._condition

    def _init_limits(self) -> None:
        worker = getattr(self._config, "worker", None)
        worktree = getattr(self._config, "worktree", None)
        event_cfg = getattr(self._config, "event", None)
        tool_cfg = getattr(self._config, "tool", None)
        queue_cfg = getattr(self._config, "queue", None)
        self._limits[ResourceKind.WORKER_SLOT] = max(
            0, int(getattr(worker, "global_max", 2))
        )
        self._root_limits[ResourceKind.WORKER_SLOT] = max(
            0, int(getattr(worker, "per_root_max", 2))
        )
        self._limits[ResourceKind.WORKTREE_SLOT] = max(
            0, int(getattr(worktree, "global_max", 10))
        )
        self._root_limits[ResourceKind.WORKTREE_SLOT] = max(
            0, int(getattr(worktree, "per_root_max", 3))
        )
        event_limit = int(getattr(event_cfg, "queue_max_size", 0))
        self._limits[ResourceKind.EVENT_CAPACITY] = max(0, event_limit)
        self._limits[ResourceKind.TOOL_SLOT] = max(
            0, int(getattr(tool_cfg, "global_max", 8))
        )
        self._root_limits[ResourceKind.TOOL_SLOT] = max(
            0, int(getattr(tool_cfg, "per_root_max", 4))
        )
        self._queue_max_size = max(1, int(getattr(queue_cfg, "max_size", 64)))
        self._queue_timeout_s = max(
            0.001, float(getattr(queue_cfg, "timeout_seconds", 120.0))
        )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_shutting_down(self) -> bool:
        with self._condition:
            return self._shutting_down

    @property
    def queue_depth(self) -> int:
        with self._condition:
            return len(self._queue)

    @property
    def active_leases_count(self) -> int:
        with self._condition:
            return len(self._leases)

    def admit(self, request: ResourceRequest) -> AdmissionResult:
        """Try immediate admission, or enqueue in enforce mode.

        Callers that are able to wait should use :meth:`admit_wait`; ``admit``
        remains non-blocking for API inspection and compatibility.
        """
        request = self._normalize_request(request)
        events: list[dict[str, Any]] = []
        with self._condition:
            if self._shutting_down:
                return AdmissionResult(
                    AdmissionOutcome.SHUTDOWN,
                    reason="Governor is shutting down",
                    pressure=ResourcePressure.DRAINING,
                )
            pressure = self._compute_pressure_locked(request)
            impossible = self._check_impossible_locked(request)
            if impossible and self._mode == "enforce":
                self._total_impossibles += 1
                return AdmissionResult(
                    AdmissionOutcome.IMPOSSIBLE,
                    reason=impossible,
                    pressure=ResourcePressure.CRITICAL,
                )

            blocked = self._would_block_locked(request)
            if self._mode == "observe":
                for kind, value in blocked.items():
                    if value:
                        self._blocked_would_be[kind] += 1
                lease = self._grant_locked(request)
                self._total_grants += 1
                result = AdmissionResult(
                    AdmissionOutcome.GRANTED, lease=lease, pressure=pressure
                )
                events.append(self._resource_event("granted", request, result))
            elif self._mode == "soft_enforce":
                if any(blocked.values()):
                    logger.warning(
                        "soft_enforce: would block request %s for %s",
                        request.request_id,
                        [kind.name for kind, value in blocked.items() if value],
                    )
                lease = self._grant_locked(request)
                self._total_grants += 1
                result = AdmissionResult(
                    AdmissionOutcome.GRANTED, lease=lease, pressure=pressure
                )
                events.append(self._resource_event("granted", request, result))
            elif not self._queue and not any(blocked.values()):
                lease = self._grant_locked(request)
                self._total_grants += 1
                result = AdmissionResult(
                    AdmissionOutcome.GRANTED, lease=lease, pressure=pressure
                )
                events.append(self._resource_event("granted", request, result))
            elif len(self._queue) >= self._queue_max_size:
                self._total_rejections += 1
                result = AdmissionResult(
                    AdmissionOutcome.CAPACITY_TIMEOUT,
                    reason=f"Queue full ({self._queue_max_size})",
                    pressure=ResourcePressure.CRITICAL,
                )
                events.append(self._resource_event("rejected", request, result))
            else:
                result = AdmissionResult(
                    AdmissionOutcome.QUEUED,
                    queue_position=len(self._queue) + 1,
                    pressure=pressure,
                )
                queued = _QueuedRequest(
                    request=request,
                    enqueued_at=time.monotonic(),
                    result=result,
                )
                self._queue.append(queued)
                self._queued_by_id[request.request_id] = queued
                events.append(self._resource_event("queued", request, result))
            self._condition.notify_all()
        self._emit_events(events)
        self._notify_pressure_changes(request)
        return result

    def admit_wait(
        self,
        request: ResourceRequest,
        timeout_s: float | None = None,
    ) -> AdmissionResult:
        """Admit and, when queued, block until grant/cancel/timeout/shutdown."""
        started = time.monotonic()
        result = self.admit(request)
        if result.outcome is not AdmissionOutcome.QUEUED:
            result.wait_time_s = time.monotonic() - started
            return result

        with self._condition:
            queued_entry = self._queued_by_id.get(request.request_id)
        if queued_entry is None:
            # A release can grant the shared result object between admit()
            # returning and this lookup. Do not misreport that race as a
            # cancellation.
            if result.outcome is not AdmissionOutcome.QUEUED:
                result.wait_time_s = time.monotonic() - started
                return result
            return AdmissionResult(
                AdmissionOutcome.CANCELLED,
                wait_time_s=time.monotonic() - started,
                reason="Queued request disappeared before wait began",
            )

        effective_timeout = (
            timeout_s
            if timeout_s is not None
            else request.timeout_s if request.timeout_s > 0
            else self._queue_timeout_s
        )
        deadline = started + max(0.001, effective_timeout)
        while True:
            with self._condition:
                if (
                    queued_entry.result is not None
                    and queued_entry.result.outcome
                    is not AdmissionOutcome.QUEUED
                ):
                    result = queued_entry.result
                    break
                token = request.cancel_token
                if token is not None and getattr(token, "is_cancelled", False):
                    self._cancel_locked(
                        request.request_id,
                        AdmissionOutcome.CANCELLED,
                        getattr(token, "detail", "") or "Request cancelled",
                    )
                    result = AdmissionResult(
                        AdmissionOutcome.CANCELLED,
                        reason=getattr(token, "detail", "") or "Request cancelled",
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._cancel_locked(
                        request.request_id,
                        AdmissionOutcome.CAPACITY_TIMEOUT,
                        f"Capacity wait exceeded {effective_timeout:.3f}s",
                    )
                    self._total_timeouts += 1
                    result = AdmissionResult(
                        AdmissionOutcome.CAPACITY_TIMEOUT,
                        reason=f"Capacity wait exceeded {effective_timeout:.3f}s",
                        pressure=ResourcePressure.CRITICAL,
                    )
                    break
                self._condition.wait(timeout=min(0.1, remaining))

        result.wait_time_s = time.monotonic() - started
        self._emit_events([
            self._resource_event(result.outcome.value, request, result)
        ])
        return result

    def cancel_request(self, request_id: str) -> bool:
        with self._condition:
            cancelled = self._cancel_locked(
                request_id, AdmissionOutcome.CANCELLED, "Request cancelled"
            )
            if cancelled:
                self._total_cancellations += 1
                self._condition.notify_all()
        return cancelled

    def snapshot(self) -> ResourceGovernorSnapshot:
        now = time.monotonic()
        with self._condition:
            snapshots: dict[ResourceKind, ResourceSnapshot] = {}
            for kind in ResourceKind:
                limit = self._limits.get(kind, 0)
                reserved = self._reserved.get(kind, 0)
                consumed = self._consumed.get(kind, 0)
                capacity_used = reserved + (
                    consumed if kind not in _RENEWABLE_KINDS else 0
                )
                available = max(0, limit - capacity_used) if limit > 0 else 0
                queued_entries = [
                    item for item in self._queue
                    if item.request.resources.get(kind, 0) > 0
                ]
                wait_max = max(
                    (now - item.enqueued_at for item in queued_entries),
                    default=0.0,
                )
                snapshots[kind] = ResourceSnapshot(
                    kind=kind,
                    limit=limit,
                    consumed=consumed,
                    reserved=reserved,
                    available=available,
                    queued=len(queued_entries),
                    queue_wait_max_s=wait_max,
                    pressure=self._kind_pressure(kind, limit, consumed, reserved),
                )
            return ResourceGovernorSnapshot(
                timestamp_s=now,
                mode=self._mode,
                snapshots=snapshots,
                total_grants=self._total_grants,
                total_rejections=self._total_rejections,
                total_timeouts=self._total_timeouts,
                active_leases=len(self._leases),
            )

    # ── Per-run capacity (ModeExecutionPolicy integration) ──────────────

    def set_limit_for_run(
        self, run_id: str, kind: ResourceKind, limit: int,
    ) -> None:
        """Set a per-run capacity limit for one resource kind.

        When *limit* is 0, the limit is removed.  Used by serial modes
        (Build, Plan) to enforce ``max_in_flight_workers == 1``.
        """
        with self._condition:
            if limit <= 0:
                self._run_limits.pop(run_id, None)
                return
            run_limits = self._run_limits.setdefault(run_id, {})
            run_limits[kind] = max(0, limit)

    def remove_limit_for_run(self, run_id: str) -> None:
        """Remove all per-run limits when a Run terminates."""
        with self._condition:
            self._run_limits.pop(run_id, None)
            self._run_reserved.pop(run_id, None)

    def shutdown(self) -> None:
        events: list[dict[str, Any]] = []
        with self._condition:
            if self._shutting_down:
                return
            self._shutting_down = True
            for entry in list(self._queue):
                result = entry.result or AdmissionResult(
                    AdmissionOutcome.SHUTDOWN,
                )
                result.outcome = AdmissionOutcome.SHUTDOWN
                result.reason = "Governor is shutting down"
                result.pressure = ResourcePressure.DRAINING
                entry.result = result
                entry.event.set()
                events.append(
                    self._resource_event("shutdown", entry.request, result)
                )
            self._queue.clear()
            self._queued_by_id.clear()
            self._condition.notify_all()
        self._emit_events(events)

    def blocked_would_be_counts(self) -> dict[str, int]:
        with self._condition:
            return {
                kind.name: count
                for kind, count in self._blocked_would_be.items()
            }

    def on_pressure_change(
        self,
        callback: Callable[
            [ResourcePressure, ResourcePressure, ResourceKind], None
        ],
    ) -> None:
        self._pressure_callbacks.append(callback)

    def on_event(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._event_callbacks.append(callback)

    def publish_accounting_event(
        self,
        phase: str,
        request: ResourceRequest,
        resources: Mapping[ResourceKind, int],
        *,
        actual: Mapping[ResourceKind, int] | None = None,
    ) -> None:
        """Publish facts owned by another authority without enforcing twice.

        ExecutionBudget uses this for token lifecycle visibility. No governor
        reservation, consumption, or admission decision is performed here.
        """
        accounting_request = ResourceRequest(
            request_id=request.request_id,
            root_session_id=request.root_session_id,
            session_id=request.session_id,
            run_id=request.run_id,
            task_id=request.task_id,
            resources=dict(resources),
            priority=request.priority,
            timeout_s=request.timeout_s,
            cancel_token=request.cancel_token,
        )
        result = AdmissionResult(
            AdmissionOutcome.GRANTED,
            reason="accounting_only",
        )
        self._emit_events([
            self._resource_event(
                phase,
                accounting_request,
                result,
                actual=actual,
            )
        ])

    def get_per_root_usage(self, root_id: str, kind: ResourceKind) -> int:
        with self._condition:
            return self._root_reserved.get(root_id, {}).get(kind, 0)

    def _release_lease(
        self,
        lease: ResourceLease,
        actual_used: int | Mapping[ResourceKind, int],
    ) -> None:
        events: list[dict[str, Any]] = []
        with self._condition:
            if lease.lease_id not in self._leases:
                return
            actual = self._normalize_actual_usage(lease, actual_used)
            for kind, reserved in lease.resources.items():
                self._reserved[kind] = max(
                    0, self._reserved.get(kind, 0) - reserved
                )
                root_map = self._root_reserved.get(lease.root_session_id, {})
                root_map[kind] = max(0, root_map.get(kind, 0) - reserved)
                # Per-run tracking
                _req = lease.request
                if _req is not None:
                    _rid = getattr(_req, "run_id", "") or ""
                    if _rid:
                        run_map = self._run_reserved.get(_rid, {})
                        run_map[kind] = max(0, run_map.get(kind, 0) - reserved)
                if kind not in _RENEWABLE_KINDS:
                    self._consumed[kind] = (
                        self._consumed.get(kind, 0)
                        + max(0, int(actual.get(kind, 0)))
                    )
            self._leases.pop(lease.lease_id, None)
            request = lease.request
            if request is not None:
                result = AdmissionResult(
                    AdmissionOutcome.GRANTED,
                    lease=lease,
                    reason="released",
                )
                events.append(
                    self._resource_event(
                        "released", request, result, actual=actual
                    )
                )
            events.extend(self._drain_queue_locked())
            self._condition.notify_all()
        self._emit_events(events)
        self._notify_pressure_changes(request)

    def _grant_locked(self, request: ResourceRequest) -> ResourceLease:
        lease = ResourceLease(
            lease_id=f"lease-{uuid.uuid4().hex}",
            root_session_id=request.root_session_id,
            resources=request.resources,
            governor=self,
            request=request,
        )
        root_map = self._root_reserved.setdefault(
            request.root_session_id,
            {kind: 0 for kind in ResourceKind},
        )
        # Per-run tracking
        run_id = getattr(request, "run_id", "") or ""
        run_map: dict[ResourceKind, int] = {}
        if run_id and run_id in self._run_limits:
            run_map = self._run_reserved.setdefault(
                run_id, {kind: 0 for kind in ResourceKind},
            )
        for kind, amount in request.resources.items():
            self._reserved[kind] = self._reserved.get(kind, 0) + amount
            root_map[kind] = root_map.get(kind, 0) + amount
            if run_map:
                run_map[kind] = run_map.get(kind, 0) + amount
        self._leases[lease.lease_id] = lease
        self._last_granted_root_id = request.root_session_id
        return lease

    def _drain_queue(self) -> None:
        with self._condition:
            events = self._drain_queue_locked()
            self._condition.notify_all()
        self._emit_events(events)

    def _drain_queue_locked(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = time.monotonic()
        while self._queue:
            # Purge terminal entries throughout the queue. A request blocked
            # by its own per-root quota must not stall unrelated roots.
            for entry in list(self._queue):
                request = entry.request
                outcome: AdmissionOutcome | None = None
                reason = ""
                if self._request_cancelled(request):
                    outcome = AdmissionOutcome.CANCELLED
                    reason = "Request cancelled"
                else:
                    timeout = (
                        request.timeout_s
                        if request.timeout_s > 0
                        else self._queue_timeout_s
                    )
                    if now - entry.enqueued_at >= timeout:
                        outcome = AdmissionOutcome.CAPACITY_TIMEOUT
                        reason = f"Capacity wait exceeded {timeout:.3f}s"
                if outcome is None:
                    continue
                self._queue.remove(entry)
                self._queued_by_id.pop(request.request_id, None)
                entry.result = entry.result or AdmissionResult(
                    AdmissionOutcome.QUEUED,
                )
                entry.result.outcome = outcome
                entry.result.wait_time_s = now - entry.enqueued_at
                entry.result.reason = reason
                entry.event.set()
                if outcome is AdmissionOutcome.CANCELLED:
                    self._total_cancellations += 1
                else:
                    self._total_timeouts += 1
                events.append(
                    self._resource_event(outcome.value, request, entry.result)
                )

            grantable = [
                item for item in self._queue
                if not any(self._would_block_locked(item.request).values())
            ]
            if not grantable:
                break
            other_root = [
                item for item in grantable
                if item.request.root_session_id != self._last_granted_root_id
            ]
            entry = (other_root or grantable)[0]
            request = entry.request
            self._queue.remove(entry)
            self._queued_by_id.pop(request.request_id, None)
            lease = self._grant_locked(request)
            entry.result = entry.result or AdmissionResult(
                AdmissionOutcome.QUEUED,
            )
            entry.result.outcome = AdmissionOutcome.GRANTED
            entry.result.lease = lease
            entry.result.queue_position = 0
            entry.result.wait_time_s = now - entry.enqueued_at
            entry.result.pressure = self._compute_pressure_locked(request)
            entry.event.set()
            self._total_grants += 1
            events.append(self._resource_event("granted", request, entry.result))
        return events

    def _cancel_locked(
        self,
        request_id: str,
        outcome: AdmissionOutcome,
        reason: str,
    ) -> bool:
        entry = self._queued_by_id.pop(request_id, None)
        if entry is None:
            return False
        try:
            self._queue.remove(entry)
        except ValueError:
            pass
        entry.result = entry.result or AdmissionResult(
            AdmissionOutcome.QUEUED,
        )
        entry.result.outcome = outcome
        entry.result.reason = reason
        entry.event.set()
        return True

    def _check_impossible_locked(self, request: ResourceRequest) -> str:
        for kind, amount in request.resources.items():
            limit = self._limits.get(kind, 0)
            root_limit = self._root_limits.get(kind, 0)
            if limit > 0 and amount > limit:
                return (
                    f"Requested {amount} {kind.name} exceeds total capacity "
                    f"of {limit}"
                )
            if root_limit > 0 and amount > root_limit:
                return (
                    f"Requested {amount} {kind.name} exceeds per-root capacity "
                    f"of {root_limit}"
                )
        return ""

    def _would_block(self, request: ResourceRequest) -> dict[ResourceKind, bool]:
        with self._condition:
            return self._would_block_locked(request)

    def _would_block_locked(
        self, request: ResourceRequest
    ) -> dict[ResourceKind, bool]:
        root_map = self._root_reserved.get(request.root_session_id, {})
        run_id = getattr(request, "run_id", "") or ""
        run_limits = self._run_limits.get(run_id, {}) if run_id else {}
        run_map = self._run_reserved.get(run_id, {}) if run_id else {}
        result: dict[ResourceKind, bool] = {}
        for kind, amount in request.resources.items():
            limit = self._limits.get(kind, 0)
            root_limit = self._root_limits.get(kind, 0)
            run_limit = run_limits.get(kind, 0)
            consumed = (
                self._consumed.get(kind, 0)
                if kind not in _RENEWABLE_KINDS
                else 0
            )
            result[kind] = (
                limit > 0
                and consumed + self._reserved.get(kind, 0) + amount > limit
            ) or (
                root_limit > 0
                and root_map.get(kind, 0) + amount > root_limit
            ) or (
                run_limit > 0
                and run_map.get(kind, 0) + amount > run_limit
            )
        return result

    def _compute_pressure(self, request: ResourceRequest) -> ResourcePressure:
        with self._condition:
            return self._compute_pressure_locked(request)

    def _compute_pressure_locked(
        self, request: ResourceRequest
    ) -> ResourcePressure:
        result = ResourcePressure.NORMAL
        for kind, amount in request.resources.items():
            limit = self._limits.get(kind, 0)
            if limit <= 0:
                continue
            consumed = (
                self._consumed.get(kind, 0)
                if kind not in _RENEWABLE_KINDS
                else 0
            )
            ratio = (
                consumed + self._reserved.get(kind, 0) + amount
            ) / limit
            if ratio >= 0.85:
                return ResourcePressure.CRITICAL
            if ratio >= 0.60:
                result = ResourcePressure.WARNING
        return result

    @staticmethod
    def _kind_pressure(
        kind: ResourceKind,
        limit: int,
        consumed: int,
        reserved: int,
    ) -> ResourcePressure:
        if limit <= 0:
            return ResourcePressure.NORMAL
        capacity_consumed = consumed if kind not in _RENEWABLE_KINDS else 0
        ratio = (capacity_consumed + reserved) / limit
        if ratio >= 0.85:
            return ResourcePressure.CRITICAL
        if ratio >= 0.60:
            return ResourcePressure.WARNING
        return ResourcePressure.NORMAL

    @staticmethod
    def _request_cancelled(request: ResourceRequest) -> bool:
        return (
            request.cancel_token is not None
            and getattr(request.cancel_token, "is_cancelled", False)
        )

    @staticmethod
    def _normalize_request(request: ResourceRequest) -> ResourceRequest:
        resources = {
            ResourceKind(kind): max(0, int(amount))
            for kind, amount in request.resources.items()
            if int(amount) >= 0
        }
        if resources == request.resources:
            return request
        return ResourceRequest(
            request_id=request.request_id,
            root_session_id=request.root_session_id,
            session_id=request.session_id,
            run_id=request.run_id,
            task_id=request.task_id,
            resources=resources,
            priority=request.priority,
            timeout_s=request.timeout_s,
            cancel_token=request.cancel_token,
        )

    @staticmethod
    def _normalize_actual_usage(
        lease: ResourceLease,
        actual_used: int | Mapping[ResourceKind, int],
    ) -> dict[ResourceKind, int]:
        if isinstance(actual_used, Mapping):
            return {
                ResourceKind(kind): max(0, int(value))
                for kind, value in actual_used.items()
            }
        if ResourceKind.TOKEN_BUDGET in lease.resources:
            return {ResourceKind.TOKEN_BUDGET: max(0, int(actual_used))}
        return {lease.kind: max(0, int(actual_used))}

    def _resource_event(
        self,
        phase: str,
        request: ResourceRequest,
        result: AdmissionResult,
        *,
        actual: Mapping[ResourceKind, int] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": f"delegation_resource_{phase}",
            "request_id": request.request_id,
            "root_session_id": request.root_session_id,
            "session_id": request.session_id,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "resources": {
                kind.value: amount for kind, amount in request.resources.items()
            },
            "actual": {
                kind.value: amount for kind, amount in (actual or {}).items()
            },
            "outcome": result.outcome.value,
            "queue_position": result.queue_position,
            "wait_time_s": result.wait_time_s,
            "reason": result.reason,
            "timestamp_s": time.time(),
        }

    def _emit_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            for callback in tuple(self._event_callbacks):
                try:
                    callback(event)
                except Exception:
                    logger.exception("Resource event callback failed")

    def _notify_pressure_changes(
        self,
        request: ResourceRequest | None = None,
    ) -> None:
        if not self._pressure_callbacks and not self._event_callbacks:
            return
        snap = self.snapshot()
        for kind, resource in snap.snapshots.items():
            old = self._last_pressure.get(kind, ResourcePressure.NORMAL)
            new = resource.pressure
            if old is new:
                continue
            self._last_pressure[kind] = new
            if request is not None:
                self._emit_events([{
                    "type": "resource_pressure_changed",
                    "request_id": request.request_id,
                    "root_session_id": request.root_session_id,
                    "session_id": request.session_id,
                    "run_id": request.run_id,
                    "task_id": request.task_id,
                    "resource_kind": kind.value,
                    "old_pressure": old.value,
                    "pressure": new.value,
                    "timestamp_s": time.time(),
                }])
            for callback in tuple(self._pressure_callbacks):
                try:
                    callback(old, new, kind)
                except Exception:
                    logger.exception("Resource pressure callback failed")


_VALID_MODES = frozenset({"observe", "soft_enforce", "enforce"})


def _validate_mode(mode: str) -> str:
    normalized = mode.lower().strip()
    if normalized not in _VALID_MODES:
        logger.warning(
            "Unknown resource_governance mode '%s', falling back to observe",
            mode,
        )
        return "observe"
    return normalized
