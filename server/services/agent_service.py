"""
Agent service — wraps SessionRuntime lifecycle for web access.

Singleton service initialised once by ``server/main.py::create_app()``.
All web API calls delegate to this service.

Initialization flow (adapted from entry/chat.py ChatSession):
    1. load_config() → AppConfig
    2. create_backend_from_config(config) → LLMBackend
    3. build_registry(config, repo_path) → ToolRegistry
    4. AgentRegistryV2(project_dir=repo_path)
    5. SessionStore(default_session_db_path(repo_path))
    6. SessionRuntime(store, backend, base_registry, agent_registry, ...)
    7. EventBus — wired as event_callback for WebSocket streaming

Usage:
    service = AgentService(repo_path=".")
    result = await service.chat(session_id="abc", prompt="Fix the bug")
    service.cancel_session(session_id="abc", detail="User cancelled")
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from agent.task import RunResult, TaskIntent
from server.events import WsStatus

logger = logging.getLogger(__name__)


def _load_json_file(path: Path, rules: list, label: str) -> None:
    """Load permission rules from a settings.json file.

    Appends parsed rules from *path* to *rules* in-place.
    Silently skips if the file doesn't exist or is malformed.
    """
    if not path.is_file():
        return
    try:
        import json
        from hitl.permission_rule import PermissionRule, PermissionRuleTier
        data = json.loads(path.read_text(encoding="utf-8"))
        perms = data.get("permissions", {})
        count = 0
        for raw in perms.get("deny", []):
            try:
                rules.append(PermissionRule.parse(str(raw), tier=PermissionRuleTier.DENY, source=label))
                count += 1
            except ValueError:
                continue
        for raw in perms.get("ask", []):
            try:
                rules.append(PermissionRule.parse(str(raw), tier=PermissionRuleTier.ASK, source=label))
                count += 1
            except ValueError:
                continue
        for raw in perms.get("allow", []):
            try:
                rules.append(PermissionRule.parse(str(raw), tier=PermissionRuleTier.ALLOW, source=label))
                count += 1
            except ValueError:
                continue
        if count:
            logger.info("Loaded %d rules from %s", count, path)
    except Exception:
        logger.debug("Failed to load rules from %s", path, exc_info=True)


class AgentService:
    """Web-facing SessionRuntime wrapper.

    Attributes:
        repo_path: Absolute path to the repository being worked on.
        session_service: Query service for session/message/event reads.
    """

    def __init__(
        self,
        repo_path: str,
        config_path: str | None = None,
        *,
        event_bus: Any = None,
        model: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.repo_path = str(Path(repo_path).expanduser().resolve())
        self._config_path = config_path
        self._event_bus = event_bus
        self._root_session = None
        self._root_session_id: str | None = None
        self._runtime: Any | None = None
        self._registry: Any | None = None
        self._memory_store: Any | None = None
        self._external_store: Any | None = None
        self._memory_indexer: Any | None = None
        self._memory_retriever: Any | None = None
        self._memory_recall_service: Any | None = None
        self._memory_context: Any | None = None
        self._hook_dispatcher: Any | None = None
        self._mcp_integration: Any | None = None
        self._memory_maintenance_stop: Any = None
        self._memory_maintenance_thread: Any = None
        self._memory_stop_event: Any | None = None
        self._memory_maintenance_task: Any | None = None
        self._observe_retries: bool = os.environ.get("FORGE_OBSERVE_RETRIES") == "1"
        """P2-18 runtime switch: when True, LLM retry metrics are logged."""

        # ── 1. Load config ──
        from config.schema import load_config, AppConfig

        self._config: AppConfig = load_config(config_path)
        self._apply_cli_overrides(model, provider, api_key, base_url, max_steps)
        from core.provider_governor import ProviderGovernor
        self._provider_governor = ProviderGovernor(
            self._config.resource_governance,
        )
        if self._event_bus is not None:
            self._event_bus._queue_max_size = max(
                0,
                int(self._config.resource_governance.event.queue_max_size),
            )

        # Save effective LLM config snapshot — preserves CLI overrides and
        # dynamic updates across model switches (P0-3).
        self._effective_llm_config = {
            "provider": self._config.llm.provider,
            "api_key": self._config.llm.api_key or None,
            "base_url": self._config.llm.base_url or None,
            "max_tokens": self._config.llm.max_tokens,
            "timeout_seconds": self._config.llm.timeout_seconds,
        }

        # ── 2. Create LLM backend ──
        from llm.router import create_backend_from_config

        # Default backend — used as fallback when no per-session backend
        # has been registered. Per-session overrides are created by
        # _run_and_notify() when a model switch is pending.
        self._backend = create_backend_from_config({
            "provider": self._config.llm.provider,
            "model": self._config.llm.model,
            "api_key": self._config.llm.api_key or None,
            "base_url": self._config.llm.base_url or None,
            "max_tokens": self._config.llm.max_tokens,
            "timeout_seconds": self._config.llm.timeout_seconds,
        })
        from llm.provider_capacity import attach_provider_governor

        attach_provider_governor(
            self._backend,
            self._provider_governor,
            provider_name=self._config.llm.provider,
        )

        # ── 3. Build ToolRegistry ──
        from entry.bootstrap.registry_factory import build_registry

        # CC-aligned: approval_mode="auto" so unclassified tools pass
        # without prompting.  ASK-rule tools (Write/Edit/dangerous Bash)
        # are force_interactive=True and always show the approval card.
        # The web_confirm_callback blocks the agent thread on
        # threading.Event — the exact equivalent of CC's stdin-blocking
        # control_request / control_response protocol.
        # ── Init MCP registry (connect servers, discover tools) ──
        # ── 4. Session store + StorageBackend ──
        from agent.session import default_session_db_path
        from agent.session.session_store import SessionStore
        from app.storage.sqlite import SqliteStorageBackend

        db_path = default_session_db_path(self.repo_path)
        from core.state_paths import migrate_legacy_session_db

        migrate_legacy_session_db(self.repo_path, db_path)
        self._store = SessionStore(db_path)
        interrupted_delegations = self._store.reconcile_interrupted_delegations()
        if interrupted_delegations:
            logger.warning(
                "Marked %d interrupted delegation run(s) recovery_required",
                len(interrupted_delegations),
            )
        self._storage: SqliteStorageBackend = SqliteStorageBackend(db_path)
        recovered = self._storage.recover_orphaned_runs()
        if recovered:
            logger.warning(
                "Recovered %d orphaned run(s) left by a previous process",
                len(recovered),
            )

        # ── SessionService ─────────────────────────────────────────────
        from server.services.session_service import SessionService
        self.session_service = SessionService(self._storage)

        # ── StatsService + StatsRecorder ───────────────────────────────
        from server.services.stats_service import StatsService
        from server.services.stats_recorder import StatsRecorder

        self._stats_service = StatsService(self._storage)
        self._stats_recorder = StatsRecorder(self._stats_service)
        from server.services.evaluation_service import EvaluationService
        self._evaluation_service = EvaluationService(self.repo_path)
        if self._event_bus is not None:
            from server.services.trace_cache import InMemoryTraceCache
            self._trace_cache = InMemoryTraceCache()
            self._event_bus.recorder = self._stats_recorder
            self._event_bus.trace_store = self._storage
            self._event_bus.trace_cache = self._trace_cache
        else:
            from server.services.trace_cache import InMemoryTraceCache
            self._trace_cache = InMemoryTraceCache()

        # ── Memory system ─────────────────────────────────────────────────
        # Mirrors entry/bootstrap/memory_bootstrap.py in web mode.
        # TwoTierMemoryStore (SQLite + file) + semantic search + retriever.
        from memory.store import TwoTierMemoryStore
        from memory.context import MemoryContext

        self._memory_store = None
        self._external_store = None
        self._memory_context = None
        self._memory_indexer = None

        # ── MemoryStore (TwoTier: project + global scopes) ──
        try:
            self._memory_store = TwoTierMemoryStore(
                repo_path=self.repo_path,
                db_path=db_path,
                memory_dir=getattr(self._config.memory, 'directory', None) or None,
                max_index_lines=getattr(self._config.memory, 'max_index_lines', 200),
            )
            logger.info("TwoTierMemoryStore initialized")
        except Exception:
            logger.warning("Failed to initialize TwoTierMemoryStore — falling back to MemoryStore", exc_info=True)
            try:
                from memory.store import MemoryStore
                self._memory_store = MemoryStore(repo_path=self.repo_path, db_path=db_path)
            except Exception:
                logger.warning("MemoryStore fallback also failed", exc_info=True)

        # ── Semantic search stack (optional: needs fastembed) ──
        try:
            importlib.import_module("fastembed")
            from memory.external_store import ExternalMemoryStore
            from memory.indexer import MemoryIndexer
            from memory.retriever import ProactiveRetriever
            self._external_store = ExternalMemoryStore()
            self._memory_indexer = MemoryIndexer(self._external_store)
            self._memory_retriever = ProactiveRetriever(
                self._external_store, max_chunks=5, max_tokens=2000,
            )
            logger.info("Semantic memory search enabled (fastembed)")
        except ImportError:
            logger.info("fastembed not installed — semantic memory search disabled")
            self._external_store = None
            self._memory_indexer = None
            self._memory_retriever = None
        except Exception:
            logger.warning("Failed to initialize semantic memory stack", exc_info=True)
            self._external_store = None
            self._memory_indexer = None
            self._memory_retriever = None

        # ── MemoryContext (auto-memory extraction + injection) ──
        try:
            if self._memory_store is not None:
                from memory.recall import MemoryRecallService
                def _publish_memory_recall(session_id: str, result) -> None:
                    if self._event_bus is None:
                        return
                    from server.events import WsMemoryRecall
                    injected = [r.memory_name for r in result.records if r.injected]
                    self._event_bus.publish_typed(session_id, WsMemoryRecall(
                        injected_count=len(injected),
                        candidate_count=result.total_candidates,
                        omitted_count=max(0, len(result.records) - len(injected)),
                        top_names=injected[:5],
                    ))

                self._memory_recall_service = MemoryRecallService(
                    self._memory_store,
                    getattr(self, '_memory_retriever', None),
                    event_callback=_publish_memory_recall,
                )
                self._memory_context = MemoryContext(
                    store=self._memory_store,
                    max_lines=getattr(self._config.memory, 'max_index_lines', 50),
                    enabled=getattr(self._config.memory, 'enabled', True),
                    retriever=getattr(self, '_memory_retriever', None),
                    selector_backend=None,  # uses default — no separate selector LLM
                    recall_service=self._memory_recall_service,
                )
                logger.info("MemoryContext created — auto-memory extraction + injection enabled")
        except Exception:
            logger.warning("Failed to create MemoryContext", exc_info=True)

        # ── Startup maintenance: prune expired + decay stale ──
        # Run in a background thread so a large memory store doesn't
        # block server startup (P1-34).
        if self._memory_store is not None:
            import threading
            def _prune_background() -> None:
                try:
                    pruned = self._memory_store.prune_expired()
                    if pruned:
                        logger.info("Startup memory prune: %d entries cleaned", pruned)
                    self._stats_service.prune_raw_telemetry()
                except Exception:
                    logger.warning("Startup maintenance failed", exc_info=True)
            threading.Thread(target=_prune_background, daemon=True, name="memory-startup-prune").start()
            try:
                loop = asyncio.get_running_loop()
                self._memory_stop_event = asyncio.Event()

                async def _memory_maintenance_loop() -> None:
                    while not self._memory_stop_event.is_set():
                        try:
                            await asyncio.wait_for(
                                self._memory_stop_event.wait(),
                                timeout=6 * 60 * 60,
                            )
                        except asyncio.TimeoutError:
                            try:
                                changed = await asyncio.to_thread(
                                    self._memory_store.prune_expired
                                )
                                telemetry = await asyncio.to_thread(
                                    self._stats_service.prune_raw_telemetry
                                )
                                logger.info(
                                    "Scheduled maintenance completed: %d memory changes, telemetry=%s",
                                    changed,
                                    telemetry,
                                )
                            except Exception:
                                logger.warning(
                                    "Scheduled memory maintenance failed",
                                    exc_info=True,
                                )

                self._memory_maintenance_task = loop.create_task(
                    _memory_maintenance_loop(),
                    name="memory-maintenance",
                )
            except RuntimeError:
                logger.debug("Memory maintenance scheduler awaits server event loop")

        # ── 5. Resource Governor ──
        from core.resource_governor import ResourceGovernor
        from core.resource_metrics import ResourceMetricsCollector
        self._resource_governor = ResourceGovernor(self._config.resource_governance)
        self._resource_metrics = ResourceMetricsCollector(
            self._resource_governor,
        )
        def _on_resource_event(event: dict[str, Any]) -> None:
            self._resource_metrics.collect()
            from core.resource_governor import ResourceKind
            snapshot = self._resource_governor.snapshot()
            worker = snapshot.snapshots[ResourceKind.WORKER_SLOT]
            event["governance"] = {
                "mode": self._resource_governor.mode,
                "worker": {
                    "limit": worker.limit,
                    "reserved": worker.reserved,
                    "consumed": worker.consumed,
                    "available": worker.available,
                    "queued": worker.queued,
                    "pressure": worker.pressure.value,
                },
                "active_leases": snapshot.active_leases,
                "queue_depth": self._resource_governor.queue_depth,
            }
            task_id = str(event.get("task_id", ""))
            resources = dict(event.get("resources") or {})
            actual = dict(event.get("actual") or {})
            phase = str(event.get("type", "")).removeprefix(
                "delegation_resource_"
            )
            if task_id and str(event.get("type", "")).startswith(
                "delegation_resource_"
            ):
                facts: dict[str, object] = {
                    "outcome": event.get("outcome", ""),
                    "queue_position": event.get("queue_position", 0),
                    "wait_time_s": event.get("wait_time_s", 0.0),
                    "reason": event.get("reason", ""),
                    "updated_at": event.get("timestamp_s", 0.0),
                }
                if phase == "queued":
                    facts["requested"] = resources
                elif phase == "granted":
                    facts["granted"] = resources
                elif phase in {"reconciled", "released"}:
                    facts["consumed"] = actual
                    facts["refunded"] = {
                        kind: max(
                            0,
                            int(amount) - int(actual.get(kind, 0)),
                        )
                        for kind, amount in resources.items()
                    }
                self._store.update_delegation_task_resource(
                    task_id, facts,
                )
            if self._event_bus is not None:
                session_id = str(event.get("session_id", ""))
                if session_id:
                    self._event_bus.publish_raw(
                        session_id,
                        {
                            "type": event.get("type"),
                            "payload": event,
                        },
                    )

        self._resource_governor.on_event(_on_resource_event)
        if self._resource_governor.mode == "observe":
            logger.info(
                "Resource governance running in OBSERVE mode — "
                "calculating capacity facts but NOT blocking any requests. "
                "Set resource_governance.mode to 'soft_enforce' or 'enforce' "
                "in config/default.yaml to enable enforcement."
            )

        # ── 6. Agent registry ──
        from agent.session.agent_registry import AgentRegistryV2
        self._agent_registry = AgentRegistryV2(project_dir=self.repo_path)

        # ── 7. Build ToolRegistry (with memory_store for LLM tools) ──
        self._registry = build_registry(
            self._config,
            repo_path=self.repo_path,
            approval_mode="auto",
            memory_store=self._memory_store,
            external_store=getattr(self, "_external_store", None),
        )
        self._registry.attach_resource_governor(
            self._resource_governor,
            root_session_resolver=lambda session_id: (
                (record.root_id or record.id)
                if (record := self._store.get_session(session_id)) is not None
                else session_id
            ),
        )
        from entry.bootstrap.registry_factory import initialize_mcp_integration
        self._mcp_integration = initialize_mcp_integration(
            self._config,
            self._registry,
        )

        # ── Load permission rules from settings.json ──
        self._loaded_rules = self._load_permission_rules()

        # ── 8. Log directory ──
        from core.state_paths import ProjectStatePaths

        self._log_dir = str(ProjectStatePaths.for_project(self.repo_path).logs)

        # ── 9. SessionRuntime ──
        from agent.session.runtime import SessionRuntime

        # ── HookDispatcher with memory consolidation STOP hook ──
        # Must be created BEFORE SessionRuntime (passed via constructor).
        self._hook_dispatcher = None
        if self._memory_store is not None:
            try:
                from hooks import HookDispatcher, HookEvent, HookRegistry, InternalHook

                _hook_registry = HookRegistry()
                _settings_path = Path(self.repo_path) / ".grace" / "settings.json"
                _hook_registry.load_from_settings(_settings_path)

                _store_ref = self._memory_store
                _log_dir_ref = self._log_dir
                _backend_ref = self._backend
                _repo_ref = Path(self.repo_path)

                def _on_session_stop(ctx):
                    from memory.consolidation import record_session_end, run_consolidation
                    try:
                        _store_dir = getattr(_store_ref, 'store_dir', None)
                        if _store_dir:
                            record_session_end(_store_dir)
                        run_consolidation(
                            _store_ref, log_dir=_log_dir_ref, backend=_backend_ref,
                            async_run=True, workspace_root=_repo_ref,
                        )
                        _store_ref.prune_expired()
                    except Exception as exc:
                        logger.debug("Consolidation hook skipped: %s", exc)

                _hook_registry.register_internal(HookEvent.STOP, InternalHook(callback=_on_session_stop))
                self._hook_dispatcher = HookDispatcher(_hook_registry, cwd=str(_repo_ref.resolve()))
                logger.info("HookDispatcher initialized with memory consolidation STOP hook")
            except Exception:
                logger.warning("Failed to initialize HookDispatcher", exc_info=True)

        self._runtime = SessionRuntime(
            store=self._store,
            backend=self._backend,
            base_registry=self._registry,
            agent_registry=self._agent_registry,
            root_agent_config=self._build_agent_cfg(),
            log_dir=self._log_dir,
            memory_context=self._memory_context,
            hook_dispatcher=self._hook_dispatcher,
            mcp_integration=self._mcp_integration,
            event_callback=self._event_bus.publish if self._event_bus is not None else None,
            governor=self._resource_governor,
        )
        # Mark as Web mode — child agents use this to create web callbacks
        self._runtime._is_web_mode = True
        self._runtime._stats_recorder = self._stats_recorder

        # Wire hook_dispatcher into registry for PreToolUse/PostToolUse hooks
        if self._hook_dispatcher is not None:
            self._registry.attach_hook_dispatcher(self._hook_dispatcher)

        # Wire worktree completion → WS event.  Keeps Runtime agnostic of
        # the transport layer (same pattern as _event_callback).
        if self._event_bus is not None:
            _eb = self._event_bus
            def _on_worktree_done(parent_id, child_id, action, status):
                from server.events import WsWorktreeResolved
                _eb.publish_typed(parent_id, WsWorktreeResolved(
                    child_session_id=child_id, action=action, status=status,
                ))
            self._runtime.set_worktree_completion_callback(_on_worktree_done)

            # ── Run lifecycle callback: run_started / run_terminal ──
            def _on_run_terminal(session_id: str, event: dict) -> None:
                """Publish run_started / run_terminal via EventBus.

                Both go through publish_raw → _publish_msg → persist
                (insert_trace_event, gets sequence) → WS broadcast.
                Same code path as all other events — no skip_persist.
                """
                if _eb is not None:
                    _eb.publish_raw(session_id, event)

            self._runtime._publish_run_terminal = _on_run_terminal

            def _on_memory_written(session_id, memory, source):
                from server.events import WsMemoryWritten
                if not session_id:
                    return
                _eb.publish_typed(session_id, WsMemoryWritten(
                    name=memory.name,
                    description=memory.description,
                    source=source,
                    confidence=float(getattr(memory.metadata, "confidence", 0.0)),
                ))
            self._runtime._memory_event_callback = _on_memory_written

        # ── Plan revision storage (SQLite-backed) ───────────────────────
        from server.services.plan_revision_service import PlanRevisionService
        self._plan_revisions = PlanRevisionService(self._storage, self.repo_path)

        # Read-only multi-agent review orchestration. It owns durable Review
        # Job/Task/Message state while SessionRuntime executes each reviewer.
        from server.services.review_service import ReviewService
        self._review_service = ReviewService(
            storage=self._storage,
            runtime=self._runtime,
            repo_path=self.repo_path,
            event_callback=(
                self._event_bus.publish_typed
                if self._event_bus is not None else None
            ),
        )

        # Read-only live registry and per-session architecture projection.
        from server.services.architecture_service import ArchitectureService
        self._architecture_service = ArchitectureService(self)
        from server.services.replay_service import ReplayService
        self._replay_service = ReplayService(self)
        from server.services.safety_service import SafetyService
        self._safety_service = SafetyService(self)
        from server.services.multi_agent_service import MultiAgentService
        self._multi_agent_service = MultiAgentService(self)
        from server.services.reliability_service import ReliabilityService
        self._reliability_service = ReliabilityService(self)
        from server.services.project_overview_service import ProjectOverviewService
        self._project_overview_service = ProjectOverviewService(self)

        # ── Memory maintenance daemon: periodic decay + TTL expiry ──
        self._memory_maintenance_stop = threading.Event()
        self._memory_maintenance_thread = threading.Thread(
            target=self._memory_maintenance_loop, daemon=True,
        )
        self._memory_maintenance_thread.start()

        logger.info(
            "AgentService initialized — repo=%s, model=%s",
            self.repo_path, self._config.llm.model,
        )

    # ── Config helpers ────────────────────────────────────────────────────

    def _apply_cli_overrides(
        self,
        model: str | None,
        provider: str | None,
        api_key: str | None,
        base_url: str | None,
        max_steps: int | None,
    ) -> None:
        """Apply CLI-specified overrides to the loaded config."""
        if model:
            self._config.llm.model = model
        if provider:
            self._config.llm.provider = provider
        if api_key:
            self._config.llm.api_key = api_key
        if base_url:
            self._config.llm.base_url = base_url
        if max_steps is not None:
            self._config.agent.max_steps = max_steps

    def _build_agent_cfg(self):
        """Build AgentConfig from the current AppConfig (adapted from ChatSession)."""
        from agent.core import AgentConfig

        cfg = AgentConfig(
            max_steps=self._config.agent.max_steps,
            budget_tokens=self._config.agent.budget_tokens,
            request_budget_tokens=self._config.context.request_budget_tokens,
            history_max_messages=self._config.context.history_window * 2,
            llm_max_retries=3,
            llm_retry_delay=1.0,
            request_timeout=float(self._config.llm.timeout_seconds),
            stream=True,
            confirm_dangerous=False,
            token_budget_continuation=True,
            streaming_tool_execution=True,
            prompt_config=self._config.prompts,
        )

        # ── L-1: Langfuse RetryMetrics tracer (Phase 7) ────────────────
        if self._observe_retries:
            from observability.retry_tracer import get_retry_tracer

            _tracer = get_retry_tracer()
            _tracer._enabled = True
            cfg.llm_metrics_callback = _tracer.emit
            logger.info(
                "RetryTracer activated — metrics will be logged after each LLM call",
            )

        return cfg

    # ── Permission rule loading ────────────────────────────────────────────

    def _load_permission_rules(self) -> list:
        """Load deny/ask/allow permission rules from settings files.

        CC-aligned configuration hierarchy (latter overrides former):
        1. Builtin defaults (read-only tools allowed, destructive blocked)
        2. ~/.forge-agent/settings.json (user-level)
        3. .forge-agent/settings.json (project-level, version-controlled)
        4. .forge-agent/settings.local.json (local, git-ignored)

        Returns:
            list[PermissionRule]: Merged rules from all sources.
        """
        from hitl.permission_rule import PermissionRule

        rules: list[PermissionRule] = []
        repo = self.repo_path

        # Builtin defaults are already loaded by build_registry() →
        # load_permission_settings() when the pipeline is constructed.
        # Only load user/project/local overrides here.

        # Load user-level settings
        _load_json_file(Path.home() / ".forge-agent" / "settings.json", rules, "user")

        # Load project-level settings
        _load_json_file(Path(repo) / ".forge-agent" / "settings.json", rules, "project")

        # Load local settings (highest priority)
        _load_json_file(Path(repo) / ".forge-agent" / "settings.local.json", rules, "local")

        logger.info("Loaded %d permission rules for session", len(rules))
        return rules

    def _maybe_reload_rules(self) -> None:
        """Re-read settings files if any have changed on disk (mtime polling).

        Called before each run — lightweight (just stat() calls), no
        external dependencies needed.
        """
        if not hasattr(self, '_settings_mtimes'):
            self._settings_mtimes: dict[str, float] = {}

        _paths = [
            Path.home() / ".forge-agent" / "settings.json",
            Path(self.repo_path) / ".forge-agent" / "settings.json",
            Path(self.repo_path) / ".forge-agent" / "settings.local.json",
        ]
        _changed = False
        for p in _paths:
            try:
                _mtime = p.stat().st_mtime
                if self._settings_mtimes.get(str(p)) != _mtime:
                    self._settings_mtimes[str(p)] = _mtime
                    _changed = True
            except OSError:
                continue

        if _changed:
            logger.info("Settings file(s) changed — reloading permission rules")
            self._loaded_rules = self._load_permission_rules()

    # ── Web headless approval callback (CC control_request equivalent) ────

    def _build_web_confirm_callback(self, session_id: str):
        """Build a synchronous blocking callback for Web headless approval.

        CC equivalent::

            stdout → {"type":"control_request","request_id":"...","tool":"..."}
            stdin  ← {"type":"control_response","request_id":"...","decision":"allow"}

        Forge equivalent::

            WS push → {"type":"approval_required","request_id":"...","tool_name":"..."}
            Agent thread blocks on threading.Event
            HTTP POST → broker.resolve(request_id, decision)
            Event.set() → Agent thread continues
        """
        broker = self._runtime._ensure_approval_broker(session_id)
        event_bus = self._event_bus
        from server.services.approval_broker import ApprovalRequest

        def _confirm(request):
            from hitl.pipeline import PromptAction
            decision_started = time.monotonic()
            ar = ApprovalRequest(
                tool_name=request.tool_name,
                params=dict(request.params),
                thought=request.thought or "",
            )

            _req_info = {
                "tool_name": request.tool_name,
                "params": dict(request.params),
                "thought": request.thought or "",
                "decision_reason": getattr(request, 'decision_reason', ""),
                "tool_use_id": getattr(request, 'tool_use_id', ""),
            }

            def push_event(req_id: str) -> None:
                """Push approval_required WS event (CC control_request equivalent)."""
                if event_bus is not None:
                    from server.events import WsApprovalRequired
                    event_bus.publish_typed(session_id, WsApprovalRequired(
                        request_id=req_id,
                        tool_name=_req_info["tool_name"],
                        params=_req_info["params"],
                        thought=_req_info["thought"],
                        decision_reason=_req_info.get("decision_reason", ""),
                        tool_use_id=_req_info.get("tool_use_id", ""),
                    ))

            # Block until decision or timeout
            decision = broker.wait_for_decision(ar, on_pending=push_event)
            wait_ms = (time.monotonic() - decision_started) * 1000

            if event_bus is not None:
                from server.events import WsApprovalResolved
                event_bus.publish_typed(session_id, WsApprovalResolved(
                    request_id=ar.request_id or "",
                    tool_name=_req_info["tool_name"],
                    decision=decision.action.value,
                    note=decision.note or "",
                    updated_input=decision.updated_params is not None,
                    wait_ms=wait_ms,
                ))

            # If timed out, push a cleanup event so the frontend removes the card
            if decision.action is PromptAction.DENY and "timed out" in (decision.note or ""):
                if event_bus is not None:
                    from server.events import WsApprovalTimeout
                    event_bus.publish_typed(session_id, WsApprovalTimeout(
                        request_id=ar.request_id or "",
                    ))

            return decision

        return _confirm

    # ── Session management ────────────────────────────────────────────────

    def ensure_root_session(self) -> str:
        """Reuse or create a root session.

        On first start, creates a new root session in the DB.
        On subsequent restarts, reuses the most recent session to avoid
        inflating the session count with dead root sessions.

        Returns:
            str: The root session ID.
        """
        if self._root_session_id is not None:
            existing = self.session_service.get_session(self._root_session_id)
            if existing is not None:
                return self._root_session_id

        # Reuse the most recent non-child session in the DB
        try:
            sessions = self.session_service.list_sessions(limit=1)
            if sessions:
                sid = sessions[0]["id"]
                self._root_session_id = sid
                logger.info("Reusing existing session as root: %s", sid)
                return sid
        except Exception:
            pass

        # No existing sessions — create a fresh root
        if hasattr(self, "_runtime") and self._runtime is not None:
            self._root_session = self._runtime.create_root_session(
                agent_name="build",
                repo_path=self.repo_path,
                title="Web MVP Root Session",
                metadata={"entrypoint": "web", "source": "server"},
            )
            self._root_session_id = self._root_session.id
        else:
            # Runtime not available yet (partial init) — use storage directly
            from agent.session.models import SessionMode, AgentKind
            rec = self._storage.create_session(
                agent_name="build", mode=SessionMode.PRIMARY,
                repo_path=self.repo_path, title="Web MVP Root Session",
                agent_kind=AgentKind.PRIMARY,
            )
            self._root_session_id = rec.id
        logger.info("Created root session: %s", self._root_session_id)
        return self._root_session_id

    def create_session(
        self,
        agent_name: str = "build",
        *,
        repo_path: str | None = None,
        title: str = "",
    ) -> str:
        """Create a new root session and return its ID.

        Args:
            agent_name: Agent definition name (e.g. 'build', 'plan').
            repo_path: Repo path (defaults to service's repo_path).
            title: Optional human-readable title.

        Returns:
            str: The new session's 12-char hex ID.
        """
        record = self._runtime.create_root_session(
            agent_name=agent_name,
            repo_path=repo_path or self.repo_path,
            title=title or "Session via Web API",
        )
        logger.info("Created session: %s (agent=%s)", record.id, agent_name)
        return record.id

    # ── Execution ─────────────────────────────────────────────────────────

    async def chat(
        self,
        session_id: str,
        prompt: str,
        agent_name: str = "build",
        intent: str | None = None,
    ) -> RunResult:
        """Execute one chat round via SessionRuntime.run_session() (blocking).

        Kept for backward compatibility. New code should use
        ``run_chat_async()`` for non-blocking execution with WS events.
        """
        resolved_intent: TaskIntent | None = None
        if intent is not None:
            resolved_intent = TaskIntent(intent.lower())

        def _run() -> RunResult:
            return self._runtime.run_session(
                session_id=session_id,
                agent_name=agent_name,
                task_description=prompt,
                intent=resolved_intent,
            )

        return await asyncio.to_thread(_run)

    def run_chat_async(
        self,
        session_id: str,
        prompt: str,
        agent_name: str = "build",
        intent: str | None = None,
        display_prompt: str = "",
        allowed_prompts: list[dict[str, str]] | None = None,
        run_context: Any = None,
    ) -> None:
        """Execute chat asynchronously in a background thread.

        Returns immediately.  All execution events are pushed through the
        EventBus to WebSocket subscribers.

        When *run_context* is provided (from the POST /chat transaction),
        it carries the pre-created run_id / turn_id / turn_index.  The
        pipeline will transition the run through QUEUED → RUNNING →
        COMPLETED / FAILED / CANCELLED.

        The caller should ensure the frontend has subscribed to the WS
        before calling this method.

        allowed_prompts: CC-aligned ExitPlanMode pre-approved tool calls
        that carry over to the build session.
        """
        resolved_intent: TaskIntent | None = None
        if intent is not None:
            resolved_intent = TaskIntent(intent.lower())

        # TOCTOU guard: atomically check-and-acquire before spawning thread.
        # Skip when a run_context is provided — the POST handler already
        # verified no active run exists via get_active_run().
        if run_context is None:
            if not self._runtime.try_acquire_session(session_id):
                raise RuntimeError(f"Session {session_id} is already running")

        # MCP readiness gate: wait up to 5s for background MCP connection.
        # Prevents agent from running before MCP tools are discovered.
        if self._mcp_integration is not None:
            _mcp_deadline = time.time() + 5.0
            while not self._mcp_integration.is_initialized:
                if time.time() > _mcp_deadline:
                    logger.warning("MCP not ready after 5s — proceeding without MCP tools")
                    break
                time.sleep(0.2)

        # Plan detection: explicit only.  Callers must pass agent_name="plan".
        # Intent is an execution hint, not a mode-switch — the agent definition
        # is the single source of truth for what tools/permissions are available.
        # ── Inject permission rules + mode into the runtime ──
        self._maybe_reload_rules()
        _pending_perm = self._runtime.pop_pending_permission_mode_override(
            session_id,
        )
        _effective_perm = _pending_perm or "acceptEdits"

        # ── Delegate to ChatPipeline (6-stage pipeline, P1-10) ──
        # Permission mode is passed through ctx → pipeline.execute()
        # → run_session(inject_permission_mode=) — single-owner path.
        from server.services.chat_pipeline import (
            ChatPipeline,
            ChatPipelinePorts,
            ChatRequest,
        )

        ports = ChatPipelinePorts(
            runtime=self._runtime,
            session_service=self.session_service,
            backend=self._backend,
            config=self._config,
            effective_llm_config=dict(self._effective_llm_config),
            repo_path=self.repo_path,
            build_confirm_callback=self._build_web_confirm_callback,
            reload_rules=self._maybe_reload_rules,
            loaded_rules=lambda: list(self._loaded_rules),
            accumulate_session_stats=self._accumulate_session_stats,
            compact_session_async=self.compact_session_async,
            event_bus=self._event_bus,
            plan_revisions=self._plan_revisions,
        )
        pipeline = ChatPipeline(ports)
        request = ChatRequest(
            session_id=session_id,
            prompt=prompt,
            display_prompt=display_prompt,
            agent_name=agent_name,
            intent=resolved_intent,
            permission_mode=_effective_perm,
            repo_path=self.repo_path,
            allowed_prompts=tuple(allowed_prompts or ()),
            run_context=run_context,
        )

        pipeline.run_in_background(request)

    def resolve_user_skill(
        self,
        name: str,
        arguments: str = "",
        *,
        session_id: str = "",
    ) -> str:
        """Validate and render one user-invocable Skill for Web execution."""
        skill_registry = self._registry.skill_registry
        if skill_registry is None:
            raise ValueError("Skills are not available")
        normalized = name.strip()
        meta = skill_registry.get_skill_meta(normalized)
        if meta is None:
            raise ValueError(f"Unknown Skill: {normalized}")
        if not meta.user_can_invoke:
            raise ValueError(f"Skill is not user-invocable: {normalized}")
        if meta.context == "fork":
            raise ValueError(
                f"Skill '{normalized}' requires fork context, which is not "
                "supported by direct Web invocation yet"
            )
        rendered = skill_registry.load_and_render(
            normalized,
            arguments,
            session_id=self._root_session_id or "",
            project_dir=self.repo_path,
        )
        if not rendered:
            raise ValueError(f"Skill has no executable instructions: {normalized}")
        if session_id:
            from skills.tool import SkillContextModifier
            if meta.model:
                self._runtime.set_pending_model(session_id, meta.model)
            if meta.effort:
                self._runtime.set_pending_effort(session_id, meta.effort)
            self._runtime.set_pending_skill_modifier(
                session_id,
                SkillContextModifier(
                    allowed_tools=meta.allowed_tools,
                    disallowed_tools=meta.disallowed_tools,
                    model=meta.model,
                    effort=meta.effort,
                    context=meta.context,
                ),
            )
        return (
            f"[USER-INVOKED SKILL: {normalized}]\n\n"
            f"{rendered}"
        )

    # ── Compression recovery helper (module-level) ──────────────────────

    @staticmethod
    def _build_recovery_context(repo_path: str) -> str:
        """Build recovery context to re-inject after compaction.

        Returns CLAUDE.md content (if present) and a list of recently
        modified files so the agent can re-orient after compression.
        CC equivalent: post-AutoCompact context restoration.
        """
        import os as _os
        parts: list[str] = []
        root = Path(repo_path)

        # 1. CLAUDE.md / AGENTS.md
        for md_name in ("CLAUDE.md", "AGENTS.md", "AGENT.md"):
            md_path = root / md_name
            if md_path.is_file():
                try:
                    content = md_path.read_text(encoding="utf-8")[:3000]
                    parts.append(f"## Project Instructions ({md_name})\n{content}")
                except Exception:
                    pass
                break

        # 2. Recently modified files (last 5, capped at 1K chars each)
        # Limit scope: skip VCS, caches, and dependency dirs
        _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                       ".forge-agent", ".grace", ".claude", ".mypy_cache",
                       ".pytest_cache", ".tox", "dist", "build", ".eggs"}
        try:
            recent: list[tuple[str, float]] = []
            for dirpath, dirnames, filenames in _os.walk(str(root)):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    fp = _os.path.join(dirpath, fn)
                    try:
                        mtime = _os.stat(fp).st_mtime
                        recent.append((fp, mtime))
                    except OSError:
                        continue
            recent.sort(key=lambda x: x[1], reverse=True)
            shown = 0
            for fp, _ in recent:
                if shown >= 5:
                    break
                rel = str(Path(fp).relative_to(root))
                try:
                    content = Path(fp).read_text(encoding="utf-8")[:1000]
                    parts.append(f"## Recent file: {rel}\n```\n{content}\n```")
                except Exception:
                    parts.append(f"## Recent file: {rel}\n[Binary or unreadable]")
                shown += 1
        except Exception:
            pass

        return "\n\n".join(parts) if parts else ""

    def compact_session_async(self, session_id: str) -> None:
        """Trigger context compression in a background thread.

        Runs the Snip → MicroCompact → AutoCompact pipeline.
        Pushes a ``compacted`` status event via EventBus when done.
        """
        def _compact():
            try:
                # Get session messages
                msgs = self.session_service.get_messages(session_id)
                if not msgs:
                    if self._event_bus is not None:
                        self._event_bus.publish_raw(session_id, {
                            "type": "status", "status": "compacted",
                            "message": "No messages to compact",
                        })
                    return

                # Run compaction via runtime
                from context.compaction import ConversationCompactor
                compactor = ConversationCompactor(backend=self._backend)
                compacted = compactor.compact_history(msgs)
                logger.info(
                    "Compacted session %s: %d → %d messages",
                    session_id, len(msgs), len(compacted),
                )

                # ── Recovery: re-inject critical context after compaction ──
                _recovery = AgentService._build_recovery_context(self.repo_path)
                if _recovery:
                    compacted.append({
                        "role": "user",
                        "content": f"[AUTOCOMPACT RECOVERY]\n{_recovery}",
                        "kind": "recovery_context",
                    })

                before_chars = sum(
                    len(str(item.get("content", ""))) for item in msgs
                )
                after_chars = sum(
                    len(str(item.get("content", ""))) for item in compacted
                )
                result = self._storage.replace_messages_with_compaction(
                    session_id,
                    compacted,
                    method="snip_micro_auto",
                    tokens_before=(before_chars + 3) // 4,
                    tokens_after=(after_chars + 3) // 4,
                    truncation_status=(
                        "reduced" if after_chars < before_chars else "unchanged"
                    ),
                )

                # After compaction, run storage optimization to reclaim space
                try:
                    opt = self._storage.optimize_storage()
                    logger.debug("Storage optimized: %d → %d pages",
                                opt.get("pages_before", 0), opt.get("pages_after", 0))
                except Exception:
                    pass

                if self._event_bus is not None:
                    self._event_bus.publish_raw(session_id, {
                        "type": "status",
                        "status": "compacted",
                        "compaction": result,
                        "message": f"Compressed {len(msgs)} → {len(compacted)} messages",
                    })
            except Exception as exc:
                logger.exception("Compact failed for session %s", session_id)
                if self._event_bus is not None:
                    self._event_bus.publish_raw(session_id, {
                        "type": "status",
                        "status": "failed",
                        "error": str(exc),
                    })

        import threading
        thread = threading.Thread(target=_compact, daemon=True)
        thread.start()

    # ── Plan file management ─────────────────────────────────────────────

    # ── Memory maintenance daemon ────────────────────────────────────────

    def _memory_maintenance_loop(self) -> None:
        """Background daemon: periodically prune expired + decay stale memories."""
        _INTERVAL = int(os.environ.get("GRACE_MEMORY_MAINTENANCE_SECONDS", str(6 * 3600)))
        while not self._memory_maintenance_stop.wait(_INTERVAL):
            self._do_memory_maintenance()

    def _do_memory_maintenance(self) -> None:
        """Run one cycle of memory decay + TTL expiry."""
        if self._memory_store is None:
            return
        try:
            pruned = self._memory_store.prune_expired()
            if pruned:
                logger.info("Memory maintenance: %d entries cleaned", pruned)
        except Exception:
            logger.debug("Memory maintenance cycle skipped", exc_info=True)

    def remove_plan_file(self, session_id: str) -> bool:
        """Remove the plan file for a session (CC-aligned cleanup).

        Called when a plan is approved (consumed), aborted (discarded),
        or the session is deleted.
        """
        try:
            plan_dir = Path(self.repo_path) / ".grace" / "plans"
            plan_file = plan_dir / f"{session_id}.md"
            if plan_file.is_file():
                plan_file.unlink()
                logger.info("Plan file removed: %s", plan_file)
                return True
        except Exception:
            logger.debug("Plan file removal skipped for %s", session_id, exc_info=True)
        return False

    # ── Session context injection ────────────────────────────────────────

    def _inject_session_context(self, session_id: str) -> bool:
        """Inject previous session summary once per root session.

        CLI ChatSession does this on startup (chat.py:130-138).
        Web mode injects on the first round, then sets a metadata flag
        to prevent duplicate injection on subsequent rounds.
        """
        # Guard: only inject once per session
        rec = self.session_service.get_session(session_id)
        if rec is None:
            return False
        already_injected = rec.metadata.get("session_context_injected")
        if already_injected:
            return False

        injected = False
        try:
            from context.compaction import load_session_summary
            summary_path = Path(self.repo_path) / ".grace" / "session_summary.md"
            summary = load_session_summary(str(summary_path))
            if summary:
                from llm.base import LLMMessage
                self._storage.append_message(session_id, LLMMessage(
                    role="user",
                    content=f"[Previous Session Context]\n{summary}",
                ))
                self._storage.append_message(session_id, LLMMessage(
                    role="assistant", content="Understood.",
                ))
                injected = True
        except Exception:
            logger.debug("Session summary injection skipped", exc_info=True)

        # Mark as injected regardless of success (don't retry every round)
        try:
            store = self._storage.store
            with store._connect() as conn:
                meta = dict(rec.metadata)
                meta["session_context_injected"] = True
                conn.execute(
                    "UPDATE sessions SET metadata_json = ? WHERE id = ?",
                    (json.dumps(meta, ensure_ascii=True), session_id),
                )
        except Exception:
            pass

        return injected

    # ── Cross-round stats ─────────────────────────────────────────────────

    def _accumulate_session_stats(self, session_id: str, result) -> None:
        """Accumulate cross-round statistics in session metadata.

        CLI ChatSession tracks total_tokens, total_steps, and round_count
        across multiple run_session() calls.  Web mode must persist these
        in session metadata since each call is stateless.
        """
        try:
            rec = self.session_service.get_session(session_id)
            if rec is None:
                return
            meta = dict(rec.metadata)
            meta["total_tokens"] = meta.get("total_tokens", 0) + (result.total_tokens or 0)
            meta["total_steps"] = meta.get("total_steps", 0) + (result.steps_taken or 0)
            meta["round_count"] = meta.get("round_count", 0) + 1
            # Persist via storage — also touch updated_at so the
            # frontend context bar shows a fresh "Updated" timestamp.
            store = self._storage.store
            with store._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET metadata_json = ?, updated_at = datetime('now') WHERE id = ?",
                    (json.dumps(meta, ensure_ascii=True), session_id),
                )
        except Exception:
            logger.debug("Failed to accumulate session stats for %s", session_id, exc_info=True)

    # ── Cancel ────────────────────────────────────────────────────────────

    def cancel_session(self, session_id: str, detail: str = "") -> bool:
        """Cancel the active run via its cancellation token.

        Args:
            session_id: The session whose active run to cancel.
            detail: Human-readable reason.

        Returns:
            bool: True if an active cancellation token was found and signalled.
        """
        return self.cancel_run(session_id, detail)

    def cancel_run(self, session_id: str, detail: str = "") -> bool:
        """Cancel the currently active run.

        CAS-updates the run to 'cancelled' and signals the cancellation
        token.  The agent loop will stop at the next safe point and the
        finally block will broadcast run_terminal { status: "cancelled" }.
        """
        # Wake any pending approval first so the agent loop can exit quickly
        broker = self._runtime.get_approval_broker(session_id)
        if broker is not None:
            broker.cancel_pending()

        # CAS-update the active run → cancelled
        active_run = self._storage.get_active_run(session_id)
        if active_run is not None:
            try:
                self._storage.update_run(
                    active_run["id"],
                    status="cancelled",
                    error=detail or "User cancelled",
                    expect_status="running",
                )
                from agent.session.models import SessionStatus
                self._storage.update_status(
                    session_id,
                    SessionStatus.CANCELLED,
                    error=detail or "User cancelled",
                )
            except Exception:
                logger.debug("Run cancel CAS failed", exc_info=True)

        cancelled = self._runtime.cancel_session(session_id, detail=detail)
        if cancelled and getattr(self, "_event_bus", None) is not None:
            self._event_bus.publish_typed(
                session_id,
                WsStatus(status="cancelled", message=detail or "User cancelled"),
            )
        return cancelled

    # ── Config snapshot ───────────────────────────────────────────────────

    def get_config_snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the current runtime configuration.

        Returns:
            dict: Config fields safe for API response.
        """
        return {
            "repo_path": self.repo_path,
            "model": self._config.llm.model,
            "provider": self._config.llm.provider,
            "max_steps": self._config.agent.max_steps,
            "budget_tokens": self._config.agent.budget_tokens,
            "root_session_id": self._root_session_id,
            "agents_available": list(
                self._agent_registry.list_primary_agents()
            ) if hasattr(self._agent_registry, "list_primary_agents") else [],
        }

    # ── Cleanup ───────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Release resources. Called on app shutdown.

        Phase 2 unified shutdown order:
          1. Stop admission (governor)
          2. Cancel running sessions
          3. Close LLM backend streams
          4. Dispose runtime (cancel tokens, drain threads, shutdown executor)
          5. Memory maintenance
          6. MCP disconnect
        """
        logger.info("AgentService shutting down")

        # 1. Stop resource admission
        if self._resource_governor is not None:
            self._resource_governor.shutdown()

        # 2. Cancel running sessions
        if self._runtime is not None:
            for (session_id, gen) in list(self._runtime._cancellation_tokens.keys()):
                self._runtime.cancel_session(
                    session_id, detail="Server shutting down",
                )

        # 3. Close LLM backend streams
        if hasattr(self._backend, "close"):
            try:
                self._backend.close()
            except Exception:
                logger.debug("Error closing LLM backend", exc_info=True)

        # 4. Dispose runtime (cancels tokens, drains threads, shuts down executor)
        if self._runtime is not None:
            self._runtime.dispose()

        # 5. Memory maintenance
        if hasattr(self, '_memory_maintenance_stop'):
            self._memory_maintenance_stop.set()
        if self._memory_stop_event is not None:
            self._memory_stop_event.set()
        if self._memory_maintenance_task is not None:
            self._memory_maintenance_task.cancel()
            try:
                await self._memory_maintenance_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Memory maintenance shutdown failed", exc_info=True)
        if self._memory_store is not None:
            try:
                pruned = self._memory_store.prune_expired()
                if pruned:
                    logger.info("Shutdown memory prune: %d entries cleaned", pruned)
            except Exception:
                pass

        # 6. Close the single Tool/Skills/MCP lifecycle root.
        if self._registry is not None:
            self._registry.close()
