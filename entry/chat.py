"""
entry/chat.py

交互对话模式。使用 SessionRuntime 执行每一轮，保持跨轮共享 history。

架构：
- ChatSession 持有 SessionRuntime，所有 agent 执行委托给 run_session()
- SessionRuntime 提供：runtime_message_source、completion_fact_check、
  plan 节流、SESSION_START hook、SQLite 持久化、try/finally 清理
- ChatSession 负责：跨轮 history、SessionState、渲染、auto-compact

用法：
    python -m entry.cli chat --repo .
"""

from __future__ import annotations

import logging
import os
import time
import sys
import uuid
from pathlib import Path

import click

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from prompts.builder import reset_prompt_usage
from entry.renderer import InlineRenderer, create_renderer
from observability import flush_observability

Renderer = InlineRenderer
RendererBase = InlineRenderer


class ChatSession:
    """跨轮对话会话。

    Phase 0b: 支持双路径初始化——
      - 传入 runtime + agent_runtime → AgentRuntime 核心执行 + SessionRuntime 回调
      - 仅传 runtime (或无) → 完全 SessionRuntime（旧路径, 保留兼容）
    """

    def __init__(
        self,
        backend,
        registry,
        config,
        repo_path: str,
        log_dir: str,
        *,
        runtime=None,
        hook_dispatcher=None,
        mcp_integration=None,
        agent_name: str = "build",
        confirm_callback=None,
        renderer=None,
        memory_store=None,
        memory_context=None,
        skill_registry=None,
        # Phase 0b: AgentRuntime 替代 SessionRuntime
        agent_runtime=None,
        conversation_store=None,
    ) -> None:
        from context.history import ConversationHistory

        self.repo_path = repo_path
        self.log_dir = log_dir
        self.config = config
        self._session_id = uuid.uuid4().hex[:12]
        self._agent_name = agent_name
        self._model = getattr(backend, "model_name", "?")
        self._provider = getattr(config.llm, "provider", "?")
        self._confirm_callback = confirm_callback
        self._skill_registry = skill_registry
        self._memory_store = memory_store
        self._memory_context = memory_context
        self._hook_dispatcher = hook_dispatcher
        self._hooks_started = False  # Phase 0b: SESSION_START hook once

        # Phase 0b Step 12: load_agent_definitions 替代 AgentRegistryV2
        from agent.session.agent_definition import load_agent_definitions
        self._agent_definitions = load_agent_definitions(project_dir=repo_path)

        from core.goal import GoalStore, goal_stop_hook
        from core.state_paths import ProjectStatePaths

        self.goal_store = GoalStore(ProjectStatePaths.for_project(repo_path).goals)
        self.goal_store.restore()
        if self._hook_dispatcher is not None:
            from hooks import (
                HookDecision,
                HookEvent,
                HookOutput,
                InternalHook,
            )

            def _goal_policy_hook(ctx):
                messages = goal_stop_hook(
                    self.goal_store,
                    list(ctx.messages or []),
                    backend_factory=self._create_goal_judge_backend,
                )
                if not messages:
                    return None
                return HookOutput(
                    decision=HookDecision.BLOCK,
                    reason=str(messages[0].get("content", "")),
                )

            self._hook_dispatcher.register_internal(
                HookEvent.STOP,
                InternalHook(
                    callback=_goal_policy_hook,
                    hook_id="goal-completion-policy",
                    priority=20,
                ),
            )

        self._backend = backend
        self._registry = registry
        self._renderer = renderer or create_renderer(
            model=self._model, mode=self._agent_name,
        )

        # ── Phase 0b: AgentRuntime 路径（优先）vs SessionRuntime 回退 ──
        if agent_runtime is not None and conversation_store is not None:
            self._agent_runtime = agent_runtime
            self._conversation_store = conversation_store
            self._native_mode = True
            # SessionRuntime 仅保留回调基础设施（compact / stream callbacks）
            self._runtime = runtime  # 可能为 None
            self._root_session_id = self._session_id  # 简化：直接用 session_id
            self._root_session = None
        else:
            # 旧路径：完全 SessionRuntime
            # G36M-6: DEPRECATED — use runtime_core.runtime.AgentRuntime (G16)
            from agent.session.runtime import SessionRuntime  # noqa: G36M
            from agent.session.session_store import SessionStore
            from agent.session import default_session_db_path

            self._native_mode = False
            if runtime is not None:
                self._runtime = runtime
            else:
                db_path = default_session_db_path(str(repo_path))
                from core.state_paths import migrate_legacy_session_db
                migrate_legacy_session_db(repo_path, db_path)
                store = SessionStore(db_path)
                from core.resource_governor import ResourceGovernor
                gov = ResourceGovernor(self.config.resource_governance)

                self._runtime = SessionRuntime(
                    store=store, backend=backend, base_registry=registry,
                    agent_registry=None,  # deprecated — agent_definitions used instead
                    root_agent_config=self._build_agent_cfg(),
                    log_dir=log_dir,
                    hook_dispatcher=hook_dispatcher,
                    mcp_integration=mcp_integration,
                    memory_context=memory_context,
                    event_callback=self._make_event_callback(),
                    governor=gov,
                )

            # Root session — 所有轮次共享
            self._root_session = self._runtime.create_root_session(
                agent_name=agent_name,
                repo_path=repo_path,
                title=f"Chat {self._session_id}",
                metadata={"entrypoint": "chat", "session_id": self._session_id},
            )
            self._root_session_id = self._root_session.id

        # 跨轮共享 history — 从 DB 初始化
        self._shared_history = ConversationHistory(
            max_messages=config.context.history_window * 2,
        )
        self._sync_shared_history()

        # SessionState — 结构化任务追踪
        from context.session import SessionState
        self._session_state = SessionState()

        # 跨 session 上下文恢复
        self._inject_session_summary()

        # 清理过期记忆
        if self._memory_store:
            try:
                self._memory_store.prune_expired()
            except Exception:
                pass

        self.total_tokens = 0
        self.total_steps = 0
        self.round_count = 0

    def _build_agent_cfg(self):
        from agent.core import AgentConfig
        cfg = AgentConfig(
            max_steps=self.config.agent.max_steps,
            budget_tokens=self.config.agent.budget_tokens,
            request_budget_tokens=self.config.context.request_budget_tokens,
            artifact_threshold_tokens=self.config.context.artifact_threshold_tokens,
            history_max_messages=self.config.context.history_window * 2,
            llm_max_retries=3, llm_retry_delay=1.0,
            stream=True,
            stream_callback=self._make_stream_callback(),
            thought_callback=None,
            confirm_dangerous=self._confirm_callback is not None,
            confirm_callback=self._confirm_callback,
            streaming_tool_execution=os.environ.get("FORGE_STREAMING", "1") != "0",
            token_budget_continuation=os.environ.get("FORGE_NUDGE", "0") != "0",
            prompt_config=self.config.prompts,
        )
        # Load verify callback from env (FORGE_VERIFY_SCRIPT) for Chat mode
        _verify_env = os.environ.get("FORGE_VERIFY_SCRIPT", "")
        if _verify_env:
            self._load_verify_callback(cfg, _verify_env)
        return cfg

    def _load_verify_callback(self, cfg, script_path: str) -> None:
        """Load verify callback from Python file and set on agent config."""
        from pathlib import Path
        _vp = Path(script_path).resolve()
        if not _vp.exists():
            logger.warning("FORGE_VERIFY_SCRIPT not found: %s", script_path)
            return
        try:
            import importlib.util
            _spec = importlib.util.spec_from_file_location("verify_module", _vp)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            if hasattr(_mod, "verify"):
                cfg.verify_callback = _mod.verify
                logger.info("Verify callback loaded from %s", _vp)
            else:
                logger.warning("verify script %s must export a 'verify' function", _vp)
        except Exception as exc:
            logger.warning("Failed to load verify script %s: %s", _vp, exc)

    # ── 流式渲染回调 ──────────────────────────────────────────────────

    def _make_stream_callback(self):
        _started, _thought_printed = [False], [False]
        _buf = []
        renderer = self._renderer

        def stream_cb(text):
            if not _started[0]:
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _started[0] = True
            elif _thought_printed[0]:
                sys.stdout.write("\n\n")
                sys.stdout.flush()
                _thought_printed[0] = False
            _buf.append(text)
            renderer.stream_text(text)

        return stream_cb

    # ── Event 回调（实时渲染） ────────────────────────────────────────

    def _make_event_callback(self):
        renderer = self._renderer
        _last_tool = [""]
        _last_params = [{}]

        def callback(event):
            from agent.task import EventType
            p = event.payload
            if event.event_type == EventType.ACTION:
                action = p["action"]
                for tc in (action.get("tool_calls") or []):
                    _last_tool[0] = tc["name"]
                    _last_params[0] = tc.get("params", {})
                    renderer.on_tool_call(
                        step=p["step"], name=tc["name"], params=tc.get("params", {}),
                    )
                if action.get("action_type") == "finish":
                    renderer.on_finish(step=p["step"], message=action.get("message", ""))
                elif action.get("action_type") == "give_up":
                    renderer.on_give_up(step=p["step"], message=action.get("message", ""))
            elif event.event_type == EventType.OBSERVATION:
                obs = p["observation"]
                renderer.on_observation(
                    step=p["step"], tool_name=obs.get("tool_name", _last_tool[0]),
                    status=obs.get("status", ""), output=obs.get("output", ""),
                    error=obs.get("error"),
                )
            elif event.event_type == EventType.REFLECTION:
                renderer.on_reflection(reason=p.get("reason", ""))

        return callback

    # ── History 同步 ──────────────────────────────────────────────────

    def _sync_shared_history(self) -> None:
        """从 DB 读取消息，重建共享 history。"""
        # Phase 0b Step 11: ConversationStore 替代 SessionStore
        if self._native_mode and hasattr(self, '_conversation_store'):
            msgs = self._conversation_store.list_messages(
                self._root_session_id, limit=200,
            )
        else:
            msgs = self._runtime._store.list_messages(self._root_session_id)
        self._shared_history._messages.clear()
        for m in msgs:
            self._shared_history.add(m)

    # ── 轮次执行 ──────────────────────────────────────────────────────

    def run_round(self, user_input: str) -> bool:
        from agent.task import TaskIntent
        from llm.base import LLMMessage

        self.round_count += 1
        reset_prompt_usage()

        # Phase 0b Step 12: _agent_definitions 替代 _BUILTIN_AGENTS
        definition = self._agent_definitions.get(self._agent_name)
        intent = definition.intent if definition else TaskIntent.EDIT

        # Phase 0b Step 13: SESSION_START hook（仅首次）
        if self._hook_dispatcher and not self._hooks_started:
            from hooks.events import HookContext, HookEvent
            self._hook_dispatcher.dispatch(
                HookEvent.SESSION_START,
                HookContext(
                    event=HookEvent.SESSION_START,
                    session_id=self._root_session_id,
                    user_input=user_input,
                ),
            )
            self._hooks_started = True

        # SessionState 任务追踪
        task_ctx = self._session_state.start_task(user_goal=user_input, intent=intent)

        t0 = time.time()

        # ── Phase 0b: AgentRuntime 路径 vs SessionRuntime 回退 ──
        if self._native_mode:
            result = self._run_native_turn(user_input, definition, intent)
        else:
            # 旧路径
            result = self._runtime.run_session(
                self._root_session_id,
                agent_name=self._agent_name,
                task_description=user_input,
                intent=intent,
            )

        elapsed = time.time() - t0
        self.total_tokens += result.total_tokens
        self.total_steps += result.steps_taken

        # 同步 history
        self._sync_shared_history()

        # 渲染结果摘要到 shared_history
        if result.summary:
            self._shared_history.add(LLMMessage(role="assistant", content=result.summary))

        # SessionState 任务完成
        task_summary = self._build_task_summary(task_ctx=task_ctx, result=result, elapsed=elapsed)
        self._session_state.finish_task(task_summary)

        # Auto-compact
        self._maybe_auto_compact_after_round(result)

        # Renderer 轮次结束
        sys.stdout.write("\n")
        sys.stdout.flush()
        from agent.task import RunStatus
        self._renderer.on_round_end(
            round_num=self.round_count, steps=result.steps_taken,
            tokens=result.total_tokens, elapsed=elapsed,
            cache_stats=getattr(result, 'cache_stats', None),
        )

        flush_observability()
        return result.is_success() or result.status is RunStatus.GAVE_UP

    # ── Phase 0b: Native Turn 执行 ────────────────────────────────────

    def _run_native_turn(self, user_input: str, definition, intent):
        """使用 AgentRuntime.run() 执行一轮——Step 1 的核心替换。"""
        import uuid as _uuid
        from runtime_core.execution import ConversationSnapshot, RuntimeExecution
        from core.eventing.identifiers import SessionId, RunId

        # 构建跨轮 conversation
        conv_msgs = []
        if hasattr(self, '_conversation_store'):
            msgs = self._conversation_store.list_messages(
                self._root_session_id, limit=200,
            )
            for m in msgs:
                conv_msgs.append({
                    "role": getattr(m, "role", "user"),
                    "content": getattr(m, "content", ""),
                })
        conv_msgs.append({"role": "user", "content": user_input})
        conv = ConversationSnapshot(messages=tuple(conv_msgs))

        ctx = RuntimeExecution(
            session_id=SessionId(self._root_session_id),
            run_id=RunId(str(_uuid.uuid4())),
            max_steps=getattr(definition, "max_turns", 25) or 25,
            budget_tokens=200_000,
            conversation=conv,
        )

        outcome = self._agent_runtime.run(ctx)

        # 转为 RunResult 兼容格式
        from agent.task import RunResult, RunStatus
        return RunResult(
            status=RunStatus(outcome.status.value),
            summary=outcome.summary or "",
            steps_taken=outcome.steps_taken,
            total_tokens=outcome.tokens_used,
        )

    # ── 模式/模型切换 ────────────────────────────────────────────────

    def switch_mode(self, agent_name: str) -> None:
        # Phase 0b Step 12: load_agent_definitions 替代 _BUILTIN_AGENTS
        if agent_name not in self._agent_definitions:
            raise ValueError(
                f"Unknown agent: {agent_name!r}. "
                f"Available: {sorted(self._agent_definitions)}"
            )
        self._agent_name = agent_name
        self._renderer.mode = agent_name

    def switch_model(self, model, provider=None, api_key=None, base_url=None) -> None:
        from entry.agent_session_factory import rebuild_backend_for_model
        self._backend, self._model, self._provider = rebuild_backend_for_model(
            model, provider=provider, api_key=api_key, base_url=base_url,
            current_provider=self._provider,
        )
        self._renderer.model = model
        # Phase 0b Step 10 gap: NativeBackend doesn't support per-invoke model override.
        # For now, propagate to SessionRuntime if in legacy mode.
        if not self._native_mode and self._runtime is not None:
            self._runtime._backend = self._backend

    # ── 辅助：skill fork ─────────────────────────────────────────────

    def _handle_slash_skill(self, user_input: str) -> str | None:
        """/skill-name 命令处理"""
        if not user_input.startswith("/"):
            return None
        if self._skill_registry is None:
            return None
        parts = user_input[1:].split(None, 1)
        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        meta = self._skill_registry.get_skill_meta(name)
        if meta is None:
            return None
        if not meta.user_can_invoke:
            return None
        rendered = self._skill_registry.load_and_render(name, args, runtime=self._runtime)
        if rendered is None:
            return None
        from skills.activation import SkillActivationService
        activation = SkillActivationService(self._skill_registry).activate(
            name,
            source="cli_slash",
            session_id=self._root_session_id,
        )
        if activation is not None:
            # Phase 0b: Skills are tool-invoked in native path, not preloaded.
            # record_skill_activation is legacy SessionRuntime only.
            if not self._native_mode and self._runtime is not None:
                self._runtime.record_skill_activation(
                    activation.skill_name,
                    source=activation.source,
                    fingerprint=activation.fingerprint,
                    mcp_dependencies=list(activation.mcp_dependencies),
                    session_id=activation.session_id,
                )
        if meta.context == "fork":
            self._run_skill_fork(name, rendered, meta)
        else:
            from llm.base import LLMMessage, MessageKind

            skill_buffer = self._registry.skill_buffer
            if skill_buffer is not None:
                rendered = skill_buffer.activate(name, rendered)
            self._registry.activate_skill(meta)
            self._shared_history.add(LLMMessage(
                role="user",
                kind=MessageKind.RUNTIME_NOTICE,
                content=f"[Skill: {name}]\n{rendered}",
            ))
        return name

    def _run_skill_fork(self, name, rendered, meta) -> None:
        """以子会话方式运行 skill。"""
        from llm.base import LLMMessage
        from agent.session import AgentSpawnRequest, ExecutionPlacement
        from agent.session.run_context import CancellationToken
        from core.policy import PhasePolicy
        fork_request = AgentSpawnRequest.named(
            definition=meta,
            description=f"skill/{name}",
            prompt=rendered,
            execution_placement=ExecutionPlacement.FOREGROUND,
        )
        # Phase 0b: Fork agent not in native path scope (Phase 1-9 skipped fork).
        if self._native_mode:
            from agent.task import RunResult, RunStatus as _RS
            result = RunResult(
                status=_RS.GAVE_UP,
                summary="Fork agent not available in native execution path.",
            )
        else:
            result = self._runtime.spawn_agent(
                parent_session_id=self._root_session_id,
                request=fork_request,
                parent_policy=PhasePolicy(),
                cancellation_token=CancellationToken(),
                budget_tokens=20_000,
                parent_max_steps=10,
            )
        if result.summary:
            self._shared_history.add(LLMMessage(
                role="assistant",
                content=f"[Skill: {name}]\n{result.summary}",
            ))
        self.total_tokens += result.tokens_used

    # ── 压缩 ──────────────────────────────────────────────────────────

    def compact(self, focus: str = "") -> str:
        # Phase 0b Step 14: ContextBudgetManager for native path
        if self._native_mode:
            # Native path: ContextBudgetManager auto-trims in StepLoop.
            # Manual compact = sync history (which is all we need for CLI display).
            self._sync_shared_history()
            return "Compacted (auto-budget management active)."
        msg = self._runtime.compact(focus=focus)
        self._sync_shared_history()
        return msg

    def _maybe_auto_compact_after_round(self, result) -> None:
        from agent.task import RunStatus
        if not getattr(self.config.context, "auto_compact_after_round", True):
            return
        # Phase 0b: normalize RunStatus comparison
        _status = getattr(result, 'status', None)
        if isinstance(_status, str):
            try:
                _status = RunStatus(_status)
            except ValueError:
                _status = None
        if _status not in (RunStatus.SUCCESS, RunStatus.GAVE_UP, RunStatus.MAX_STEPS):
            return
        compact_rounds = getattr(self.config.context, "compact_every_rounds", 3)
        if self.round_count % compact_rounds != 0:
            return
        history_tokens = getattr(self._shared_history, "estimated_tokens", lambda: 0)()
        threshold = getattr(self.config.context, "session_compact_tokens", 30_000)
        if history_tokens < threshold:
            return
        prompt = self._session_state.active_task.user_goal if self._session_state.active_task else ""
        msg = self.compact(focus=prompt)
        self._session_state.compaction_count += 1

    # ── Session 上下文注入 ────────────────────────────────────────────

    def _inject_session_summary(self) -> None:
        from context.compaction import load_session_summary
        from llm.base import LLMMessage
        summary = load_session_summary(str(Path(self.repo_path) / ".grace" / "session_summary.md"))
        if summary:
            self._shared_history.add(LLMMessage(
                role="user",
                content=f"[Previous Session Context]\n{summary}",
            ))
            self._shared_history.add(LLMMessage(role="assistant", content="Understood."))

    # ── TaskSummary 构建 ──────────────────────────────────────────────

    def _build_task_summary(self, *, task_ctx, result, elapsed):
        from context.session import TaskSummary
        return TaskSummary(
            task_id=task_ctx.task_id,
            user_goal=task_ctx.user_goal,
            outcome=result.status.value,
            steps_taken=result.steps_taken,
            tokens_spent=result.total_tokens,
            elapsed_seconds=elapsed,
        )

    def print_stats(self) -> None:
        """Print session statistics."""
        from context.token_budget import estimate_tokens
        history_dicts = self._shared_history.to_dicts()
        shared_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in history_dicts)
        ss = self._session_state
        session_info = (
            f"completed_tasks={len(ss.completed_tasks)}, "
            f"compactions={ss.compaction_count}, "
            f"session_summary_tokens={ss.estimated_tokens()}"
        )
        if ss.last_compaction_reason:
            session_info += f", last_compact_reason={ss.last_compaction_reason}"
        self._renderer.on_stats(
            rounds=self.round_count,
            steps=self.total_steps,
            tokens=self.total_tokens,
            shared_history_tokens=shared_tokens,
            session_info=session_info,
        )

    def _create_goal_judge_backend(self, judge_model: str):
        from llm.base import MockBackend
        if not judge_model or judge_model == "mock":
            return MockBackend([])
        from config.schema import load_config
        config = load_config()
        llm_cfg = config.llm
        provider = llm_cfg.provider
        from entry.cli import create_backend_from_config
        return create_backend_from_config({
            "provider": provider, "model": judge_model,
            "api_key": os.environ.get(f"{provider.upper()}_API_KEY", ""),
            "base_url": llm_cfg.base_url or "",
        })
