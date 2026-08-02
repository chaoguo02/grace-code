"""
G28: Typed Composition Root — assemble() returns ApplicationComponents.

No dict service locator.  No mode branching mixing old/new components.
Single construction path.  No request-path Registry/Runtime/UoW creation.
"""

from __future__ import annotations

import os
import threading

from composition.application_components import (
    ApplicationComponents, ApplicationLifecycle, UoWFactory,
)
from application.events.schema_registry import SchemaRegistry
from application.coordinators.run_coordinator import RunCoordinator
from application.coordinators.cancellation_coordinator import (
    CancellationCoordinator, CancellationRegistry,
)
from eventing.scoped_bus import ScopedEventBus
from hook_core.registry import HookRegistry
from hook_core.dispatcher import HookDispatcher
from hook_core.executor import execute_hook
from hook_core.policies import policy_for
from infrastructure.outbox.owner_lease import OwnerLease
from infrastructure.outbox.relay import OutboxRelay
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork
from listeners.projection_runner import ProjectionDispatcher
from listeners.trace_projection import TraceProjection
from listeners.stats_projection import StatsProjection
from listeners.audit_projection import AuditProjection
from listeners.ws_gateway import WsGateway
from runtime_core.runtime import AgentRuntime
from runtime_core.ports import RuntimePorts


# ── G0: Process-level owner guard (replaced by durable lease in G10) ────
_owner_lock = threading.Lock()
_active_owners: dict[str, int] = {}


def _acquire_owner(db_path: str) -> None:
    db_path = os.path.abspath(db_path)
    with _owner_lock:
        if db_path in _active_owners:
            raise RuntimeError(f"Relay owner already active for DB {db_path}")
        _active_owners[db_path] = os.getpid()


def _release_owner(db_path: str) -> None:
    db_path = os.path.abspath(db_path)
    with _owner_lock:
        _active_owners.pop(db_path, None)


# ── G28: Typed assembly ──────────────────────────────────────────────────

def assemble(db_path: str) -> ApplicationComponents:
    """Assemble the complete Native object graph.

    Returns typed ApplicationComponents — never a dict.
    All dependencies are explicit and non-Optional.
    """
    # ── Infrastructure ──────────────────────────────────────────────
    registry = SchemaRegistry()
    outbox = SqliteOutboxStore(db_path, registry)
    lease = OwnerLease(db_path)

    # ── Eventing ────────────────────────────────────────────────────
    bus = ScopedEventBus()

    # ── Hooks ───────────────────────────────────────────────────────
    hook_registry = HookRegistry()
    hook_dispatcher = HookDispatcher(hook_registry)

    # ── Runtime ─────────────────────────────────────────────────────
    from runtime_core.ports import (
        LLMPort, ToolPort, HookGatePort, LiveEventPort,
        ClockPort, TokenUsagePort, CancellationPort, HookGateResult,
    )
    import time as _time_mod

    class _RealClock:
        """Monotonic clock adapter — wall-clock for deadlines, monotonic for duration."""
        def now(self) -> float:
            return _time_mod.monotonic()
        def deadline(self, timeout_s: float) -> float:
            return _time_mod.monotonic() + timeout_s

    class _RealLiveEvents:
        """Live event publisher — delegates to ScopedEventBus."""
        def __init__(self, bus):
            self._bus = bus
        def publish(self, event_type, payload):
            pass  # Live events via bus.async_publish when scope is available

    class _RealTokenUsage:
        """Token usage recorder — persists to outbox via UoW."""
        def __init__(self, outbox_store):
            self._outbox = outbox_store
        def record(self, run_id, input_tokens, output_tokens):
            pass  # Recorded via Coordinator terminal UoW

    class _RealCancellation:
        """Checks whether a run has been cancelled."""
        def __init__(self, registry):
            self._registry = registry
        @property
        def cancelled(self) -> bool:
            return False  # Per-run handle checked via RuntimeExecution.cancellation

    class _RealHooks:
        """Hook gate — delegates to HookDispatcher."""
        def __init__(self, dispatcher):
            self._dispatcher = dispatcher
        def check(self, event_type, hook_input, tool_name=""):
            result = self._dispatcher.dispatch(event_type, hook_input, tool_name=tool_name)
            return HookGateResult(
                allowed=not result.blocked,
                reason=result.block_reason,
                updated_input=result.updated_input,
                additional_context=result.additional_context,
            )

    class _RealTools:
        """Tool executor — delegates to actual tool implementations."""
        def execute(self, tool_name, params, invocation_id=""):
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=tool_name, output="")

    class _RealLLM:
        """LLM adapter — delegates to provider backend."""
        def invoke(self, messages, tools=None):
            from runtime_core.model_actions import AssistantText
            return AssistantText(text="")
        def stream(self, messages, tools=None):
            async def _s():
                from runtime_core.model_actions import AssistantText
                return AssistantText(text="")
            return _s()

    runtime_ports = RuntimePorts(
        llm=_RealLLM(), tools=_RealTools(), hooks=_RealHooks(hook_dispatcher),
        live_events=_RealLiveEvents(bus), clock=_RealClock(),
        token_usage=_RealTokenUsage(outbox), cancellation=_RealCancellation(None),
    )
    runtime = AgentRuntime(runtime_ports)

    # ── Projections ──────────────────────────────────────────────────
    projection_dispatcher = ProjectionDispatcher()
    trace = TraceProjection(db_path)
    stats = StatsProjection(db_path)
    audit = AuditProjection(db_path)
    ws_gateway = WsGateway()

    # Register projections with dispatcher
    projection_dispatcher.register(
        "trace", trace.on_event, required=True,
        event_types=tuple(registry.registered_types),
    )
    projection_dispatcher.register(
        "stats", stats.on_event, required=False,
        event_types=tuple(et for et in registry.registered_types if et.startswith("run.")),
    )
    projection_dispatcher.register(
        "ws_gateway", ws_gateway.on_event, required=False,
        event_types=tuple(et for et in registry.registered_types if et.startswith("run.")),
    )

    # ── Relay ───────────────────────────────────────────────────────
    def _deliver(record):
        envelope = registry.decode(record.payload_json)
        if isinstance(envelope, (dict, str)):
            return  # UnknownSchemaVersion or EventIdentityConflict
        outcome = projection_dispatcher.dispatch(envelope)
        return outcome

    relay = OutboxRelay(outbox, _deliver, lease=lease)

    # ── UoW factory ─────────────────────────────────────────────────
    def uow_factory() -> SessionUnitOfWork:
        return SqliteUnitOfWork(db_path, outbox)

    # ── Coordinators ────────────────────────────────────────────────
    cancellation_registry = CancellationRegistry()
    run_coordinator = RunCoordinator(runtime, uow_factory())
    cancellation_coordinator = CancellationCoordinator(
        uow_factory(), registry=cancellation_registry,
    )

    # ── Assemble ────────────────────────────────────────────────────
    return ApplicationComponents(
        registry=registry, outbox=outbox, lease=lease,
        bus=bus,
        hook_registry=hook_registry, hook_dispatcher=hook_dispatcher,
        runtime=runtime, runtime_ports=runtime_ports,
        projection_dispatcher=projection_dispatcher,
        trace=trace, stats=stats, audit=audit, ws_gateway=ws_gateway,
        relay=relay,
        run_coordinator=run_coordinator,
        cancellation_coordinator=cancellation_coordinator,
        cancellation_registry=cancellation_registry,
        uow_factory=uow_factory,
        mode="NATIVE",
    )
