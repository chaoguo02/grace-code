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

import os
import time
import sys
import uuid
from pathlib import Path

import click

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent.prompt import reset_prompt_usage, set_project_dir
from entry.renderer import InlineRenderer, create_renderer
from observability import flush_observability

Renderer = InlineRenderer
RendererBase = InlineRenderer


class ChatSession:
    """跨轮对话会话。每轮通过 SessionRuntime.run_session() 执行。"""

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
    ) -> None:
        from agent.core import AgentConfig
        from context.history import ConversationHistory
        from agent.session.runtime import SessionRuntime
        from agent.session.session_store import SessionStore
        from agent.session.agent_registry import AgentRegistryV2
        from agent.session import default_session_db_path

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

        self._backend = backend
        self._registry = registry
        self._agent_registry = AgentRegistryV2(project_dir=repo_path)
        self._renderer = renderer or create_renderer(
            model=self._model, mode=self._agent_name,
        )

        # SessionRuntime: 统一执行入口
        if runtime is not None:
            self._runtime = runtime
        else:
            db_path = default_session_db_path(str(repo_path))
            from executor.state_paths import migrate_legacy_session_db
            migrate_legacy_session_db(repo_path, db_path)
            store = SessionStore(db_path)
            self._runtime = SessionRuntime(
                store=store, backend=backend, base_registry=registry,
                agent_registry=self._agent_registry,
                root_agent_config=self._build_agent_cfg(),
                log_dir=log_dir,
                hook_dispatcher=hook_dispatcher,
                mcp_integration=mcp_integration,
                memory_context=memory_context,
                event_callback=self._make_event_callback(),
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

        # Goal Stop Hook
        from executor.goal import GoalStore
        from executor.state_paths import ProjectStatePaths
        self.goal_store = GoalStore(ProjectStatePaths.for_project(repo_path).goals)
        self.goal_store.restore()

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
        msgs = self._runtime._store.list_messages(self._root_session_id)
        self._shared_history._messages.clear()
        for m in msgs:
            self._shared_history.add(m)

    # ── 轮次执行 ──────────────────────────────────────────────────────

    def run_round(self, user_input: str) -> bool:
        from agent.event_log import EventLog
        from agent.task import Task, TaskIntent, RunStatus
        from agent.session.models import _BUILTIN_AGENTS
        from llm.base import LLMMessage

        self.round_count += 1
        set_project_dir(self.repo_path)
        reset_prompt_usage()

        definition = _BUILTIN_AGENTS.get(self._agent_name)
        intent = definition.intent if definition else TaskIntent.EDIT

        # SessionState 任务追踪
        task_ctx = self._session_state.start_task(user_goal=user_input, intent=intent)

        # 重置 compaction thrashing
        agent = getattr(self._runtime, "_last_agent", None)
        if agent and hasattr(agent, "compactor") and hasattr(agent.compactor, "reset_thrashing_counter"):
            agent.compactor.reset_thrashing_counter()

        t0 = time.time()

        # 通过 SessionRuntime 执行
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
        self._renderer.on_round_end(
            round_num=self.round_count, steps=result.steps_taken,
            tokens=result.total_tokens, elapsed=elapsed,
            cache_stats=result.cache_stats,
        )

        flush_observability()
        return result.is_success() or result.status is RunStatus.GAVE_UP

    # ── 模式/模型切换 ────────────────────────────────────────────────

    def switch_mode(self, agent_name: str) -> None:
        from agent.session.models import _BUILTIN_AGENTS
        if agent_name not in _BUILTIN_AGENTS:
            raise ValueError(
                f"Unknown agent: {agent_name!r}. Available: {sorted(_BUILTIN_AGENTS)}"
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
        self._run_skill_fork(name, rendered, meta)
        return name

    def _run_skill_fork(self, name, rendered, meta) -> None:
        """以子会话方式运行 skill。"""
        from agent.session import AgentSpawnRequest, ExecutionPlacement, TaskIntent
        from agent.session.task_contract import TaskContract
        from agent.session.run_context import CancellationToken
        from core.policy import PhasePolicy
        fork_request = AgentSpawnRequest.named(
            definition=meta,
            description=f"skill/{name}",
            prompt=rendered,
            execution_placement=ExecutionPlacement.FOREGROUND,
        )
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
        msg = self._runtime.compact(focus=focus)
        self._sync_shared_history()
        return msg

    def _maybe_auto_compact_after_round(self, result) -> None:
        from agent.task import RunStatus
        if not getattr(self.config.context, "auto_compact_after_round", True):
            return
        if result.status not in (RunStatus.SUCCESS, RunStatus.GAVE_UP, RunStatus.MAX_STEPS):
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
        summary = load_session_summary(str(Path(self.repo_path) / ".forge-agent" / "session_summary.md"))
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

    # ── Goal Stop Hook ────────────────────────────────────────────────

    def _goal_stop_hook(self, messages: list[dict]):
        from executor.goal import goal_stop_hook
        return goal_stop_hook(
            messages=messages,
            goal_store=self.goal_store,
            repo_path=self.repo_path,
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
