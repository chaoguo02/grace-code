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
#
#  _invoke_via_backend DELETED (Condition 2).  All providers now use
#  NativeBackend / OpenAINativeBackend via NativeBackendAdapter.
#  Test mode uses _FakeNativeLLM below.


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

    # Install outbox + lease DDL (first-run safe)
    import sqlite3
    from pathlib import Path as _Path
    _Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        SqliteOutboxStore.install(conn)
        SqliteOutboxStore.migrate_add_columns(conn)
        OwnerLease.install(conn)
        from listeners.projection_state import ProjectionStateStore
        ProjectionStateStore.install(conn)
        from listeners.stats_projection import StatsProjection
        from listeners.audit_projection import AuditProjection
        StatsProjection.install(conn)
        AuditProjection.install(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS run_token_usage (
                run_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL
            )
        """)
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

    # ── DDL installation (first-run safe) ─────────────────────────
    # Ensure .grace/ directory exists before sqlite3.connect()
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path
    _Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    _ddl_conn = _sqlite3.connect(db_path)
    try:
        # Outbox + relay lease + projection infrastructure
        SqliteOutboxStore.install(_ddl_conn)
        SqliteOutboxStore.migrate_add_columns(_ddl_conn)
        OwnerLease.install(_ddl_conn)
        from listeners.projection_state import ProjectionStateStore
        ProjectionStateStore.install(_ddl_conn)
        # Projection tables (stats, audit)
        from listeners.stats_projection import StatsProjection
        from listeners.audit_projection import AuditProjection
        StatsProjection.install(_ddl_conn)
        AuditProjection.install(_ddl_conn)
        # Token usage recording
        _ddl_conn.execute("""
            CREATE TABLE IF NOT EXISTS run_token_usage (
                run_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL
            )
        """)
        _ddl_conn.commit()
    finally:
        _ddl_conn.close()

    # ── Eventing ────────────────────────────────────────────────────
    bus = ScopedEventBus()

    # ── Hooks ───────────────────────────────────────────────────────
    hook_registry = HookRegistry()
    # Phase C: Load hook configuration from settings
    _perm_rules = {}
    _perm_mode = ""
    if hook_settings is not None:
        _load_hooks_from_settings(hook_registry, hook_settings)
        _perm_rules = hook_settings.get("permission_rules", {})
        _perm_mode = str(hook_settings.get("permission_mode", "") or "")
    hook_dispatcher = HookDispatcher(hook_registry)

    # ── R1: wire permission_rules into a PermissionPipeline for Native path ──
    # 对齐 CC：permission_rules（settings.json 的 "Write": "deny" 等）必须在
    # Native StepLoop 生效。PermissionPipeline 同时承载 deny/ask/allow 规则 +
    # 权限模式 + RiskLevel/TrustAccumulator（Phase 2 已接线）。
    #
    # P0-1: PermissionPipeline 必须始终存在——即使 settings 没有显式规则。
    # 原因：(a) Layer 1 validateInput（工具自身黑名单 + 保护路径）是安全底线，
    # 不依赖任何规则；(b) 没有 pipeline 时 _RealHooks 会跳过整个权限 gate，
    # 连 deny 规则都不生效。空规则 → 退化为 builtin 安全默认（acceptEdits）。
    #
    # P0-2: approval_mode=AUTO —— native 是 headless 无交互，CC 语义下
    # headless 的 "ask" = auto-deny。AUTO 让未分类工具直接放行（等价 legacy
    # web 的 approval_mode="auto"），而 ask 规则因 force_interactive 会跳过
    # AUTO 分支落到 INTERACTIVE DENY —— 这样 _RealHooks 拦截 INTERACTIVE
    # DENY 才是安全的（不会误伤普通工具），ask 规则在 native 下变成硬拒绝
    # 而非静默执行。
    from hitl.permission_rule import PermissionRule
    from hitl.pipeline import PermissionPipeline, ToolApprovalMode

    _perm_rules_list = []
    if _perm_rules:
        for _pat, _tier in _perm_rules.items():
            try:
                _perm_rules_list.append(
                    PermissionRule.parse(str(_pat), tier=str(_tier)),
                )
            except ValueError:
                continue  # 跳过非法规则语法
    if not _perm_rules_list:
        # P0-2: native 用 acceptEdits 规则集（不含 Write/Edit ASK）。
        # legacy 的 _builtin_defaults() 把 Write/Edit 标 ASK → native headless
        # 下 ask=auto-deny → 主 agent 无法写文件。builtin_native_rules()
        # 只保留 deny(blocked) + allow(readonly) + ask(危险命令)。
        # Write/Edit 不在规则集 → acceptEdits 模式自动批准（CC coding agent）。
        from hitl.settings_loader import builtin_native_rules
        _perm_rules_list = builtin_native_rules()
    # P0-3: bind project_root so Layer 5 path sandbox resolves relative to the
    # real repo (db lives at <repo>/.grace/grace.db → repo root is two levels up).
    import os as _os
    _repo_root = _os.path.abspath(
        _os.path.dirname(_os.path.dirname(db_path))
        if _os.path.basename(_os.path.dirname(db_path)) == ".grace"
        else _os.path.dirname(db_path)
    )
    _permission_pipeline = PermissionPipeline(
        rules=_perm_rules_list,
        approval_mode=ToolApprovalMode.AUTO,
        project_root=_repo_root,
    )
    # P0-2: native 主 agent（build/orchestrator）是 coding agent → acceptEdits。
    # 显式 permission_mode（如 plan）优先；空则 acceptEdits 让 Write/Edit 自动批准。
    _effective_perm_mode = _perm_mode or "acceptEdits"
    _permission_pipeline.set_permission_mode(_effective_perm_mode)

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
        """Hook gate — HookDispatcher + PermissionPipeline (CC-aligned order).

        CC tool-call order (runToolUse → streamedCheckPermissionsAndCallTool):
          1. PreToolUse hooks     — user scripts, HIGHEST priority, bypass-immune
          2. canUseTool permission — deny/ask/allow rules + mode + callback
          3. tool.call()
          4. PostToolUse hooks

        Each layer owns a distinct concern:
          - HookDispatcher  → "should this run?"  (user policy, safety scripts,
                               absolute floor — a hook deny applies even in
                               bypassPermissions, and a hook allow supersedes
                               permission rules)
          - PermissionPipeline → "may this run?"  (permission rules + mode +
                               per-session ask interaction)

        Order here matches CC: hooks run FIRST.  A blocking hook short-circuits
        before permission (so user scripts override rules).  If hooks pass, the
        permission gate decides.  Permission ALLOW that skips hooks is avoided —
        hooks always see every tool call (CC recommendation for must-run checks).

        P0-3: per-session PermissionPipeline copy + web_confirm_callback registry.
        """
        def __init__(self, dispatcher, permission_pipeline=None,
                     tool_registry=None):
            self._dispatcher = dispatcher
            self._base_permission = permission_pipeline
            self._permission = permission_pipeline
            self._tool_registry = tool_registry
            self._session_confirm_callbacks: dict[str, Callable] = {}
            self._session_pipelines: dict[str, object] = {}
            import threading as _threading
            self._session_lock = _threading.Lock()

        def register_session_confirm(self, session_id: str, callback) -> None:
            """Register a per-session web_confirm_callback (CC control_response).

            Called by the web layer (ChatPipeline) before a run starts, so the
            pipeline's Layer 6 ask rule can block on the user's decision instead
            of silently executing the tool.
            """
            with self._session_lock:
                self._session_confirm_callbacks[session_id] = callback
                self._session_pipelines.pop(session_id, None)  # invalidate copy

        def _pipeline_for(self, session_id: str):
            """Return the per-session PermissionPipeline (base + confirm cb).

            Uses ``PermissionPipeline.scoped()`` (deep-copied rule lists +
            isolated denial counters) rather than raw copy.copy so per-session
            state never leaks across sessions.  The confirm callback is applied
            via configure_session on the per-session clone only.
            """
            if self._base_permission is None:
                return None
            if not session_id:
                return self._base_permission
            with self._session_lock:
                cached = self._session_pipelines.get(session_id)
                if cached is not None:
                    return cached
                from hitl.pipeline import PermissionSessionConfig
                clone = self._base_permission.scoped(self._base_permission._project_root or ".")
                cb = self._session_confirm_callbacks.get(session_id)
                if cb is not None:
                    clone.configure_session(PermissionSessionConfig(
                        session_id=session_id,
                        web_confirm_callback=cb,
                    ))
                self._session_pipelines[session_id] = clone
                return clone

        def check(self, event_type, hook_input, tool_name=""):
            # CC-aligned: hooks run FIRST (bypass-immune), then permission.
            # HookDispatcher owns "should this run?"; PermissionPipeline owns
            # "may this run?".  A blocking hook short-circuits before permission
            # so user scripts override rules (CC documented behavior).
            hook_result = self._dispatcher.dispatch(
                event_type, hook_input, tool_name=tool_name,
            )
            if event_type == "PreToolUse":
                if hook_result.blocked:
                    # Hook deny — absolute floor, even in bypassPermissions.
                    return HookGateResult(
                        allowed=False,
                        reason=hook_result.block_reason or "blocked by hook",
                        updated_input=hook_result.updated_input,
                        additional_context=hook_result.additional_context,
                    )
                # CC-aligned: a hook ALLOW supersedes permission rules —
                # canUseTool is skipped for this tool call.
                if getattr(hook_result, "permission", None) is not None:
                    from hitl.pipeline import PermissionDecision
                    if hook_result.permission is PermissionDecision.ALLOW:
                        return HookGateResult(
                            allowed=True,
                            reason="allowed by PreToolUse hook",
                            updated_input=hook_result.updated_input,
                            additional_context=hook_result.additional_context,
                        )
                # Hooks passed (no explicit decision) — permission gate decides.
                if self._permission is not None:
                    denied = self._permission_gate(tool_name, hook_input)
                    if denied is not None:
                        return denied
            return HookGateResult(
                allowed=not hook_result.blocked,
                reason=hook_result.block_reason,
                updated_input=hook_result.updated_input,
                additional_context=hook_result.additional_context,
            )

        def _permission_gate(self, tool_name, hook_input):
            """PermissionPipeline 评估；明确 DENY 返回阻止结果，否则 None（继续）。

            P0-2/P0-3: 使用 per-session pipeline（带该 session 的
            web_confirm_callback）。行为：
              - 有回调的 ask 规则 → pipeline 阻塞等用户决策，返回 ALLOW 或
                DENY(非 INTERACTIVE)，本函数如实执行/拦截。
              - 无回调（headless）的 ask 规则 → pipeline 返回 DENY(INTERACTIVE)
                = CC 语义 "headless 下 ask = auto-deny" → 拦截（fail-closed）。
              - 未分类工具 → approval_mode=AUTO 直接 ALLOW，不受影响。
            所以 INTERACTIVE 层 DENY 现在必须拦截，不再放行。
            """
            from hitl.pipeline import PermissionDecision
            session_id = str(getattr(hook_input, "session_id", "") or "")
            tool = self._resolve_tool(tool_name)
            if tool is None:
                return None  # 无法 resolve 工具 → 跳过 permission
            params = getattr(hook_input, "tool_input", {}) or {}
            from core.json_values import thaw_json
            params_dict = (
                thaw_json(params) if hasattr(params, '__dataclass_fields__')
                else (params or {})
            )
            pipeline = self._pipeline_for(session_id)
            if pipeline is None:
                return None
            perm_result = pipeline.check(tool, params_dict)
            if perm_result.decision is PermissionDecision.DENY:
                return HookGateResult(
                    allowed=False,
                    reason=perm_result.reason or "denied by permission rule",
                )
            return None

        def _resolve_tool(self, tool_name):
            tr = self._tool_registry
            if tr is None:
                return None
            if callable(tr):
                return tr(tool_name)
            if hasattr(tr, 'resolve'):
                return tr.resolve(tool_name)
            return None

    class _RealTools:
        """H2+T19+T21: Tool executor + dynamic registry (R2).

        Accepts callable lookup (backward compat) or ToolRegistryPort (T19).
        T19: When tool_registry is provided, uses its resolve() method.
        R2: Implements ToolRegistryPort dynamic interface (register/unregister/
        resolve/list_names/metadata_for).  Dynamically-registered tools (e.g.
        MCP servers discovered at runtime) execute BEFORE the static lookup.
        """
        def __init__(self, tool_lookup=None, tool_registry=None):
            self._lookup = tool_lookup
            self._registry = tool_registry  # T19: ToolRegistryPort | None
            self._dynamic: dict[str, object] = {}  # R2: 动态注册的工具（name → BaseTool）

        # ── R2: ToolRegistryPort dynamic interface ────────────────────────

        def register(self, tool) -> None:
            """Register a tool at runtime (incl. its aliases)."""
            name = getattr(tool, "name", "") or ""
            if not name:
                return
            self._dynamic[name] = tool
            for alias in getattr(tool, "aliases", ()) or ():
                self._dynamic[alias] = tool

        def unregister(self, name: str) -> None:
            """Remove a dynamically-registered tool."""
            self._dynamic.pop(name, None)

        def resolve(self, name: str) -> object | None:
            """Resolve a tool: dynamic table → mcp__ alias → static lookup."""
            if name in self._dynamic:
                return self._dynamic[name]
            if name.startswith("mcp__"):
                parts = name.split("__", 2)
                if len(parts) >= 3 and parts[2] in self._dynamic:
                    return self._dynamic[parts[2]]
            if self._lookup is not None:
                if callable(self._lookup):
                    return self._lookup(name)
                res = getattr(self._lookup, "resolve", None)
                if callable(res):
                    return res(name)
            if self._registry is not None:
                res = getattr(self._registry, "resolve", None)
                if callable(res):
                    return res(name)
            return None

        def list_names(self) -> list[str]:
            """List dynamically-registered tool names."""
            return list(self._dynamic.keys())

        def metadata_for(self, name: str):
            """Bridge a tool's core metadata to runtime_core ToolMetadata."""
            from runtime_core.tool_scheduler import ToolMetadata
            tool = self.resolve(name)
            if tool is None:
                return None
            return ToolMetadata.from_base_tool(tool)

        def _execute_dynamic(self, tool, tool_name, params, invocation_id=""):
            """Execute a dynamically-registered BaseTool directly."""
            from core.json_values import thaw_json
            from runtime_core.ports import (
                ToolSuccess, ToolFailure, ToolErrorType,
            )
            params_dict = (
                thaw_json(params) if hasattr(params, '__dataclass_fields__')
                else (params or {})
            )
            try:
                result = tool.execute(params_dict)
                return ToolSuccess(
                    tool_name=tool_name,
                    output=getattr(result, "output", "") or "",
                    duration_ms=getattr(result, "duration_ms", 0.0),
                    tool_use_id=invocation_id,
                )
            except Exception as exc:
                return ToolFailure(
                    tool_name=tool_name,
                    error=f"{type(exc).__name__}: {exc}",
                    error_type=ToolErrorType.EXECUTION_ERROR,
                )

        def execute(self, tool_name, params, invocation_id=""):
            # ── R2: dynamically-registered tools execute first (incl. mcp__ alias) ──
            dyn = self._dynamic
            resolved = tool_name
            if tool_name not in dyn and tool_name.startswith("mcp__"):
                parts = tool_name.split("__", 2)
                if len(parts) >= 3 and parts[2] in dyn:
                    resolved = parts[2]
            if resolved in dyn:
                return self._execute_dynamic(dyn[resolved], tool_name, params, invocation_id)

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

    # ── Test mode fake (when llm_backend is None) ──────────────────
    from runtime_core.native_llm_adapter import NativeBackendAdapter

    class _FakeNativeLLM:
        """H1 test mode: returns controlled fake response through native interface."""
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            from runtime_core.model_actions import AssistantText, TokenUsage
            return AssistantText(
                text="H1 fake response", stop_reason="end_turn",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )

    # ── Condition 2: All providers → Native pipeline ──────────────────
    if llm_backend is not None:
        from llm.anthropic_backend import AnthropicBackend
        from llm.openai_backend import OpenAIBackend
        if isinstance(llm_backend, AnthropicBackend):
            from runtime_core.native_backend import NativeBackend
            _native = NativeBackend.from_backend(llm_backend)
        elif isinstance(llm_backend, OpenAIBackend):
            from runtime_core.openai_native_backend import OpenAINativeBackend
            _native = OpenAINativeBackend.from_backend(llm_backend)
        else:
            _native = _FakeNativeLLM()
        _llm_adapter = NativeBackendAdapter(_native)
    else:
        _llm_adapter = NativeBackendAdapter(_FakeNativeLLM())  # test mode

    runtime_ports = RuntimePorts(
        llm=_llm_adapter,
        tools=_RealTools(tool_lookup=tool_registry, tool_registry=tool_registry),  # T19: dual path
        hooks=_RealHooks(
            hook_dispatcher,
            permission_pipeline=_permission_pipeline,
            tool_registry=tool_registry,
        ),  # R1: permission_rules gate on Native path
        live_events=_RealLiveEvents(bus), clock=_RealClock(),
        token_usage=_RealTokenUsage(outbox),
    )

    # ── Phase 7: ToolScheduler — parallel tool execution ─────────────────
    # Agent tool is marked concurrency_safe so multiple Agent calls in one
    # ToolCallBatch can execute in parallel (CC parallel fan-out).
    from runtime_core.tool_scheduler import ToolScheduler, ToolMetadata
    _scheduler = ToolScheduler()
    # Pre-register Agent metadata so scheduler knows Agent calls are parallel-safe
    _scheduler.register(ToolMetadata(
        name="Agent", read_only=False, concurrency_safe=True,
    ))
    runtime = AgentRuntime(runtime_ports, scheduler=_scheduler)

    # ── Phase 2: Register NativeAgentTool ──────────────────────────────
    # Agent definitions are loaded from .grace/agents/ (project + user + built-in).
    # The NativeAgentTool routes CC Agent tool calls through the native path.
    try:
        from agent.session.agent_definition import load_agent_definitions
        _project_dir = os.path.dirname(db_path) if os.path.dirname(db_path) else "."
        _agent_defs = load_agent_definitions(project_dir=_project_dir)
        from runtime_core.native_agent_tool import NativeAgentTool
        _native_backend = (
            _native if (llm_backend is not None and not isinstance(_native, _FakeNativeLLM))
            else None
        )
        _native_agent = NativeAgentTool(
            definition_registry=_agent_defs,
            parent_ports=runtime_ports,
            parent_backend=_native_backend,
            project_dir=_project_dir,  # Phase 3: GRACE.md injection
        )
        runtime_ports.tools.register(_native_agent)

        # ── Phase 2 fix: Agent tool schema → NativeBackend ──────────────
        if _native_backend is not None:
            from runtime_core.native_backend import NativeToolSchema
            _agent_schema = NativeToolSchema(
                name="Agent",
                description="Spawn a named subagent (explore, general, etc.) to delegate work in a fresh isolated context.",
                input_schema=_native_agent.parameters_schema,
            )
            _existing = list(_native_backend._tool_schemas)
            _existing.append(_agent_schema)
            object.__setattr__(_native_backend, '_tool_schemas', tuple(_existing))
            _native_backend._cached_api_tools.append(_agent_schema.to_api_dict())
    except Exception:
        pass  # Agent tool registration is best-effort; not fatal for test mode

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
