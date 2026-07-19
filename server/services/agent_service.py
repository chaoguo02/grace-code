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
import logging
import os
from pathlib import Path
from typing import Any

from agent.task import RunResult, TaskIntent

from server.services.session_service import SessionService

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

        # ── 1. Load config ──
        from config.schema import load_config, AppConfig

        self._config: AppConfig = load_config(config_path)
        self._apply_cli_overrides(model, provider, api_key, base_url, max_steps)

        # ── 2. Create LLM backend ──
        from llm.router import create_backend_from_config

        self._backend = create_backend_from_config({
            "provider": self._config.llm.provider,
            "model": self._config.llm.model,
            "api_key": self._config.llm.api_key or None,
            "base_url": self._config.llm.base_url or None,
            "max_tokens": self._config.llm.max_tokens,
            "timeout_seconds": self._config.llm.timeout_seconds,
        })

        # ── 3. Build ToolRegistry ──
        from entry.bootstrap.registry_factory import build_registry

        # CC-aligned: approval_mode="auto" so unclassified tools pass
        # without prompting.  ASK-rule tools (Write/Edit/dangerous Bash)
        # are force_interactive=True and always show the approval card.
        # The web_confirm_callback blocks the agent thread on
        # threading.Event — the exact equivalent of CC's stdin-blocking
        # control_request / control_response protocol.
        # ── Init MCP registry (connect servers, discover tools) ──
        self._mcp_registry = None
        try:
            from mcp.registry import McpRegistry
            self._mcp_registry = McpRegistry(self.repo_path)
            # Connect in background (non-blocking)
            import asyncio as _asyncio
            import threading as _threading
            def _connect_mcp():
                try:
                    _loop = _asyncio.new_event_loop()
                    _asyncio.set_event_loop(_loop)
                    _loop.run_until_complete(self._mcp_registry.connect_all())
                    _loop.run_until_complete(self._mcp_registry.fetch_all_tools())
                    _loop.close()
                    _total = self._mcp_registry.total_tools
                    if _total:
                        logger.info("MCP: %d tools from %d servers ready",
                                    _total, len(self._mcp_registry.connected_servers))
                except Exception as e:
                    logger.warning("MCP init failed: %s", e)
            _thread = _threading.Thread(target=_connect_mcp, daemon=True)
            _thread.start()
        except Exception as e:
            logger.info("MCP not available: %s", e)

        # ── 4. Session store + StorageBackend ──
        from agent.session import default_session_db_path
        from agent.session.session_store import SessionStore
        from app.storage.sqlite import SqliteStorageBackend

        db_path = default_session_db_path(self.repo_path)
        from core.state_paths import migrate_legacy_session_db

        migrate_legacy_session_db(self.repo_path, db_path)
        self._store = SessionStore(db_path)
        self._storage: SqliteStorageBackend = SqliteStorageBackend(db_path)

        # ── SessionService ─────────────────────────────────────────────
        from server.services.session_service import SessionService
        self.session_service = SessionService(self._storage)

        # ── StatsService + StatsRecorder ───────────────────────────────
        from server.services.stats_service import StatsService
        from server.services.stats_recorder import StatsRecorder

        self._stats_service = StatsService(self._storage)
        self._stats_recorder = StatsRecorder(self._stats_service)
        if self._event_bus is not None:
            self._event_bus.recorder = self._stats_recorder

        # ── MemoryStore (needed by both build_registry and router) ──────
        from memory.store import MemoryStore

        try:
            self._memory_store = MemoryStore(repo_path=self.repo_path, db_path=db_path)
        except Exception:
            logger.warning("Failed to initialize MemoryStore", exc_info=True)
            self._memory_store = None

        # ── ExternalMemoryStore (semantic search) ───────────────────────
        try:
            from memory.external_store import ExternalMemoryStore
            self._external_store = ExternalMemoryStore()
        except Exception:
            logger.info("ExternalMemoryStore not available (install fastembed for semantic search)")
            self._external_store = None

        # ── Memory maintenance (asyncio task, graceful shutdown) ─────────
        if self._memory_store is not None:
            self._memory_stop_event: asyncio.Event | None = asyncio.Event()
            _store = self._memory_store
            _stop = self._memory_stop_event
            _interval = 600  # 10 minutes, configurable

            async def _memory_maintenance():
                while not _stop.is_set():
                    try:
                        await asyncio.wait_for(_stop.wait(), timeout=_interval)
                        break  # stop signaled
                    except asyncio.TimeoutError:
                        pass  # time to decay
                    try:
                        backend = getattr(_store, '_backend', None)
                        if backend is not None and hasattr(backend, 'decay_confidences'):
                            decayed = backend.decay_confidences()
                            if decayed:
                                logger.debug("Memory decay: %d entries updated", decayed)
                    except Exception:
                        pass

            self._memory_maintenance_task = asyncio.ensure_future(_memory_maintenance())
        else:
            self._memory_stop_event = None
            self._memory_maintenance_task = None

        # ── 5. Agent registry ──
        from agent.session.agent_registry import AgentRegistryV2
        self._agent_registry = AgentRegistryV2(project_dir=self.repo_path)

        # ── 6. Build ToolRegistry (with memory_store for LLM tools) ──
        self._registry = build_registry(
            self._config,
            repo_path=self.repo_path,
            approval_mode="auto",
            memory_store=self._memory_store,
            external_store=getattr(self, "_external_store", None),
            mcp_registry=self._mcp_registry,
        )

        # ── Load permission rules from settings.json ──
        self._loaded_rules = self._load_permission_rules()

        # ── 6. Log directory ──
        from core.state_paths import ProjectStatePaths

        self._log_dir = str(ProjectStatePaths.for_project(self.repo_path).logs)

        # ── 7. SessionRuntime ──
        from agent.core import AgentConfig
        from agent.session.runtime import SessionRuntime

        self._runtime = SessionRuntime(
            store=self._store,
            backend=self._backend,
            base_registry=self._registry,
            agent_registry=self._agent_registry,
            root_agent_config=self._build_agent_cfg(),
            log_dir=self._log_dir,
            event_callback=self._event_bus.publish if self._event_bus is not None else None,
        )
        # Mark as Web mode — child agents use this to create web callbacks
        self._runtime._is_web_mode = True

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

        # ── Plan revision storage (SQLite-backed) ───────────────────────
        from server.services.plan_revision_service import PlanRevisionService
        self._plan_revisions = PlanRevisionService(self._storage, self.repo_path)

        # Root session created lazily on first chat()
        self._root_session = None
        self._root_session_id: str | None = None
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

        return AgentConfig(
            max_steps=self._config.agent.max_steps,
            budget_tokens=self._config.agent.budget_tokens,
            request_budget_tokens=self._config.context.request_budget_tokens,
            history_max_messages=self._config.context.history_window * 2,
            llm_max_retries=3,
            llm_retry_delay=1.0,
            stream=True,  # Web MVP: streaming for real-time step-by-step display
            confirm_dangerous=False,
        )

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

        def _confirm(request) -> "PromptDecision":
            from hitl.pipeline import PromptDecision, PromptAction
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
            existing = self._session_service.get_session(self._root_session_id)
            if existing is not None:
                return self._root_session_id

        # Reuse the most recent non-child session in the DB
        try:
            sessions = self._session_service.list_sessions(limit=1)
            if sessions:
                sid = sessions[0]["id"]
                self._root_session_id = sid
                logger.info("Reusing existing session as root: %s", sid)
                return sid
        except Exception:
            pass

        # No existing sessions — create a fresh root
        self._root_session = self._runtime.create_root_session(
            agent_name="build",
            repo_path=self.repo_path,
            title="Web MVP Root Session",
            metadata={"entrypoint": "web", "source": "server"},
        )
        self._root_session_id = self._root_session.id
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
            title=title or f"Session via Web API",
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
            resolved_intent = TaskIntent(intent)

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
    ) -> None:
        """Execute chat asynchronously in a background thread.

        Returns immediately.  All execution events are pushed through the
        EventBus to WebSocket subscribers.  When execution finishes, a
        ``status: completed`` or ``status: failed`` event is pushed.

        For plan sessions (agent_name="plan" or intent="analysis"), a
        ``plan_ready`` event is emitted on completion so the frontend can
        show the approve/reject UI.

        The caller should ensure the frontend has subscribed to the WS
        before calling this method.
        """
        resolved_intent: TaskIntent | None = None
        if intent is not None:
            resolved_intent = TaskIntent(intent)

        _is_plan = agent_name == "plan" or (
            resolved_intent is not None and resolved_intent == TaskIntent.ANALYSIS
        )
        # Gap 2 fix: when intent=ANALYSIS, force plan agent definition.
        # The session may have been created with agent_name="build" —
        # using build agent for plan violates read-only constraints.
        if _is_plan and agent_name != "plan":
            agent_name = "plan"

        def _resolve_mentions(text: str, repo: str) -> str:
            """Resolve @path mentions in *text* to file content blocks.

            Scans for @<path> tokens, reads the referenced files from *repo*,
            and wraps them in [FILE: ...] [/FILE] blocks so the model sees
            the file content as part of the user prompt.
            """
            import re as _re
            _AT_RE = _re.compile(r"(?:^|\s)@(\S+)")
            _repo_root = Path(repo).resolve()
            # Sensitive paths that must not be resolved via @mention
            _DENY_PREFIXES = (".git/", ".git", ".forge-agent/", ".grace/",
                              ".claude/", ".env", "settings.json", "secrets")

            def _resolve_one(match: _re.Match) -> str:
                _ref = match.group(1).rstrip(".,;:!?")
                # Block sensitive paths
                for _prefix in _DENY_PREFIXES:
                    if _ref.startswith(_prefix) or _prefix in _ref:
                        return match.group(0)  # keep as-is, don't expand
                _full = (_repo_root / _ref).resolve()
                try:
                    _full.relative_to(_repo_root)
                except ValueError:
                    return match.group(0)  # outside repo — keep as-is
                if _full.is_file():
                    try:
                        _content = _full.read_text(encoding="utf-8")[:5000]
                        _lines = _content.count("\n") + 1
                        return (
                            f"\n[FILE: {_ref} ({_lines} lines)]\n"
                            f"{_content}\n"
                            f"[/FILE]\n"
                        )
                    except Exception:
                        return match.group(0)
                return match.group(0)  # dir / not found / binary → keep as-is

            return _AT_RE.sub(_resolve_one, text)

        def _run_and_notify():
            # ── Hot-reload: re-read settings if they changed on disk ──
            self._maybe_reload_rules()

            # ── Resolve @mentions in the prompt ──
            _resolved_prompt = _resolve_mentions(prompt, self.repo_path)

            # ── Apply pending model switch ──
            _pending = self._runtime.pop_pending_model(session_id)
            if _pending:
                _model, _provider = _pending
                logger.info("Applying model switch — session=%s model=%s provider=%s",
                            session_id[:8], _model, _provider)
                from llm.router import create_backend_from_config
                self._backend = create_backend_from_config({
                    "provider": _provider or self._config.llm.provider,
                    "model": _model,
                    "api_key": self._config.llm.api_key or None,
                    "base_url": self._config.llm.base_url or None,
                    "max_tokens": self._config.llm.max_tokens,
                    "timeout_seconds": self._config.llm.timeout_seconds,
                })

            # ── Apply pending effort/thinking/permission_mode ──
            _pending_effort = self._runtime.pop_pending_effort(session_id)
            _pending_thinking = self._runtime.pop_pending_thinking(session_id)
            _pending_perm = self._runtime.pop_pending_permission_mode_override(session_id)
            _effective_perm = _pending_perm or "acceptEdits"

            # ── Build web_confirm_callback for this session ──
            _web_cb = self._build_web_confirm_callback(session_id)
            self._runtime.set_web_confirm_callback(session_id, _web_cb)

            # Register agent name for stats tracking
            if self._event_bus is not None and self._event_bus.recorder is not None:
                self._event_bus.recorder.set_session_agent(session_id, agent_name)

            try:
                result = self._runtime.run_session(
                    session_id=session_id,
                    agent_name=agent_name,
                    task_description=_resolved_prompt,
                    intent=resolved_intent,
                    inject_rules=list(self._loaded_rules),
                    inject_permission_mode=_effective_perm,
                )
                # Push completion event
                if self._event_bus is not None:
                    if _is_plan:
                        # Save initial plan revision
                        if hasattr(self, '_plan_revisions') and result.summary:
                            try:
                                _existing = self._plan_revisions.list_revisions(session_id)
                                if not _existing:
                                    self._plan_revisions.append_revision(
                                        session_id, result.summary,
                                    )
                            except Exception:
                                pass
                        # Contract comes from ExitPlanMode tool metadata —
                        # structured, no regex parsing needed.
                        _contract = result.contract
                        if not _contract:
                            _pc = getattr(self._registry, '_pending_plan_contract', None)
                            if _pc:
                                _contract = _pc
                                self._registry._pending_plan_contract = None
                        # Get revision count from session metadata
                        _rec = self.session_service.get_session(session_id)
                        _revision = _rec.metadata.get("plan_revision", 0) if _rec and _rec.metadata else 0
                        from server.events import WsPlanReady
                        self._event_bus.publish_typed(session_id, WsPlanReady(
                            plan_text=result.summary, contract=_contract,
                            revision=_revision, max_revisions=5,
                            result={
                                "summary": result.summary,
                                "steps_taken": result.steps_taken,
                                "total_tokens": result.total_tokens,
                            },
                        ))
                    else:
                        from server.events import WsStatus
                        self._event_bus.publish_typed(session_id, WsStatus(
                            status="completed",
                            result={
                                "summary": result.summary,
                                "steps_taken": result.steps_taken,
                                "total_tokens": result.total_tokens,
                            },
                        ))
            except Exception as exc:
                logger.exception("Async chat failed for session %s", session_id)
                if self._event_bus is not None:
                    self._event_bus.publish_raw(session_id, {
                        "type": "status",
                        "status": "failed",
                        "error": str(exc),
                    })
        import threading
        thread = threading.Thread(target=_run_and_notify, daemon=True)
        thread.start()

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
                msgs = self._session_service.get_messages(session_id)
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
                    from llm.base import LLMMessage as _LLMMsg
                    self._storage.append_message(session_id, _LLMMsg(
                        role="user",
                        content=f"[AUTOCOMPACT RECOVERY]\n{_recovery}",
                    ))

                if self._event_bus is not None:
                    self._event_bus.publish_raw(session_id, {
                        "type": "status",
                        "status": "compacted",
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

    # ── Cancel ────────────────────────────────────────────────────────────

    def cancel_session(self, session_id: str, detail: str = "") -> bool:
        """Cancel a running session via its cancellation token.

        Args:
            session_id: The session to cancel.
            detail: Human-readable reason.

        Returns:
            bool: True if an active cancellation token was found and signalled.
        """
        return self._runtime.cancel_session(session_id, detail=detail)

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
        """Release resources. Called on app shutdown."""
        logger.info("AgentService shutting down")
        # Cancel memory maintenance
        if self._memory_stop_event is not None:
            self._memory_stop_event.set()
        if self._memory_maintenance_task is not None:
            self._memory_maintenance_task.cancel()
            try:
                await self._memory_maintenance_task
            except asyncio.CancelledError:
                pass
        # Disconnect MCP servers
        if self._mcp_registry is not None:
            try:
                await self._mcp_registry.disconnect_all()
                logger.info("MCP: disconnected %d servers", len(self._mcp_registry.server_names))
            except Exception:
                logger.warning("MCP shutdown failed", exc_info=True)
        # Cancel background runs
        with self._runtime._background_runs_lock:
            for (sid, gen), thread in list(self._runtime._background_runs.items()):
                logger.debug("Cancelling background run: session=%s gen=%d", sid[:8], gen)
        self._runtime._cancellation_tokens.clear()
