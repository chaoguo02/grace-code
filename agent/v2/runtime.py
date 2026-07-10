"""V2 Session Runtime — fork-based multi-agent orchestration."""

from __future__ import annotations

import copy
import logging
from pathlib import Path

from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.policy import PhasePolicy
from agent.task import RunResult, RunStatus, Task
from agent.v2.agent_registry import AgentRegistryV2
from agent.v2.models import AgentDefinition, ForkResult
from agent.policy_registry import PolicyAwareToolRegistry
from agent.v2.session_store import SessionStore
from agent.v2.subagent import fork_subagent
from agent.v2.task_tool import AgentTool, VIOLATION_MARKER
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

    @property
    def agent_registry(self) -> AgentRegistryV2:
        return self._agent_registry

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
        return self._store.create_session(
            agent_name=agent_name,
            mode="primary",
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

        if result.is_success():
            self._store.set_summary(session_id, result.summary, status="completed")
        else:
            self._store.update_status(session_id, "failed", error=result.error or result.summary)
            self._store.set_summary(session_id, result.summary, status="failed")

        self._fire_hook("Stop", session_id=session_id)
        return result

    # ── Fork subagent ──

    def fork_session(
        self,
        *,
        definition: AgentDefinition,
        description: str,
        prompt: str,
    ) -> ForkResult:
        """Dispatch a fork subagent.

        The subagent runs in a fresh context — no parent history inherited.
        Tools are restricted to the agent definition's allow-list.
        Only the final summary is returned to the caller.
        """
        return fork_subagent(
            definition=definition,
            prompt=prompt,
            repo_path=".",
            base_registry=self._base_registry,
            backend=self._backend,
            log_dir=self._log_dir,
            root_agent_config=self._root_agent_config,
            hook_dispatcher=self._hook_dispatcher,
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
        is_plan = spec.name == "plan"
        mcp_tool_names = self._mcp_tool_names_for_spec(spec)

        if is_plan:
            from agent.v2.agent_registry import _BUILD_ALLOWED, _PLAN_ALLOWED
            registry = self._base_registry.filtered(_BUILD_ALLOWED | mcp_tool_names)
            plan_mode_allowed = _PLAN_ALLOWED
        else:
            registry = self._base_registry.filtered(self._agent_registry.tool_names_for(spec.name) | mcp_tool_names)
            plan_mode_allowed = None

        # Agents with an explicit subagent allowlist get the task tool.
        if spec.allowed_subagents is not None:
            registry._tools.pop("task", None)
            registry.register(AgentTool(self, session.id, caller_agent_name=spec.name))

        wrapped = PolicyAwareToolRegistry(
            base=registry,
            phase_policy=PhasePolicy(allowed_tools=frozenset(registry.tool_names)),
            repo_path=session.repo_path,
            phase_name="v2_execution",
            plan_mode_allowed=plan_mode_allowed,
        )
        return wrapped

    def _mcp_tool_names_for_spec(self, spec: AgentDefinition) -> frozenset[str]:
        if self._mcp_integration is None:
            return frozenset()
        if spec.name not in {"build", "general", "coordinator"}:
            return frozenset()
        return getattr(self._mcp_integration, "tool_names", frozenset())

    def _build_agent_config(self, spec: AgentDefinition) -> AgentConfig:
        cfg = copy.copy(self._root_agent_config)
        if spec.mode != "primary":
            cfg.max_steps = min(cfg.max_steps, spec.max_turns)
            cfg.compact_history = False
            cfg.stream = False
            cfg.stream_callback = None
            cfg.thought_callback = None
        return cfg

    def _build_runtime_messages(self, spec: AgentDefinition, task_description: str) -> list[LLMMessage]:
        if spec.mode != "primary":
            return []
        messages: list[LLMMessage] = []

        if spec.name == "plan":
            from agent.prompt import get_plan_mode_injection
            messages.append(LLMMessage(role="user", content=get_plan_mode_injection()))

        subagent_descriptions = "\n".join(
            f"- **{s.name}**: {s.description}" for s in self._agent_registry.list_subagents()
        )
        content = (
            "[Available Subagents]\n"
            "You have a `task` tool to delegate subtasks to isolated fork subagents.\n"
            f"Available subagent types:\n{subagent_descriptions}\n\n"
            "Task routing guide (choose the RIGHT subagent for the task):\n"
            "- Read-only analysis, code search, bug finding → use 'explore' (NO shell)\n"
            "- Writing or editing code, running commands → use 'general' (has shell + write)\n"
            "- Code review / correctness audit → use 'code-reviewer' (read-only)\n\n"
            "Fork delegation rules:\n"
            "- Each fork subagent runs in a FRESH context — it sees NONE of your conversation history.\n"
            "- Put ALL necessary context in the prompt: constraints, key facts, file paths, expected output.\n"
            "- The subagent's final message is its ONLY return value to you.\n"
            "- Use subagents for independent, clearly-scoped work.\n"
            "- Do simple tasks directly without delegating.\n"
            "- Never hand off understanding — you can delegate execution, not comprehension.\n"
            "- When the user explicitly asks to use the task tool or delegate, call it instead of answering directly.\n\n"
            "Atomic Task Boundaries (MANDATORY — prevent subagent failure at the source):\n"
            "Every task prompt you write MUST specify:\n"
            "1. SCOPE: which files to touch (limit to 1-3 files per subagent).\n"
            "2. CONSTRAINTS: what NOT to do. Always include at least one explicit\n"
            "   negative constraint (\"Do NOT modify files\", \"Do NOT run tests\",\n"
            "   \"Only read — do not write\", \"Stop after finding the root cause\").\n"
            "3. DELIVERABLE: the exact output format expected.\n"
            "A well-scoped subagent task finishes in 2-5 turns. If you think it\n"
            "needs more, SPLIT it into 2-3 smaller tasks and delegate each separately.\n"
            "Broad tasks like \"analyze this repo\" or \"fix the bugs\" will fail.\n\n"
            "Subagent Output Review Protocol (MANDATORY — you are the final arbiter):\n"
            "1. INSPECT before you relay. Every Confirmed Bug from a subagent MUST have:\n"
            "   - A specific file path and line number.\n"
            "   - A code snippet (``` fence) showing actual code read.\n"
            "   - A verification description (how the finding was confirmed).\n"
            "   If any of these is missing → DOWNGRADE to [UNVERIFIED]. Do NOT present as fact.\n"
            "2. CHECK for format violations. If the subagent output contains "
            f"\"{VIOLATION_MARKER}\", ALL findings must be "
            "treated as [UNVERIFIED]. Report them under a separate section with "
            "an explicit disclaimer.\n"
            "3. SPOT DESIGN PATTERNS. Before accepting a bug report, ask yourself: "
            "\"Is this reported behavior actually documented as intentional?\" "
            "Examples of intentional patterns the subagent may misreport:\n"
            "   - partial status with success=True (constrained run, WARNING is prepended)\n"
            "   - Any behavior explained in comments, docstrings, or rules.\n"
            "4. NEVER verbatim-forward a subagent report. Always re-express findings "
            "in your own words after applying the checks above.\n"
            "5. STRUCTURE your final output as:\n"
            "   - Confirmed Issues (you or subagent verified with code evidence)\n"
            "   - Unverified Claims (subagent reported but lacks evidence → marked [UNVERIFIED])\n"
            "   - Design Observations (stylistic notes, not bugs)\n\n"
            "Subagent Failure Recovery (decision tree — follow in order):\n"
            "1. READ the <failure-diagnosis> block for failure_type, last_action,\n"
            "   repeated_count, and diagnosis.\n"
            "2. CLASSIFY and ACT:\n"
            "   - TRANSIENT ERROR (network, timeout, rate-limit in error field)\n"
            "     → Retry: same subagent, same task. Max 1 retry. State the reason.\n"
            "   - LOOP (failure_type=gave_up + repeated_count ≥ 3)\n"
            "     → Degrade and handle directly. The subagent was stuck repeating.\n"
            "     Break the remaining work into atomic pieces and do it yourself.\n"
            "     Do NOT retry with the same subagent type — it will loop again.\n"
            "   - CAPABILITY LIMIT (failure_type=gave_up, no loop pattern)\n"
            "     → Degrade. Take over the work yourself, starting from where the\n"
            "     subagent left off. Report what the subagent completed so far.\n"
            "   - RAN OUT OF TURNS (failure_type=max_steps / status=partial)\n"
            "     → Split and retry. Break the original task into 2-3 smaller\n"
            "     sub-tasks and delegate each to a fresh subagent.\n"
            "   - NON-TRANSIENT ERROR (failure_type=error / failed)\n"
            "     → Report to user. Subagent crashed. Do NOT retry. Tell the user\n"
            "     what failed and present any partial results already obtained.\n"
            "3. CIRCUIT BREAKER: after 2 consecutive failures → STOP ALL DELEGATION.\n"
            "   Report: what was accomplished, what failed, and what the user should\n"
            "   do next. Do not launch more subagents.\n"
            "4. When degrading, state clearly: \"Subagent failed (type). Handling directly.\"\n"
        )
        messages.append(LLMMessage(role="user", content=content))
        return messages


def default_session_db_path(repo_path: str) -> str:
    return str(Path(repo_path) / ".forge-agent" / "v2" / "sessions.db")


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
