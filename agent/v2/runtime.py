from __future__ import annotations

import copy
from pathlib import Path

from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.policy import PhasePolicy
from agent.task import RunResult, RunStatus, Task
from agent.v2.agent_registry import AgentRegistryV2
from agent.v2.dynamic_registry import DynamicPolicyAwareToolRegistry
from agent.v2.models import ChildSessionResult
from agent.v2.session_store import SessionStore
from agent.v2.task_tool import TaskToolV2
from context.history import ConversationHistory
from llm.base import LLMBackend, LLMMessage
from tools.base import ToolRegistry


_DELEGATION_HINT_TOKENS = (
    "task",
    "subagent",
    "subagent_type",
    "child session",
    "explore",
    "general",
    "子会话",
    "子 session",
    "task 工具",
)

_CHILD_SUMMARY_RULE = (
    "Your final answer is returned to the parent as a summary-only tool result. "
    "The parent does not automatically inherit your full reasoning or full tool history. "
    "Make your final summary standalone and directly useful."
)
_DELEGATION_FALLBACK_DENY_TOOLS = frozenset({
    "file_read",
    "file_view",
    "find_files",
    "find_symbol",
    "search_text",
})


class SessionRuntime:
    def __init__(
        self,
        *,
        store: SessionStore,
        backend: LLMBackend,
        base_registry: ToolRegistry,
        agent_registry: AgentRegistryV2,
        root_agent_config: AgentConfig,
        log_dir: str,
        child_max_steps: int = 12,
        child_budget_tokens: int = 30_000,
        memory_context=None,
    ) -> None:
        self._store = store
        self._backend = backend
        self._base_registry = base_registry
        self._agent_registry = agent_registry
        self._root_agent_config = root_agent_config
        self._log_dir = log_dir
        self._child_max_steps = child_max_steps
        self._child_budget_tokens = child_budget_tokens
        self._memory_context = memory_context
        self._session_dynamic_denied_tools: dict[str, frozenset[str]] = {}
        self._active_registries: dict[str, DynamicPolicyAwareToolRegistry] = {}

    @property
    def agent_registry(self) -> AgentRegistryV2:
        return self._agent_registry

    def create_root_session(
        self,
        *,
        agent_name: str,
        repo_path: str,
        title: str,
        metadata: dict | None = None,
    ):
        spec = self._agent_registry.get(agent_name)
        return self._store.create_session(
            agent_name=agent_name,
            mode=spec.mode,
            repo_path=repo_path,
            title=title,
            metadata=metadata or {},
        )

    def run_session(
        self,
        session_id: str,
        *,
        agent_name: str,
        task_description: str,
        intent: str,
        messages: list[LLMMessage] | None = None,
    ) -> RunResult:
        session = self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown v2 session: {session_id}")

        spec = self._agent_registry.get(agent_name)
        registry = self._build_registry_for_session(spec, session)
        agent_cfg = self._build_agent_config(spec)
        agent = ReActAgent(
            self._backend,
            registry,
            agent_cfg,
            memory_context=self._memory_context if spec.mode == "primary" else None,
        )

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
            intent=intent,
            max_steps=agent_cfg.max_steps,
            budget_tokens=agent_cfg.budget_tokens,
            metadata={
                "entrypoint": "v2",
                "mode": f"v2-{agent_name}",
                "session_id": session_id,
                "parent_session_id": session.parent_id,
                "root_session_id": session.root_id,
                "agent_name": agent_name,
                "v2_bypass_path_scope_policy": True,
                "v2_disable_legacy_analysis_prompting": True,
            },
        )

        self._store.update_status(session_id, "running")
        initial_count = len(persisted_messages)
        try:
            with EventLog.create(task, log_dir=self._log_dir) as log:
                result = agent.run(task, log)
        finally:
            if spec.mode == "primary":
                self._active_registries.pop(session_id, None)

        for message in history.to_list()[initial_count:]:
            self._store.append_message(session_id, message)

        if result.is_success():
            self._store.set_summary(session_id, result.summary, status="completed")
        else:
            self._store.update_status(session_id, "failed", error=result.error or result.summary)
            self._store.set_summary(session_id, result.summary, status="failed")
        return result

    def run_child_session(
        self,
        *,
        parent_session_id: str,
        subagent_type: str,
        description: str,
        prompt: str,
    ) -> ChildSessionResult:
        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Unknown parent session: {parent_session_id}")
        spec = self._agent_registry.get(subagent_type)
        child = self._store.create_session(
            agent_name=subagent_type,
            mode=spec.mode,
            repo_path=parent.repo_path,
            title=f"{description} (@{subagent_type} subagent)",
            parent_id=parent.id,
            root_id=parent.root_id,
            metadata={
                "task_description": description,
                "subagent_type": subagent_type,
                "run_kind": "task_child",
            },
        )
        result = self.run_session(
            child.id,
            agent_name=subagent_type,
            task_description=prompt,
            intent="analysis" if subagent_type == "explore" else "edit",
            messages=[LLMMessage(role="user", content=prompt)],
        )
        return self._build_child_session_result(child.id, result)

    def _build_registry_for_session(self, spec, session) -> ToolRegistry:
        registry = self._base_registry.filtered(spec.allowed_tools)
        if spec.allow_task_tool:
            registry.register(TaskToolV2(self, session.id))
        dynamic_denied_tools = self._session_dynamic_denied_tools.get(session.id, frozenset())
        wrapped = DynamicPolicyAwareToolRegistry(
            base=registry,
            phase_policy=PhasePolicy(allowed_tools=frozenset(registry.tool_names)),
            repo_path=session.repo_path,
            phase_name="v2_execution",
            dynamic_denied_tools=dynamic_denied_tools,
        )
        if spec.mode == "primary":
            self._active_registries[session.id] = wrapped
        return wrapped

    def _build_agent_config(self, spec) -> AgentConfig:
        cfg = copy.copy(self._root_agent_config)
        if spec.mode != "primary":
            cfg.max_steps = self._child_max_steps
            cfg.budget_tokens = self._child_budget_tokens
            cfg.compact_history = False
            cfg.stream = False
            cfg.stream_callback = None
            cfg.thought_callback = None
        return cfg

    def _build_runtime_messages(self, spec, task_description: str) -> list[LLMMessage]:
        if spec.mode == "subagent":
            return self._build_child_runtime_messages(spec)
        if spec.mode != "primary":
            return []
        messages = [
            LLMMessage(
                role="user",
                content=(
                    "[V2 Primary Session Rule]\n"
                    "When you use the task tool, treat the child session result as authoritative structured data.\n"
                    "- If a child session returns status=partial or status=failed, do NOT perform the child's "
                    "exploration or file-reading work yourself.\n"
                    "- Instead, dispatch a new, more specific child session to fill the missing parts, or ask the "
                    "user for clarification if the missing scope is ambiguous.\n"
                    "- Only do direct file exploration yourself if the user explicitly changes strategy."
                ),
            )
        ]
        if not self._is_explicit_delegation_request(task_description):
            return messages
        messages.append(
            LLMMessage(
                role="user",
                content=(
                    "[V2 Delegation Rule]\n"
                    "The user explicitly asked you to use the task tool / child session model. "
                    "Your first meaningful action must be a task tool call that delegates the requested "
                    "exploration or implementation to the appropriate subagent. "
                    "Do not do your own substantive file exploration before that first task call. "
                    "After the child session returns, treat its summary as your primary source. "
                    "Do not repeat broad exploration unless the child summary is clearly insufficient. "
                    "If the child result is insufficient, dispatch another more specific child session instead of "
                    "doing the broad exploration yourself."
                ),
            ),
        )
        return messages

    def _is_explicit_delegation_request(self, task_description: str) -> bool:
        lowered = (task_description or "").lower()
        return any(token in lowered for token in _DELEGATION_HINT_TOKENS)

    def _build_child_runtime_messages(self, spec) -> list[LLMMessage]:
        if spec.name == "explore":
            content = (
                "[V2 Child Session Rule]\n"
                "You are an isolated explore child session. Complete the exploration yourself instead of "
                "leaving obvious follow-up work for the parent.\n"
                "- Prefer targeted search + focused reads over broad wandering.\n"
                "- Stop as soon as you can name the key files, functions, and call flow.\n"
                "- Your final summary must include: files inspected, the main execution path, and any "
                "specific gaps that remain.\n"
                f"- {_CHILD_SUMMARY_RULE}"
            )
        else:
            content = (
                "[V2 Child Session Rule]\n"
                "You are an isolated general child session. Try to complete the requested implementation "
                "or focused investigation yourself before handing back control.\n"
                "- Keep your work scoped to the requested task.\n"
                "- If you finish successfully, summarize the concrete changes or findings.\n"
                "- If you cannot finish, summarize the blocker precisely.\n"
                f"- {_CHILD_SUMMARY_RULE}"
            )
        return [LLMMessage(role="user", content=content)]

    def _build_child_session_result(self, session_id: str, result: RunResult) -> ChildSessionResult:
        session = self._store.get_session(session_id)
        messages = self._store.list_messages(session_id)
        artifacts = self._extract_child_artifacts(messages)
        status = self._map_child_status(result.status)
        summary = (result.summary or "").strip()
        if not summary:
            summary = "Child session finished without a summary."
        missing_info = self._child_missing_info(status, result)
        error = (result.error or "").strip()
        if session is not None and session.error and not error:
            error = session.error.strip()
        return ChildSessionResult(
            session_id=session_id,
            status=status,
            summary=summary,
            artifacts=tuple(artifacts),
            missing_info=missing_info,
            error=error,
        )

    def apply_child_result_policy(self, parent_session_id: str, child_result: ChildSessionResult) -> None:
        if child_result.status not in {"partial", "failed"}:
            return
        denied_tools = self._session_dynamic_denied_tools.get(parent_session_id, frozenset())
        next_denied = frozenset(set(denied_tools) | set(_DELEGATION_FALLBACK_DENY_TOOLS))
        self._session_dynamic_denied_tools[parent_session_id] = next_denied
        active_registry = self._active_registries.get(parent_session_id)
        if active_registry is not None:
            active_registry.set_dynamic_denied_tools(next_denied)

    def _extract_child_artifacts(self, messages: list[LLMMessage]) -> list[str]:
        artifact_paths: list[str] = []
        seen: set[str] = set()
        for message in messages:
            if message.role != "assistant" or not message.tool_calls:
                continue
            for tool_call in message.tool_calls:
                for key in ("path", "file_path", "target_path", "new_path"):
                    value = tool_call.params.get(key)
                    if not isinstance(value, str):
                        continue
                    normalized = value.strip()
                    if not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    artifact_paths.append(normalized)
        return artifact_paths

    def _map_child_status(self, run_status: RunStatus) -> str:
        if run_status == RunStatus.SUCCESS:
            return "completed"
        if run_status == RunStatus.MAX_STEPS:
            return "partial"
        return "failed"

    def _child_missing_info(self, status: str, result: RunResult) -> str:
        if status == "completed":
            return ""
        if status == "partial":
            return (
                (result.error or "").strip()
                or "Child session stopped before fully covering the requested scope."
            )
        return (
            (result.error or "").strip()
            or (result.summary or "").strip()
            or "Child session failed before producing a complete result."
        )


def default_session_db_path(repo_path: str) -> str:
    return str(Path(repo_path) / ".forge-agent" / "v2" / "sessions.db")
