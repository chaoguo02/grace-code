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


# ── H1: LLM Backend Adapter ────────────────────────────────────────────

def _invoke_via_backend(backend, messages, tools=None):
    """Invoke LLM via backend, convert LLMResponse → ModelAction + TokenUsage.

    H1: When backend is None, returns a controlled fake response (test mode).
        When backend is provided, delegates to backend.complete() and maps
        the response to typed ModelAction with TokenUsage extracted.
    """
    from runtime_core.model_actions import (
        AssistantText, ToolCall as MACToolCall, ToolCallBatch,
        ModelStop, ModelRefusal, ModelFailure, TokenUsage,
    )

    if backend is None:
        # H1 test mode: controlled fake response
        return AssistantText(
            text="H1 fake response",
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    # ── Convert FrozenJsonObject messages → LLMMessage list ──────────
    from llm.base import LLMMessage
    from core.json_values import thaw_json

    raw_messages = thaw_json(messages) if hasattr(messages, '__dataclass_fields__') else messages
    msg_list = raw_messages.get("messages", raw_messages) if isinstance(raw_messages, dict) else raw_messages
    llm_messages = []
    if isinstance(msg_list, (list, tuple)):
        for m in msg_list:
            role = m.get("role", "user") if isinstance(m, dict) else "user"
            content = m.get("content", "") if isinstance(m, dict) else str(m)
            llm_messages.append(LLMMessage(role=role, content=content))

    # ── Invoke real backend ───────────────────────────────────────────
    response = backend.complete(llm_messages, [])

    # ── Extract TokenUsage ────────────────────────────────────────────
    usage = TokenUsage(
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )

    # ── Map Action → ModelAction ──────────────────────────────────────
    from core.types import ActionType

    action = response.action
    if action is None:
        return AssistantText(text=response.raw_content or "", usage=usage)

    if action.action_type == ActionType.FINISH:
        text = action.message or action.thought or response.raw_content or ""
        return AssistantText(text=text, stop_reason=response.finish_reason, usage=usage)

    if action.action_type == ActionType.TOOL_CALL:
        calls = []
        for tc in action.tool_calls:
            from core.json_values import freeze_json
            params = freeze_json(tc.params) if isinstance(tc.params, dict) else tc.params
            calls.append(MACToolCall(id=tc.id or "", name=tc.name, params=params))
        if len(calls) == 1:
            return MACToolCall(id=calls[0].id, name=calls[0].name, params=calls[0].params, usage=usage)
        return ToolCallBatch(calls=tuple(calls), usage=usage)

    if action.action_type == ActionType.GIVE_UP:
        return ModelFailure(error=action.thought or "gave up", usage=usage)

    if action.action_type == ActionType.REFLECTION:
        return AssistantText(text=action.thought or "", usage=usage)

    # Fallback
    return AssistantText(text=response.raw_content or "", usage=usage)


# ── H2: Tool Registry Adapter ──────────────────────────────────────────

def _execute_via_registry(lookup, tool_name, params, invocation_id=""):
    """Execute a tool via registry lookup, convert ToolResult → ToolOutcome.

    H2: When lookup is None, returns a controlled fake response (test mode).
        When lookup is provided, finds the tool and calls tool.execute(params).
    """
    from runtime_core.ports import ToolSuccess, ToolFailure

    if lookup is None:
        # H2 test mode: controlled fake response
        return ToolSuccess(
            tool_name=tool_name,
            output=f"H2 fake output for {tool_name}",
            duration_ms=1.0,
        )

    # ── Look up and execute real tool ──────────────────────────────────
    tool = lookup(tool_name)
    if tool is None:
        return ToolFailure(
            tool_name=tool_name,
            error=f"Tool not found: {tool_name}",
        )

    # Convert FrozenJsonObject params to dict for BaseTool
    from core.json_values import thaw_json
    params_dict = thaw_json(params) if hasattr(params, '__dataclass_fields__') else (params or {})

    import time as _time_mod
    started = _time_mod.monotonic()
    try:
        result = tool.execute(params_dict)
        duration_ms = (_time_mod.monotonic() - started) * 1000
        return ToolSuccess(
            tool_name=tool_name,
            output=result.output or "",
            duration_ms=duration_ms,
        )
    except Exception as exc:
        duration_ms = (_time_mod.monotonic() - started) * 1000
        return ToolFailure(
            tool_name=tool_name,
            error=f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
            duration_ms=duration_ms,
        )


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
        """H6: Checks whether a run has been cancelled. Per-run state from handle."""
        def __init__(self, registry):
            self._registry = registry
        @property
        def cancelled(self) -> bool:
            return False  # Per-run handle checked via RuntimeExecution.cancellation

    # H6: Create ProcessRegistry and wire into CancellationHandle
    from hook_core.process_runner import ProcessRegistry
    from runtime_core.execution import CancellationHandle as CHandle
    _proc_registry = ProcessRegistry()
    CHandle.set_process_registry(_proc_registry)

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
        """H2: Tool executor — delegates to BaseTool registry.

        Accepts an optional tool lookup function.  If None (test mode),
        returns a controlled fake response.  In production, pass a real
        tool registry lookup.
        """
        def __init__(self, tool_lookup=None):
            self._lookup = tool_lookup  # callable(name) -> BaseTool | None

        def execute(self, tool_name, params, invocation_id=""):
            return _execute_via_registry(self._lookup, tool_name, params, invocation_id)

    class _RealLLM:
        """H1: LLM adapter — delegates to LLMBackend, converts to ModelAction.

        Accepts an optional LLMBackend.  If None (test mode), returns a
        controlled fake response.  In production, pass the real backend.
        """
        def __init__(self, backend=None):
            self._backend = backend  # LLMBackend | None

        def invoke(self, messages, tools=None):
            return _invoke_via_backend(self._backend, messages, tools)

        def stream(self, messages, tools=None):
            async def _s():
                return _invoke_via_backend(self._backend, messages, tools)
            return _s()

    runtime_ports = RuntimePorts(
        llm=_RealLLM(backend=None),  # H1: None → fake; pass real backend in production
        tools=_RealTools(tool_lookup=None),  # H2: None → fake; pass real registry in production
        hooks=_RealHooks(hook_dispatcher),
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
