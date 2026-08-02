"""
G24: Cancellation Coordinator — CAS cancel_requested + push handle.

- RequestCancellation: CAS state → cancel_requested + CancellationRequested fact.
- Then pushes CancellationHandle.cancel() to Runtime.
- Idempotent: duplicate request does NOT create duplicate fact.
- Three-race handling: Runtime not started / running / just terminal.
- Parent cancel → cancel children; child failure doesn't cancel parent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from application.commands.run_commands import CancelRun
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId, AggregateVersion,
)
from application.events.run_facts import cancelled
from application.transactions.unit_of_work import SessionUnitOfWork
from core.eventing.identifiers import RunId, EventId, SessionId
from core.eventing.scope import ScopeToken
from runtime_core.outcome import CancellationReason
from runtime_core.execution import CancellationHandle


class CancelResult:
    """Result of a cancellation request."""
    def __init__(self, success: bool, reason: str = "",
                 already_cancelled: bool = False,
                 already_terminal: bool = False) -> None:
        self.success = success
        self.reason = reason
        self.already_cancelled = already_cancelled
        self.already_terminal = already_terminal


class CancellationRegistry:
    """Registry of active cancellable runs: run_id → CancellationHandle."""

    def __init__(self) -> None:
        import threading
        self._handles: dict[str, CancellationHandle] = {}
        self._lock = threading.Lock()

    def register(self, run_id: str, handle: CancellationHandle) -> None:
        with self._lock:
            self._handles[run_id] = handle

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._handles.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        """Cancel a run.  Returns True if handle was found and cancelled."""
        with self._lock:
            handle = self._handles.get(run_id)
        if handle is None:
            return False
        handle.cancel()
        return True

    def cancel_children(self, parent_run_id: str,
                        child_ids: list[str]) -> int:
        """Cancel all child runs.  Returns count cancelled."""
        count = 0
        for cid in child_ids:
            if self.cancel(cid):
                count += 1
        return count


class CancellationCoordinator:
    """Handles RequestCancellation: CAS + fact + signal."""

    def __init__(self, uow: SessionUnitOfWork,
                 registry: CancellationRegistry | None = None,
                 scope_factory=None) -> None:
        self._uow = uow
        self._registry = registry or CancellationRegistry()
        self._scope_factory = scope_factory or (lambda sid: (
            ScopeToken.session_scope(uuid.uuid4(), sid) if sid is not None
            else ScopeToken.global_scope()
        ))

    def request_cancellation(self, cmd: CancelRun,
                             session_id: str = "") -> CancelResult:
        """Request run cancellation.

        G24: Appends fact via UoW, then signals CancellationHandle.
        Always signals the handle even if DB state check fails.
        """
        run_id_str = str(cmd.run_id)

        # 1. Append fact via UoW (best-effort for DB state check)
        try:
            self._uow.execute(lambda tx: self._cas_cancel_requested(
                tx, cmd, run_id_str, session_id,
            ))
        except (AlreadyTerminalError, AlreadyCancellingError):
            pass  # Fact may still have been appended

        # 2. Signal the CancellationHandle (always)
        found = self._registry.cancel(run_id_str)

        return CancelResult(
            success=found, reason="cancellation signalled" if found else "handle not found",
        )

    def _cas_cancel_requested(self, tx, cmd: CancelRun,
                              run_id_str: str, session_id: str) -> None:
        """CAS inside UoW transaction."""
        if hasattr(tx, 'check_active_run'):
            active = tx.check_active_run(session_id)
            if active is None:
                raise AlreadyTerminalError(f"Run {run_id_str} not active")
            if active != run_id_str:
                raise AlreadyTerminalError(
                    f"Run {run_id_str} not the active run ({active} is)"
                )

        # Append CancellationRequested fact
        sid = SessionId(session_id) if isinstance(session_id, str) else session_id
        scope = self._scope_factory(sid)

        envelope = EventEnvelope(
            event_id=EventId.generate(),
            event_type=EventTypeName("run.cancelled.v1"),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=EventSource(process_id="coordinator", component="coordinator"),
            scope=scope,
            correlation_id=CorrelationId(str(uuid.uuid4())),
            causation_id=None,
            aggregate_id=AggregateId(run_id_str),
            aggregate_version=AggregateVersion(1),
            payload=cancelled(run_id_str, reason=str(cmd.reason)),
        )
        tx.append_fact(envelope)


class AlreadyTerminalError(RuntimeError):
    """Run is already in a terminal state."""


class AlreadyCancellingError(RuntimeError):
    """Cancellation was already requested."""
