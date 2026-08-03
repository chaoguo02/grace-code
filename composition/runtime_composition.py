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

def _invoke_via_backend(backend, messages, tools=None, tool_choice=None):
    """Invoke LLM via backend, convert LLMResponse → ModelAction + TokenUsage.

    H1: When backend is None, returns a controlled fake response (test mode).
    T5: tool_choice forwarded to backend if supported.
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
    from runtime_core.ports import ToolSuccess, ToolFailure, ToolErrorType, ERROR_RETRY_MAP

    if lookup is None:
        # H2 test mode: controlled fake response
        return ToolSuccess(
            tool_name=tool_name,
            output=f"H2 fake output for {tool_name}",
            duration_ms=1.0,
        )

    # ── Look up and execute real tool ──────────────────────────────────
    tool = lookup(tool_name)
    # T17: MCP prefix resolution — try without mcp__ prefix as fallback
    if tool is None and tool_name.startswith("mcp__"):
        _parts = tool_name.split("__", 2)
        if len(_parts) >= 3:
            _fallback = _parts[2]  # mcp__server__tool → tool
            tool = lookup(_fallback)
    if tool is None:
        return ToolFailure(
            tool_name=tool_name,
            error=f"Tool not found: {tool_name}",
            error_type=ToolErrorType.TOOL_NOT_FOUND,
        )

    # Convert FrozenJsonObject params to dict for BaseTool
    from core.json_values import thaw_json
    params_dict = thaw_json(params) if hasattr(params, '__dataclass_fields__') else (params or {})

    # T16: Validate params against tool schema (CC strict mode)
    if hasattr(tool, 'parameters_schema'):
        try:
            from core.schema_validator import SchemaValidator
            schema = tool.parameters_schema
            if callable(schema) and not isinstance(schema, dict):
                schema = schema()
            if schema:
                validator = SchemaValidator(schema)
                result = validator.safe_parse(params_dict)
                if not result.valid:
                    return ToolFailure(
                        tool_name=tool_name,
                        error=f"Schema validation failed: {result.errors}",
                        error_type=ToolErrorType.VALIDATION_ERROR,
                    )
        except ImportError:
            pass  # validator not available → skip

    import time as _time_mod
    import random
    MAX_RETRIES = 3

    for attempt in range(MAX_RETRIES + 1):
        started = _time_mod.monotonic()
        try:
            result = tool.execute(params_dict)
            duration_ms = (_time_mod.monotonic() - started) * 1000
            return ToolSuccess(
                tool_name=tool_name,
                output=result.output or "",
                duration_ms=duration_ms,
                tool_use_id=invocation_id,
            )
        except Exception as exc:
            duration_ms = (_time_mod.monotonic() - started) * 1000
            error_str = f"{type(exc).__name__}: {exc}"
            # T14: Classify error and decide retry
            if "timeout" in error_str.lower():
                err_type = ToolErrorType.TIMEOUT
            elif "permission" in error_str.lower():
                err_type = ToolErrorType.PERMISSION_DENIED
            elif "network" in error_str.lower():
                err_type = ToolErrorType.NETWORK_ERROR
            elif "resource" in error_str.lower():
                err_type = ToolErrorType.RESOURCE_EXHAUSTED
            else:
                err_type = ToolErrorType.EXECUTION_ERROR

            # T14: Retry only automatic errors, with exponential backoff
            retry_mode = ERROR_RETRY_MAP.get(err_type, "never")
            if retry_mode == "automatic" and attempt < MAX_RETRIES:
                delay = (2 ** attempt) * 0.1 * random.uniform(0.5, 1.5)
                _time_mod.sleep(delay)
                continue
            return ToolFailure(
                tool_name=tool_name, error=error_str,
                error_type=err_type, duration_ms=duration_ms,
            )


# ── Phase C: Hook config loader ────────────────────────────────────────

def _load_hooks_from_settings(registry, settings: dict) -> None:
    """Load hook definitions from settings.json format into HookRegistry.

    Supports CC-compatible hook config: { "hooks": { "EventName": [...] } }.
    Each hook entry: { "matcher": "...", "hooks": [ { "type": "command", "command": "...", "args": [...] } ] }.
    """
    import shlex
    from hook_core.process_runner import HookCommand
    from hook_core.matcher import HookMatcher, HookSelector

    hooks_config = settings.get("hooks", {})
    for event_name, matcher_groups in hooks_config.items():
        if not isinstance(matcher_groups, list):
            continue
        for group in matcher_groups:
            matcher_pattern = group.get("matcher", "*")
            try:
                selector = HookSelector.matching(matcher_pattern)
            except Exception:
                selector = HookSelector.all_tools()

            for hook_cfg in group.get("hooks", []):
                hook_type = hook_cfg.get("type", "command")
                if hook_type != "command":
                    continue  # Phase C: command hooks only for now

                name = hook_cfg.get("command", hook_cfg.get("name", ""))
                if not name:
                    continue

                args = hook_cfg.get("args", [])
                if args:
                    argv = tuple([name] + list(args))
                else:
                    # No args → shlex-split the command string
                    try:
                        argv = tuple(shlex.split(name))
                    except ValueError:
                        argv = (name,)

                try:
                    registry.register(
                        name=name,
                        event_type=event_name,
                        handler=HookCommand(argv=argv),
                        selector=selector,
                        priority=hook_cfg.get("priority", 100),
                    )
                except Exception:
                    pass  # Duplicate registration → skip


# ── R1: LiveMessage wrapper (satisfies ScopedMessage protocol) ──────────

class _LiveMessage:
    """Minimal ScopedMessage for live event publishing via EventBus."""
    __slots__ = ('_event_type', '_scope', '_payload')
    def __init__(self, event_type, scope, payload):
        self._event_type = event_type
        self._scope = scope
        self._payload = payload
    @property
    def event_type(self) -> str:
        return self._event_type
    @property
    def scope(self):
        return self._scope
    @property
    def payload(self):
        return self._payload


# ── G10: Native pipeline startup (with durable owner lease) ──────────────────

def start_native_pipeline(db_path: str) -> dict:
    """Start the native event pipeline: OutboxRelay -> Projections.

    G10: Acquires durable OwnerLease + process-level owner guard.
         Only one pipeline per DB path.
         Projection failures propagate to Relay (no false ACK).

    Call this once at server startup when GRACE_RUNTIME_MODE=NATIVE.
    Returns a dict with {'relay', 'bus', 'trace', 'stats', 'ws_gateway', 'shutdown'}
    """
    import logging
    logger = logging.getLogger(__name__)

    # G0: Acquire process-level owner guard (prevent double-start in tests)
    _acquire_owner(db_path)

    from application.events.schema_registry import SchemaRegistry
    from infrastructure.outbox.sqlite_store import SqliteOutboxStore
    from infrastructure.outbox.relay import OutboxRelay
    from infrastructure.outbox.owner_lease import OwnerLease
    from listeners.trace_projection import TraceProjection
    from listeners.stats_projection import StatsProjection
    from listeners.ws_gateway import WsGateway
    from eventing.scoped_bus import ScopedEventBus
    from core.eventing.scope import ScopeToken

    registry = SchemaRegistry()
    outbox = SqliteOutboxStore(db_path, registry)
    bus = ScopedEventBus()
    lease = OwnerLease(db_path)

    # Install outbox + lease DDL
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        SqliteOutboxStore.install(conn)
        OwnerLease.install(conn)
        conn.commit()
    finally:
        conn.close()

    # Projections
    trace = TraceProjection(db_path)
    stats = StatsProjection()
    ws_gw = WsGateway()

    # Subscribe projections to run events (live path - non-durable subscribers)
    global_scope = ScopeToken.global_scope()
    for et in registry.registered_types:
        if et.startswith("run."):
            bus.subscribe(et, trace.on_event, "trace", scope=global_scope)
            bus.subscribe(et, stats.on_event, "stats", scope=global_scope)
            bus.subscribe(et, ws_gw.on_event, "ws_gateway", scope=global_scope)

    # G10: Durable delivery with typed dispatcher
    from listeners.projection_runner import ProjectionDispatcher
    dispatcher = ProjectionDispatcher()
    dispatcher.register("trace", trace.on_event, required=True,
                        event_types=tuple(et for et in registry.registered_types if et.startswith("run.")))
    dispatcher.register("stats", stats.on_event, required=False,
                        event_types=tuple(et for et in registry.registered_types if et.startswith("run.")))
    dispatcher.register("ws_gateway", ws_gw.on_event, required=False,
                        event_types=tuple(et for et in registry.registered_types if et.startswith("run.")))

    def _deliver(record):
        envelope = registry.decode(record.payload_json)
        if isinstance(envelope, (dict, str)):
            return  # UnknownSchemaVersion or conflict
        outcome = dispatcher.dispatch(envelope)
        return outcome

    relay = OutboxRelay(outbox, _deliver, lease=lease)
    relay.acquire_lease()
    relay.start()
    logger.info("Native event pipeline started (relay=%s, db=%s)",
                relay._worker_id, db_path)

    def shutdown():
        try:
            relay.stop()
            logger.info("Native event pipeline stopped (db=%s)", db_path)
        finally:
            _release_owner(db_path)

    return {"relay": relay, "bus": bus, "trace": trace, "stats": stats,
            "ws_gateway": ws_gw, "shutdown": shutdown}


# ── G28: Typed assembly ──────────────────────────────────────────────────

def assemble(db_path: str, *,
             llm_backend=None,        # Phase C: real LLMBackend | None (test mode)
             tool_registry=None,      # Phase C: real tool lookup callable | None
             hook_settings=None,      # Phase C: hook config dict from settings.json | None
             ) -> ApplicationComponents:
    """Assemble the complete Native object graph.

    Phase C: Accepts real backends.  None = controlled fake mode for tests.
    Returns typed ApplicationComponents — never a dict.
    """
    # ── Infrastructure ──────────────────────────────────────────────
    registry = SchemaRegistry()
    outbox = SqliteOutboxStore(db_path, registry)
    lease = OwnerLease(db_path)

    # ── Eventing ────────────────────────────────────────────────────
    bus = ScopedEventBus()

    # ── Hooks ───────────────────────────────────────────────────────
    hook_registry = HookRegistry()
    # Phase C: Load hook configuration from settings
    _perm_rules = {}
    if hook_settings is not None:
        _load_hooks_from_settings(hook_registry, hook_settings)
        _perm_rules = hook_settings.get("permission_rules", {})
    hook_dispatcher = HookDispatcher(hook_registry)

    # ── Runtime ─────────────────────────────────────────────────────
    from runtime_core.ports import (
        LLMPort, ToolPort, HookGatePort, LiveEventPort,
        ClockPort, TokenUsagePort, HookGateResult,
    )
    import time as _time_mod

    class _RealClock:
        """Monotonic clock adapter — wall-clock for deadlines, monotonic for duration."""
        def now(self) -> float:
            return _time_mod.monotonic()
        def deadline(self, timeout_s: float) -> float:
            return _time_mod.monotonic() + timeout_s

    class _RealLiveEvents:
        """R1: Live event publisher — routes to ScopedEventBus when scope is known."""
        def __init__(self, bus):
            self._bus = bus
        def publish(self, event_type, payload, scope=None):
            if scope is not None:
                try:
                    msg = _LiveMessage(
                        event_type=event_type, scope=scope, payload=payload,
                    )
                    self._bus.publish(msg)
                except Exception:
                    pass  # Best-effort: live event failure is non-fatal

    class _RealTokenUsage:
        """Phase C: Token usage recorder — persists token metrics."""
        def __init__(self, outbox_store):
            self._outbox = outbox_store
        def record(self, run_id, input_tokens, output_tokens):
            # Phase C: Record token usage via outbox or direct DB write
            import sqlite3
            try:
                conn = sqlite3.connect(self._outbox._db_path)
                conn.execute(
                    """INSERT OR IGNORE INTO run_token_usage
                       (run_id, input_tokens, output_tokens, recorded_at)
                       VALUES (?, ?, ?, datetime('now'))""",
                    (str(run_id), input_tokens, output_tokens),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass  # Best-effort: token recording failure is non-fatal

    # R2: _RealCancellation deleted — step_loop checks context.cancellation directly.
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
        """H2+T19: Tool executor — delegates to BaseTool registry.

        Accepts callable lookup (backward compat) or ToolRegistryPort (T19).
        T19: When tool_registry is provided, uses its resolve() method.
        """
        def __init__(self, tool_lookup=None, tool_registry=None):
            self._lookup = tool_lookup
            self._registry = tool_registry  # T19: ToolRegistryPort | None

        def execute(self, tool_name, params, invocation_id=""):
            # M4: route through PolicyAwareToolRegistry when present so phase
            # policy (allowed_write_paths / allowed & denied tools) is enforced
            # on the native path.  Plain ToolRegistry / callable lookup keeps
            # the direct _execute_via_registry path.
            _registry = self._registry
            if _registry is not None:
                from core.policy_registry import PolicyAwareToolRegistry
                if isinstance(_registry, PolicyAwareToolRegistry):
                    from core.json_values import thaw_json
                    params_dict = (
                        thaw_json(params)
                        if hasattr(params, '__dataclass_fields__')
                        else (params or {})
                    )
                    tool_result = _registry.execute_tool(
                        tool_name, params_dict, invocation_id=invocation_id,
                    )
                    from runtime_core.ports import (
                        ToolSuccess, ToolFailure, ToolErrorType,
                    )
                    if getattr(tool_result, "success", False):
                        return ToolSuccess(
                            tool_name=tool_name,
                            output=getattr(tool_result, "output", "") or "",
                            duration_ms=getattr(tool_result, "duration_ms", 0.0),
                            tool_use_id=invocation_id,
                        )
                    return ToolFailure(
                        tool_name=tool_name,
                        error=(
                            getattr(tool_result, "error", "")
                            or getattr(tool_result, "output", "")
                            or "tool call blocked by policy"
                        ),
                        error_type=ToolErrorType.EXECUTION_ERROR,
                    )
            _lookup = self._lookup
            if _lookup is None and self._registry is not None:
                _lookup = self._registry
            # assemble() passes tool_registry as tool_lookup.  When it is a
            # ToolRegistry (not a bare callable), build a name→tool resolver.
            # ToolRegistry exposes resolve_name() (not the ToolRegistryPort
            # resolve() that ToolRegistryAdapter used to bridge).
            if _lookup is not None and not callable(_lookup):
                _resolve = getattr(_lookup, "resolve", None)
                if not callable(_resolve) and hasattr(_lookup, "resolve_name"):
                    _reg = _lookup
                    _tools_map = getattr(_reg, "_tools", {})
                    _resolve = lambda _name, _r=_reg, _m=_tools_map: _m.get(
                        _r.resolve_name(_name) or _name,
                    )
                if callable(_resolve):
                    _lookup = _resolve
            return _execute_via_registry(_lookup, tool_name, params, invocation_id)

    class _RealLLM:
        """H1: LLM adapter — delegates to LLMBackend, converts to ModelAction.

        Accepts an optional LLMBackend.  If None (test mode), returns a
        controlled fake response.  In production, pass the real backend.
        """
        def __init__(self, backend=None):
            self._backend = backend  # LLMBackend | None

        def invoke(self, messages, tools=None, tool_choice=None):
            return _invoke_via_backend(self._backend, messages, tools, tool_choice)

        def stream(self, messages, tools=None, tool_choice=None):
            async def _s():
                return _invoke_via_backend(self._backend, messages, tools, tool_choice)
            return _s()

    runtime_ports = RuntimePorts(
        llm=_RealLLM(backend=llm_backend),  # Phase C: real backend or None (test)
        tools=_RealTools(tool_lookup=tool_registry, tool_registry=tool_registry),  # T19: dual path
        hooks=_RealHooks(hook_dispatcher),
        live_events=_RealLiveEvents(bus), clock=_RealClock(),
        token_usage=_RealTokenUsage(outbox),
    )
    runtime = AgentRuntime(runtime_ports)
    # T12: Permission rules stored for T13 wiring
    # (frozen slots dataclass cannot have extra attributes; rules passed via
    #  hook_settings dict to _RealHooks in T13)
    # Note: ToolScheduler population was previously attempted via
    # object.__setattr__(runtime_ports, '_scheduler', ...) but RuntimePorts is a
    # frozen+slots dataclass, so that call always raised AttributeError.  StepLoop
    # constructs its own empty scheduler (all-serial fallback), so the code was
    # dead + broken.  Concurrency metadata wiring is deferred (see M9).

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
