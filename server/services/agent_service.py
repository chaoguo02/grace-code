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

        # For web MVP we use PROMPT approval mode (user sees tool calls in UI)
        self._registry = build_registry(
            self._config,
            repo_path=self.repo_path,
            approval_mode="prompt",
        )

        # ── 4. Agent registry ──
        from agent.session.agent_registry import AgentRegistryV2

        self._agent_registry = AgentRegistryV2(project_dir=self.repo_path)

        # ── 5. Session store + StorageBackend ──
        from agent.session import default_session_db_path
        from agent.session.session_store import SessionStore
        from app.storage.sqlite import SqliteStorageBackend

        db_path = default_session_db_path(self.repo_path)
        from core.state_paths import migrate_legacy_session_db

        migrate_legacy_session_db(self.repo_path, db_path)
        self._store = SessionStore(db_path)
        self._storage: SqliteStorageBackend = SqliteStorageBackend(db_path)

        # ── SessionService (uses StorageBackend, not raw SessionStore) ──
        self.session_service = SessionService(self._storage)

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
            stream=False,  # Web MVP: non-streaming for simplicity
            confirm_dangerous=False,
        )

    # ── Session management ────────────────────────────────────────────────

    def ensure_root_session(self) -> str:
        """Lazily create a root session if one doesn't exist.

        Returns:
            str: The root session ID.
        """
        if self._root_session_id is None:
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

        def _run_and_notify():
            try:
                result = self._runtime.run_session(
                    session_id=session_id,
                    agent_name=agent_name,
                    task_description=prompt,
                    intent=resolved_intent,
                )
                # Push completion event
                if self._event_bus is not None:
                    if _is_plan:
                        self._event_bus.publish_raw(session_id, {
                            "type": "plan_ready",
                            "status": "plan_ready",
                            "plan_text": result.summary,
                            "result": {
                                "summary": result.summary,
                                "steps_taken": result.steps_taken,
                                "total_tokens": result.total_tokens,
                            },
                        })
                    else:
                        self._event_bus.publish_raw(session_id, {
                            "type": "status",
                            "status": "completed",
                            "result": {
                                "summary": result.summary,
                                "steps_taken": result.steps_taken,
                                "total_tokens": result.total_tokens,
                            },
                        })
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
