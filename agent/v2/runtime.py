"""V2 Session Runtime — fork-based multi-agent orchestration."""

from __future__ import annotations

import copy
import logging
from pathlib import Path

from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.task import RunResult, RunStatus, Task, TaskIntent
from agent.v2.agent_registry import AgentRegistryV2
from agent.v2.models import (
    AgentDefinition,
    ForkResult,
    ForkStatus,
    SessionMode,
    SessionStatus,
)
from agent.v2.session_store import SessionStore
from agent.v2.subagent import fork_subagent
from context.history import ConversationHistory
from llm.base import LLMBackend, LLMMessage
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


class SessionRuntime:
    """V2 session runtime with fork-based subagent orchestration.

    Coordinator agents (build, plan) carry the `task` tool and can
    dispatch fork subagents.  Each fork runs in a fresh context with
    tools restricted to its agent definition allow-list.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        backend: LLMBackend,
        base_registry: ToolRegistry,
        agent_registry: AgentRegistryV2,
        root_agent_config: AgentConfig,
        log_dir: str,
        memory_context=None,
        hook_dispatcher=None,
        mcp_integration=None,
        event_callback=None,
    ) -> None:
        self._store = store
        self._backend = backend
        self._base_registry = base_registry
        self._agent_registry = agent_registry
        self._root_agent_config = root_agent_config
        self._log_dir = log_dir
        self._memory_context = memory_context
        self._hook_dispatcher = hook_dispatcher
        self._mcp_integration = mcp_integration
        self._event_callback = event_callback

        # ── Circuit Breaker (code-level, not prompt-based) ──
        from agent.circuit_breaker import CircuitBreaker
        self._circuit_breaker = CircuitBreaker()

        # ── P1-6: Dynamic Capability Registry ──
        from agent.capability_registry import CapabilityRegistry
        self._capability_registry = CapabilityRegistry()
        # Register all builtin tools from the base registry
        self._capability_registry.register_bulk(self._base_registry.tool_names)
        # Wire the registry into the base ToolRegistry for physical interception
        self._base_registry._capability_registry = self._capability_registry
        # Mark MCP tools as UNAVAILABLE if the bridge failed to connect
        self._sync_mcp_capabilities()

    @property
    def agent_registry(self) -> AgentRegistryV2:
        return self._agent_registry

    @property
    def circuit_breaker(self):
        return self._circuit_breaker

    @property
    def capability_registry(self):
        return self._capability_registry

    def _require_project_scope(self, repo_path: str) -> str:
        """Normalize and verify a repo against this Runtime's registry scope."""
        normalized = str(Path(repo_path).expanduser().resolve())
        if self._agent_registry.project_dir != normalized:
            raise ValueError(
                "Agent registry project scope does not match the execution repo: "
                f"registry={self._agent_registry.project_dir!r}, repo={normalized!r}"
            )
        return normalized

    def get_session_repo_path(self, session_id: str) -> str:
        """Return a verified parent-session project root or fail closed."""
        session = self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown v2 session: {session_id}")
        return self._require_project_scope(session.repo_path)

    # ── Root session ──

    def create_root_session(
        self,
        *,
        agent_name: str,
        repo_path: str,
        title: str,
        metadata: dict | None = None,
    ):
        spec = self._agent_registry.get(agent_name)
        normalized_repo = self._require_project_scope(repo_path)
        return self._store.create_session(
            agent_name=agent_name,
            mode=SessionMode.PRIMARY,
            repo_path=normalized_repo,
            title=title,
            metadata=metadata or {},
        )

    def run_session(
        self,
        session_id: str,
        *,
        agent_name: str,
        task_description: str,
        intent: TaskIntent | str | None = None,
        messages: list[LLMMessage] | None = None,
        max_steps_override: int | None = None,        # deprecated: use contract
        budget_tokens_override: int | None = None,    # deprecated: use contract
        contract: "TaskContract | None" = None,
    ) -> RunResult:
        session = self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown v2 session: {session_id}")
        self._require_project_scope(session.repo_path)

        # The selected agent is an explicit entrypoint decision. Runtime does
        # not override it by interpreting task prose.
        _effective_agent = agent_name

        # ── Phase 7: State finalization gate — status update is NOT a step in the flow,
        #     it's an inevitable consequence. try/finally ensures convergence. ──
        self._store.update_status(session_id, SessionStatus.RUNNING)
        result: RunResult | None = None

        try:
            from agent.v2.agent_factory import AgentFactory
            _assembly = AgentFactory.create(
                agent_name=_effective_agent,
                backend=self._backend,
                base_registry=self._base_registry,
                agent_registry=self._agent_registry,
                root_agent_config=self._root_agent_config,
                memory_context=self._memory_context,
                session=session,
                circuit_breaker=self._circuit_breaker,
                runtime=self,
                mcp_tool_names=self._mcp_tool_names_for_spec(
                    self._agent_registry.get(_effective_agent)
                ),
            )
            spec = _assembly.spec
            effective_intent = TaskIntent(intent) if intent is not None else spec.intent
            _eff_contract = contract if contract is not None else _assembly.contract
            agent = _assembly.agent
            agent_cfg = _assembly.agent_cfg

            persisted_messages = self._store.list_messages(session_id)
            if messages:
                for message in messages:
                    self._store.append_message(session_id, message)
                persisted_messages = self._store.list_messages(session_id)
            elif not persisted_messages:
                self._store.append_message(session_id, LLMMessage(role="user", content=task_description))
                persisted_messages = self._store.list_messages(session_id)

            history = ConversationHistory(max_messages=agent_cfg.history_max_messages)
            injected_messages = self._build_runtime_messages(spec, task_description)
            history.add_many(injected_messages + persisted_messages)
            agent._pending_history = history

            task = Task(
                description=task_description,
                repo_path=session.repo_path,
                intent=effective_intent,
                max_steps=(max_steps_override or _eff_contract.max_steps if _eff_contract else agent_cfg.max_steps),
                budget_tokens=(budget_tokens_override or _eff_contract.budget_tokens if _eff_contract else agent_cfg.budget_tokens),
                metadata={
                    "entrypoint": "v2",
                    "mode": f"v2-{agent_name}",
                    "session_id": session_id,
                    "parent_session_id": session.parent_id,
                    "root_session_id": session.root_id,
                    "agent_name": agent_name,
                    "v2_bypass_path_scope_policy": True,
                    "v2_disable_legacy_analysis_prompting": True,
                    "completion_requires": dict(spec.completion_requires),
                    "required_tools": sorted(spec.required_tools),
                },
            )

            self._fire_hook("SessionStart", session_id=session_id)

            # Runtime-injected messages are also in history. Counting only DB
            # messages re-appends old history and can split native tool pairs.
            initial_count = len(history)
            with EventLog.create(task, log_dir=self._log_dir) as log:
                if self._event_callback is not None:
                    original_append = log._append

                    def _append_and_emit(event):
                        original_append(event)
                        try:
                            self._event_callback(event)
                        except Exception:
                            logger.debug("V2 event callback failed", exc_info=True)

                    log._append = _append_and_emit
                result = agent.run(task, log)

            for message in history.to_list()[initial_count:]:
                self._store.append_message(session_id, message)

            return result
        finally:
            # ── Phase 7: State convergence — ALWAYS runs, regardless of path ──
            if result is not None:
                if result.is_success():
                    self._store.set_summary(
                        session_id, result.summary, status=SessionStatus.COMPLETED
                    )
                else:
                    self._store.update_status(
                        session_id,
                        SessionStatus.FAILED,
                        error=result.error or result.summary,
                    )
                    self._store.set_summary(
                        session_id, result.summary, status=SessionStatus.FAILED
                    )
            self._fire_hook("Stop", session_id=session_id)

    # ── Fork subagent ──

    def fork_session(
        self,
        *,
        parent_session_id: str,
        definition: AgentDefinition,
        description: str,
        prompt: str,
    ) -> ForkResult:
        """Dispatch a fork subagent.

        The subagent runs in a fresh context — no parent history inherited.
        Tools are restricted to the agent definition's allow-list.
        Only the final summary is returned to the caller.

        The parent session is the sole source for project and root scope.
        """
        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Unknown v2 session: {parent_session_id}")
        _repo = self._require_project_scope(parent.repo_path)
        child = self._store.create_session(
            agent_name=definition.name,
            mode=SessionMode.SUBAGENT,
            repo_path=_repo,
            title=description[:80] or definition.name,
            parent_id=parent.id,
            root_id=parent.root_id,
            metadata={
                "entrypoint": "task",
                "isolation": definition.isolation.value,
                "intent": definition.intent.value,
            },
        )
        self._store.append_message(child.id, LLMMessage(role="user", content=prompt))
        self._store.update_status(child.id, SessionStatus.RUNNING)

        fork_result: ForkResult | None = None

        def _persist_child_messages(messages: list[LLMMessage]) -> None:
            for message in messages:
                self._store.append_message(child.id, message)

        try:
            fork_result = fork_subagent(
                agent_id=child.id,
                definition=definition,
                prompt=prompt,
                repo_path=_repo,
                base_registry=self._base_registry,
                backend=self._backend,
                log_dir=self._log_dir,
                root_agent_config=self._root_agent_config,
                hook_dispatcher=self._hook_dispatcher,
                message_sink=_persist_child_messages,
            )
            self._store.append_message(
                child.id,
                LLMMessage(role="assistant", content=fork_result.summary),
            )
            return fork_result
        except Exception as exc:
            self._store.append_message(
                child.id,
                LLMMessage(role="assistant", content=f"Subagent failed: {exc}"),
            )
            raise
        finally:
            if fork_result is None or fork_result.status is ForkStatus.FAILED:
                summary = (
                    fork_result.summary if fork_result is not None
                    else "Subagent execution failed before producing a result"
                )
                error = (
                    (fork_result.error or summary)
                    if fork_result is not None else summary
                )
                self._store.update_status(child.id, SessionStatus.FAILED, error=error)
                self._store.set_summary(
                    child.id, summary, status=SessionStatus.FAILED,
                )
            elif fork_result.status is ForkStatus.PARTIAL:
                self._store.set_summary(
                    child.id, fork_result.summary, status=SessionStatus.PARTIAL,
                )
            else:
                self._store.set_summary(
                    child.id, fork_result.summary, status=SessionStatus.COMPLETED,
                )

    # ── Internal helpers ──

    def _fire_hook(self, event_name: str, session_id: str = "") -> None:
        if self._hook_dispatcher is None:
            return
        from hooks.events import HookContext, HookEvent
        try:
            evt = HookEvent(event_name)
            ctx = HookContext(event=evt, session_id=session_id)
            self._hook_dispatcher.dispatch(evt, ctx)
        except Exception:
            pass

    def _build_registry_for_session(self, spec: AgentDefinition, session) -> ToolRegistry:
        """委托给 registry_builder。"""
        from agent.v2.registry_builder import build_registry_for_session
        return build_registry_for_session(
            spec, session,
            base_registry=self._base_registry,
            agent_registry=self._agent_registry,
            circuit_breaker=self._circuit_breaker,
            runtime=self,
            mcp_tool_names=self._mcp_tool_names_for_spec(spec),
        )

    def _sync_mcp_capabilities(self) -> None:
        """Sync MCP tool states into the capability registry.

        When MCP integration is absent or a server failed to connect,
        mark those tools as UNAVAILABLE so the model never sees them.
        """
        if self._mcp_integration is None:
            return
        mcp_tool_names = getattr(self._mcp_integration, "tool_names", frozenset())
        for name in mcp_tool_names:
            self._capability_registry.register(name)

        # Check for failed MCP servers
        failed_servers = getattr(self._mcp_integration, "failed_servers", None)
        if failed_servers:
            for server_name, reason in failed_servers.items():
                server_tools = getattr(self._mcp_integration, "server_tools", {}).get(server_name, [])
                for tool_name in server_tools:
                    self._capability_registry.mark_unavailable(
                        tool_name, f"MCP server '{server_name}': {reason}",
                    )

    def _mcp_tool_names_for_spec(self, spec: AgentDefinition) -> frozenset[str]:
        if self._mcp_integration is None:
            return frozenset()
        if spec.name not in {"build", "general", "coordinator"}:
            return frozenset()
        # P1-6: Only return MCP tools that are ACTIVE in the capability registry
        raw_names = getattr(self._mcp_integration, "tool_names", frozenset())
        from agent.capability_registry import CapabilityState
        return frozenset(
            n
            for n in raw_names
            if self._capability_registry.state_for(n) is CapabilityState.AVAILABLE
        )

    def _build_agent_config(self, spec: AgentDefinition) -> AgentConfig:
        cfg = copy.copy(self._root_agent_config)
        cfg.circuit_breaker = self._circuit_breaker
        if spec.mode != SessionMode.PRIMARY:
            cfg.max_steps = min(cfg.max_steps, spec.max_turns)
            cfg.compact_history = False
            cfg.stream = False
            cfg.stream_callback = None
            cfg.thought_callback = None
            cfg.token_callback = None
        return cfg

    def _build_runtime_messages(self, spec: AgentDefinition, task_description: str) -> list[LLMMessage]:
        """委托给 runtime_prompt_builder。"""
        from agent.v2.runtime_prompt_builder import build_runtime_messages
        return build_runtime_messages(
            spec, task_description,
            agent_registry=self._agent_registry,
        )


def default_session_db_path(repo_path: str) -> str:
    from runtime.state_paths import ProjectStatePaths

    return str(ProjectStatePaths.for_project(repo_path).sessions_db)


def memory_freshness_text(name: str, store) -> str:
    """Return a freshness warning for a memory file based on mtime.

    Returns '' for fresh files (<=1 day), relative age warning for older.
    """
    import os as _os
    from datetime import datetime as _datetime

    try:
        path = store._file_path(name)
        if not path.exists():
            return ""
        mtime = _datetime.fromtimestamp(_os.path.getmtime(path))
        age_days = (_datetime.now() - mtime).days
        if age_days <= 1:
            return ""
        return f"{age_days} days ago — verify against current code"
    except Exception:
        return ""
