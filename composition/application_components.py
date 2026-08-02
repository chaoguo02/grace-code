"""
G28: ApplicationComponents — typed object graph, no dict service locator.

All components assembled once at startup.  No request-path construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from application.coordinators.run_coordinator import RunCoordinator
from application.coordinators.cancellation_coordinator import CancellationCoordinator, CancellationRegistry
from application.events.schema_registry import SchemaRegistry
from application.transactions.unit_of_work import SessionUnitOfWork
from eventing.scoped_bus import ScopedEventBus
from hook_core.registry import HookRegistry
from hook_core.dispatcher import HookDispatcher
from infrastructure.outbox.owner_lease import OwnerLease
from infrastructure.outbox.relay import OutboxRelay
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from listeners.projection_runner import ProjectionDispatcher
from listeners.trace_projection import TraceProjection
from listeners.stats_projection import StatsProjection
from listeners.audit_projection import AuditProjection
from listeners.ws_gateway import WsGateway
from runtime_core.runtime import AgentRuntime
from runtime_core.ports import RuntimePorts


# ── UoW factory type ──────────────────────────────────────────────────────
UoWFactory = Callable[[], SessionUnitOfWork]


@dataclass(frozen=True, slots=True)
class ApplicationComponents:
    """Typed object graph — all components assembled once at startup.

    G28: No dict service locator.  Every component is a typed field.
    """

    # Infrastructure
    registry: SchemaRegistry
    outbox: SqliteOutboxStore
    lease: OwnerLease

    # Eventing
    bus: ScopedEventBus

    # Hooks
    hook_registry: HookRegistry
    hook_dispatcher: HookDispatcher

    # Runtime
    runtime: AgentRuntime
    runtime_ports: RuntimePorts

    # Projections
    projection_dispatcher: ProjectionDispatcher
    trace: TraceProjection
    stats: StatsProjection
    audit: AuditProjection
    ws_gateway: WsGateway

    # Relay
    relay: OutboxRelay

    # Coordinators
    run_coordinator: RunCoordinator
    cancellation_coordinator: CancellationCoordinator
    cancellation_registry: CancellationRegistry

    # UoW factory (per-request)
    uow_factory: UoWFactory = field(default=lambda: (_ for _ in ()).throw(
        RuntimeError("UoW factory not configured")))

    # Lifecycle
    mode: str = "NATIVE"


class ApplicationLifecycle:
    """Start/stop ordering for the application."""

    def __init__(self, components: ApplicationComponents) -> None:
        self._comp = components

    def start(self) -> None:
        """Start background services (relay)."""
        self._comp.relay.acquire_lease()
        self._comp.relay.start()

    def stop(self) -> None:
        """Graceful shutdown."""
        self._comp.relay.stop()
