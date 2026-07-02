from __future__ import annotations

import copy
from pathlib import Path

from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.policy import PhasePolicy
from agent.task import RunResult, RunStatus, Task
from agent.v2.agent_registry import AgentRegistryV2
from agent.policy_registry import PolicyAwareToolRegistry
from agent.v2.models import ChildSessionResult
from agent.v2.session_store import SessionStore
from agent.v2.task_tool import TaskToolV2
from context.history import ConversationHistory
from llm.base import LLMBackend, LLMMessage
from tools.base import ToolRegistry


_CHILD_SUMMARY_RULE = (
    "Your final answer is returned to the parent as a summary-only tool result. "
    "The parent does not automatically inherit your full reasoning or full tool history. "
    "Make your final summary standalone and directly useful."
)


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
        hook_dispatcher=None,
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
        self._hook_dispatcher = hook_dispatcher

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
        max_steps_override: int | None = None,
        budget_tokens_override: int | None = None,
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
            max_steps=max_steps_override or agent_cfg.max_steps,
            budget_tokens=budget_tokens_override or agent_cfg.budget_tokens,
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
        self._fire_hook("SessionStart", session_id=session_id)

        initial_count = len(persisted_messages)
        with EventLog.create(task, log_dir=self._log_dir) as log:
            result = agent.run(task, log)

        for message in history.to_list()[initial_count:]:
            self._store.append_message(session_id, message)

        if result.is_success():
            self._store.set_summary(session_id, result.summary, status="completed")
        else:
            self._store.update_status(session_id, "failed", error=result.error or result.summary)
            self._store.set_summary(session_id, result.summary, status="failed")

        self._fire_hook("Stop", session_id=session_id)
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
        self._fire_hook("SubagentStop", session_id=child.id)
        return self._build_child_session_result(child.id, result)

    def _fire_hook(self, event_name: str, session_id: str = "") -> None:
        """Fire a lifecycle hook event if dispatcher is configured."""
        if self._hook_dispatcher is None:
            return
        from hooks.events import HookContext, HookEvent

        try:
            evt = HookEvent(event_name)
            ctx = HookContext(event=evt, session_id=session_id)
            self._hook_dispatcher.dispatch(evt, ctx)
        except Exception:
            pass

    def _build_registry_for_session(self, spec, session) -> ToolRegistry:
        # Plan agent：注册全部工具（模型能看到定义），通过 plan_mode_allowed 拦截写操作
        is_plan = spec.name == "plan"
        if is_plan:
            from agent.v2.agent_registry import _BUILD_ALLOWED, _PLAN_ALLOWED
            registry = self._base_registry.filtered(_BUILD_ALLOWED)
            plan_mode_allowed = _PLAN_ALLOWED
        else:
            registry = self._base_registry.filtered(spec.allowed_tools)
            plan_mode_allowed = None
        if spec.allow_task_tool:
            registry.register(TaskToolV2(self, session.id))
        wrapped = PolicyAwareToolRegistry(
            base=registry,
            phase_policy=PhasePolicy(allowed_tools=frozenset(registry.tool_names)),
            repo_path=session.repo_path,
            phase_name="v2_execution",
            plan_mode_allowed=plan_mode_allowed,
        )
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
        messages: list[LLMMessage] = []

        # Plan 模式下注入只读约束（ref: Claude Code EnterPlanMode 返回指令）
        if spec.name == "plan":
            from agent.prompt import get_plan_mode_injection
            messages.append(LLMMessage(role="user", content=get_plan_mode_injection()))

        subagent_descriptions = "\n".join(
            f"- {s.name}: {s.description}" for s in self._agent_registry.list_subagents()
        )
        content = (
            "[V2 Available Subagents]\n"
            "You have a `task` tool to delegate subtasks to isolated child sessions.\n"
            f"Available subagent types:\n{subagent_descriptions}\n\n"
            "Guidelines:\n"
            "- Each child session is stateless. Put ALL necessary context in the prompt.\n"
            "- The child's final summary is the only thing returned to you.\n"
            "- Use delegation for independent, clearly-scoped work.\n"
            "- Do simple tasks directly without delegating."
        )
        messages.append(LLMMessage(role="user", content=content))
        return messages

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
        messages = [LLMMessage(role="user", content=content)]

        # Inject memory context so child agents know project rules and conventions
        memory_section = self._build_child_memory_context()
        if memory_section:
            messages.append(LLMMessage(role="user", content=memory_section))

        return messages

    def _build_child_memory_context(self) -> str:
        """
        Build a compact memory snippet for child agents.

        Injects:
        - ALL procedural/feedback memories (global rules, no freshness warning)
        - Top 5 semantic/project memories by recency (with freshness warning if >1 day old)

        Returns empty string if no memory_context is configured or no memories exist.
        """
        if self._memory_context is None:
            return ""

        try:
            store = self._memory_context._store
            summaries = store.list_memories()
        except Exception:
            return ""

        if not summaries:
            return ""

        # Separate: user/feedback (always inject) vs project/reference (top-5 by recency)
        _GLOBAL_TYPES = {"user", "feedback"}
        global_mems = [s for s in summaries if s.type in _GLOBAL_TYPES]
        project_mems = [s for s in summaries if s.type not in _GLOBAL_TYPES]

        # Sort project memories by updated_at descending, take top 5
        project_mems.sort(key=lambda s: s.updated_at or "", reverse=True)
        project_mems = project_mems[:5]

        lines: list[str] = []
        lines.append("[Memory Context]")
        lines.append("The following project knowledge applies to your work:\n")

        # Read full content for global memories (they're short rules — no freshness limit)
        for s in global_mems:
            try:
                mem = store.read_memory(s.name)
                if mem and mem.content.strip():
                    lines.append(f"**{s.name}** ({s.type}): {mem.content.strip()}")
                    lines.append("")
            except Exception:
                continue

        # For project memories, include description + freshness warning
        if project_mems:
            lines.append("Project knowledge:")
            for s in project_mems:
                freshness = self._memory_freshness_text(s.name, store)
                desc = f"- {s.name}: {s.description}"
                if freshness:
                    desc += f" [{freshness}]"
                lines.append(desc)

        # Don't inject if nothing meaningful was collected
        content = "\n".join(lines)
        if content.strip() == "[Memory Context]\nThe following project knowledge applies to your work:":
            return ""

        return content

    @staticmethod
    def _memory_freshness_text(name: str, store) -> str:
        """
        Generate freshness warning for a memory file based on mtime.

        Aligned with Claude Code's memoryFreshnessText():
        - <=1 day old: no warning (fresh)
        - >1 day old: relative age warning ("X days ago")

        Uses relative time ("47 days ago") instead of ISO timestamps because
        models reason better about staleness with relative time expressions.
        """
        import os
        from datetime import datetime

        try:
            path = store._file_path(name)
            if not path.exists():
                return ""
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            age_days = (datetime.now() - mtime).days
            if age_days <= 1:
                return ""
            return f"{age_days} days ago — verify against current code"
        except Exception:
            return ""

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
