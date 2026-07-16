"""
entry/chat.py

交互对话模式。持续会话，每轮用户输入后 agent 继续工作，
history 跨轮保留，像 Claude Code 一样可以持续对话。

架构设计：
- ChatSession 持有 backend / registry / history / renderer，跨轮复用
- 每轮创建一个新 Task，但 history 通过 agent._inject_history() 延续
- EventLog 每轮独立（方便单轮审计），但统计累计显示
- 实时打印：每条 event 写入 log 后立刻通过 Renderer 输出

用法：
    agent chat --repo /path/to/repo
    agent chat --repo . --model deepseek-chat
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

from agent.prompt import reset_prompt_usage, set_project_dir  # noqa: E402
from entry.renderer import InlineRenderer, create_renderer  # noqa: E402
from observability import flush_observability  # noqa: E402

# 兼容别名
Renderer = InlineRenderer
RendererBase = InlineRenderer


# ---------------------------------------------------------------------------
# ChatSession — 跨轮持久化的会话状态
# ---------------------------------------------------------------------------

class ChatSession:
    """
    持久化会话。跨多轮对话保留：
    - backend / registry（不变）
    - ConversationHistory（核心：让 agent 记得之前做了什么）
    - Renderer（统一输出抽象）
    - 累计 token / 步数统计
    - repo_map 缓存（换 repo 时自动失效）
    """

    def __init__(
        self,
        backend,
        registry,
        config,
        repo_path: str,
        log_dir: str,
        confirm_callback=None,
        renderer: RendererBase | None = None,
        memory_store=None,
        memory_context=None,
        skill_registry=None,
    ) -> None:
        from agent.core import AgentConfig
        from context.history import ConversationHistory

        self.repo_path = repo_path
        self.log_dir = log_dir
        self.config = config
        self._session_id = uuid.uuid4().hex[:12]
        self._confirm_callback = confirm_callback
        self._agent_name = "build"
        self._model = getattr(backend, "model_name", "?")
        self._provider = getattr(config.llm, "provider", "?")

        self._backend = backend
        self._registry = registry
        from agent.v2.agent_registry import AgentRegistryV2
        self._agent_registry = AgentRegistryV2(project_dir=self.repo_path)
        self._renderer = renderer or create_renderer(
            model=self._model, mode=self._agent_name,
        )

        # ── Skill 系统 ─────────────────────────────────────────────────
        self._skill_registry = skill_registry

        # ── 记忆系统 ──────────────────────────────────────────────────
        self._memory_store = memory_store
        self._memory_context = memory_context

        # ── 流式回调（委托给 Renderer）────────────────────────────
        _stream_started = [False]
        _thought_printed = [False]
        _streamed_buf: list[str] = []

        def _thought_cb(text: str) -> None:
            if not _stream_started[0]:
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _stream_started[0] = True
            _thought_printed[0] = True
            self._renderer.stream_thought(text)

        def _stream_cb(text: str) -> None:
            if not _stream_started[0]:
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _stream_started[0] = True
            elif _thought_printed[0]:
                sys.stdout.write("\n\n")
                sys.stdout.flush()
                _thought_printed[0] = False
            _streamed_buf.append(text)
            self._renderer.stream_text(text)

        self._agent_cfg = AgentConfig(
            max_steps=config.agent.max_steps,
            budget_tokens=config.agent.budget_tokens,
            request_budget_tokens=config.context.request_budget_tokens,
            artifact_threshold_tokens=config.context.artifact_threshold_tokens,
            history_max_messages=config.context.history_window * 2,
            llm_max_retries=3,
            llm_retry_delay=1.0,
            stream=True,
            stream_callback=_stream_cb,
            thought_callback=None,
            confirm_dangerous=confirm_callback is not None,
            confirm_callback=confirm_callback,
        )
        from agent.v2.agent_factory import AgentFactory
        self._agent_assembly = AgentFactory.create(
            agent_name=self._agent_name,
            backend=self._backend,
            base_registry=self._registry,
            agent_registry=self._agent_registry,
            root_agent_config=self._agent_cfg,
            memory_context=self._memory_context,
            repo_path=self.repo_path,
        )
        self.agent = self._agent_assembly.agent
        self._shared_history = ConversationHistory(
            max_messages=config.context.history_window * 2,
        )

        # ── Session State（Phase 3: 结构化会话状态）─────────────────
        from context.session import SessionState
        self._session_state = SessionState()

        # ── Goal Stop Hook（Claude Code /goal-style session goal）────
        from executor.goal import GoalStore
        from executor.state_paths import ProjectStatePaths
        self.goal_store = GoalStore(ProjectStatePaths.for_project(self.repo_path).goals)
        self.goal_store.restore()

        # ── 跨 session 上下文恢复（从持久化的 compaction 摘要）─────
        self._inject_session_summary()

        # ── 启动时清理过期 episodic 记忆（每 session 一次）──────────
        if self._memory_store:
            try:
                self._memory_store.prune_expired()
            except Exception:
                pass

        self.total_tokens = 0
        self.total_steps = 0
        self.round_count = 0

    def switch_mode(self, agent_name: str) -> None:
        """Switch to a different agent by name."""
        from agent.v2.models import _BUILTIN_AGENTS
        if agent_name not in _BUILTIN_AGENTS:
            raise ValueError(f"Unknown agent: {agent_name!r}. Available: {sorted(_BUILTIN_AGENTS)}")
        self._agent_name = agent_name
        self._renderer.mode = agent_name
        self._rebuild_agent()

    def switch_model(
        self, model: str,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """运行时切换 LLM 模型（保持对话历史）。委托给 agent_session_factory。"""
        from entry.chat_services.agent_session_factory import rebuild_backend_for_model
        self._backend, self._model, self._provider = rebuild_backend_for_model(
            model, provider=provider, api_key=api_key, base_url=base_url,
            current_provider=self._provider,
        )
        self._renderer.model = model
        self._rebuild_agent()

    def _rebuild_agent(self) -> None:
        """用当前的 backend 重建 agent 实例。委托给 AgentFactory。"""
        from agent.v2.agent_factory import AgentFactory
        self._agent_assembly = AgentFactory.create(
            agent_name=self._agent_name,
            backend=self._backend,
            base_registry=self._registry,
            agent_registry=self._agent_registry,
            root_agent_config=self._agent_cfg,
            memory_context=self._memory_context,
            repo_path=self.repo_path,
        )
        self.agent = self._agent_assembly.agent

    def run_round(self, user_input: str) -> bool:
        """
        执行一轮对话。返回 True 表示正常结束（含 gave_up）。
        """
        from agent.event_log import EventLog
        from agent.task import Task, TaskIntent
        from context.session import TaskSummary
        from llm.base import LLMMessage

        self.round_count += 1
        set_project_dir(self.repo_path)
        reset_prompt_usage()
        self._shared_history.add(LLMMessage(role="user", content=user_input))

        # Phase 3: 开始一个结构化 task context
        from agent.v2.models import _BUILTIN_AGENTS
        definition = _BUILTIN_AGENTS.get(self._agent_name)
        intent = definition.intent if definition else TaskIntent.EDIT

        # Phase 7: 分类任务关系
        task_ctx = self._session_state.start_task(
            user_goal=user_input,
            intent=intent,
        )

        # Phase 7: 基于任务关系的预压缩
        # 用户新一轮输入，重置 compaction thrashing 计数器
        if hasattr(self.agent, "compactor") and hasattr(self.agent.compactor, "reset_thrashing_counter"):
            self.agent.compactor.reset_thrashing_counter()

        task = Task(
            description=user_input,
            repo_path=self.repo_path,
            intent=intent,
            max_steps=self.config.agent.max_steps,
            budget_tokens=self.config.agent.budget_tokens,
            metadata={
                "entrypoint": "chat",
                "agent": self._agent_name,
                "session_id": self._session_id,
                "round": self.round_count,
                "provider": self._provider,
                "model": self._model,
            },
        )

        self.agent._shared_history = self._shared_history

        # Phase 6: inject session context for this round
        session_ctx = self._session_state.get_session_context_for_prompt(budget_tokens=4000)
        self.agent._session_context = session_ctx if session_ctx else None

        t0 = time.time()
        with EventLog.create(task, log_dir=self.log_dir) as log:
            result = self._run_with_renderer(task, log)
            # 自动归档到历史目录
            try:
                from entry.history_viewer import archive_log
                archive_log(log.path)
            except Exception:
                pass

        flush_observability()
        elapsed = time.time() - t0
        self.total_tokens += result.total_tokens
        self.total_steps += result.steps_taken

        if result.summary:
            self._shared_history.add(LLMMessage(
                role="assistant",
                content=result.summary,
            ))

        # Phase 3: 任务结束时生成 TaskSummary 并更新 session state
        task_summary = self._build_task_summary(
            task_ctx=task_ctx,
            result=result,
            elapsed=elapsed,
        )
        self._session_state.finish_task(task_summary)

        # Phase 3: 检查是否需要自动压缩 shared_history
        self._maybe_auto_compact_after_round(result)

        sys.stdout.write("\n")
        sys.stdout.flush()

        self._renderer.on_round_end(
            round_num=self.round_count,
            steps=result.steps_taken,
            tokens=result.total_tokens,
            elapsed=elapsed,
            cache_stats=result.cache_stats,
        )

        from agent.task import RunStatus
        return result.is_success() or result.status is RunStatus.GAVE_UP

    def _run_with_renderer(self, task, log):
        """运行 agent，通过 monkey-patch EventLog 实现事件实时输出。"""
        from agent.task import EventType

        original_append = log._append
        # 记录当前工具名和参数，供 observation 使用
        _last_tool_name = [""]
        _last_tool_params: list[dict] = [{}]

        def _handle_event(event):
            etype = event.event_type
            p = event.payload

            if etype == EventType.ACTION:
                action = p["action"]
                atype = action.get("action_type", "")
                tcs = action.get("tool_calls") or []

                if tcs:
                    for tc in tcs:
                        _last_tool_name[0] = tc["name"]
                        _last_tool_params[0] = tc.get("params", {})
                        self._renderer.on_tool_call(
                            step=p["step"],
                            name=tc["name"],
                            params=tc.get("params", {}),
                        )
                elif atype == "finish":
                    self._renderer.on_finish(
                        step=p["step"],
                        message=action.get("message", ""),
                    )
                elif atype == "give_up":
                    self._renderer.on_give_up(
                        step=p["step"],
                        message=action.get("message", ""),
                    )

            elif etype == EventType.OBSERVATION:
                obs = p["observation"]
                tool_name = obs.get("tool_name", _last_tool_name[0])
                self._renderer.on_observation(
                    step=p["step"],
                    tool_name=tool_name,
                    status=obs.get("status", ""),
                    output=obs.get("output", ""),
                    error=obs.get("error"),
                )
                # 主动记忆：检测成功的构建/测试命令
            elif etype == EventType.REFLECTION:
                self._renderer.on_reflection(
                    reason=p.get("reason", ""),
                )

        def live_append(event):
            original_append(event)
            _handle_event(event)

        log._append = live_append
        return self._run_injecting_history(task, log)

    def _run_injecting_history(self, task, log):
        """注入共享 history + skills prompt 后运行 agent。"""
        agent = self.agent
        agent._pending_history = self._shared_history
        # 注入 skills metadata（如果有）
        if self._skill_registry and self._skill_registry.list_skills():
            agent._skills_prompt = self._skill_registry.format_for_prompt()
        agent._goal_stop_hook = self._goal_stop_hook
        result = agent.run(task, log)
        if hasattr(agent, "_pending_history"):
            del agent._pending_history
        if hasattr(agent, "_goal_stop_hook"):
            del agent._goal_stop_hook
        return result

    def _goal_stop_hook(self, messages: list[dict]):
        from executor.goal import goal_stop_hook
        return goal_stop_hook(
            self.goal_store,
            messages,
            backend_factory=self._create_goal_judge_backend,
        )

    def _create_goal_judge_backend(self, judge_model: str):
        from llm.router import create_backend
        provider = self.config.llm.provider
        model = judge_model
        if model == "haiku" and provider != "anthropic":
            model = self.config.llm.model
        return create_backend(
            provider=provider,
            model=model,
            api_key=self.config.llm.api_key or None,
            base_url=self.config.llm.base_url or None,
            max_tokens=500,
            timeout_seconds=30.0,
        )

    # ------------------------------------------------------------------
    # Skill 系统
    # ------------------------------------------------------------------

    def _handle_slash_skill(self, user_input: str) -> str | None:
        """Handle /skill-name [arguments] input from the chat REPL.

        Aligned with Claude Code: skills are invoked via /skill-name directly
        by the user, injecting the rendered skill content into context without
        a tool_use round-trip. The SkillTool (use_skill fallback) remains
        available for LLM-initiated invocations.

        Returns the rendered skill content if a matching skill is found,
        or None if the input does not match any registered skill name.
        """
        if not user_input.startswith("/"):
            return None
        if not self._skill_registry:
            return None

        # Strip leading / and split name from arguments
        inner = user_input[1:]
        parts = inner.split(maxsplit=1)
        name, args = parts[0], (parts[1] if len(parts) > 1 else "")

        if not self._skill_registry.has_skill(name):
            return None

        # SK-04: respect user-invocable — only user-invocable skills can be /-invoked
        meta = self._skill_registry.get_skill_meta(name)
        if meta is not None and not meta.user_can_invoke:
            return None  # Silently ignore; skill is for LLM-only invocation

        rendered = self._skill_registry.load_and_render(name, args)
        if rendered is None:
            return None

        # SK-07: context:fork — run skill in a forked subagent
        if meta is not None and meta.context == "fork":
            self._run_skill_fork(name, rendered, meta)
            return None  # Fork handles its own execution; no inline injection

        # Default: inline context — inject skill content into shared_history
        return (
            f"[Skill: {name}]\n\n"
            f"{rendered}\n\n"
            f"[/End of skill: {name} — follow the instructions above.]"
        )

    def _run_skill_fork(self, name: str, rendered: str, meta) -> None:
        """SK-07: Execute a context:fork skill as a subagent.

        Spawns a subagent of the type declared in meta.agent (default: general)
        with the rendered skill content as the task prompt.
        """
        import click
        from agent.v2.models import DelegationScope, WorkspaceMode

        agent_type = meta.agent or "general"
        click.echo(
            click.style(f"\n  Forking subagent '{agent_type}' for skill '{name}'...", fg="cyan")
        )

        try:
            # Rebuild agent to pick up any model/effort overrides from the skill
            fork_assembly = self._build_fork_assembly(agent_type, meta)
            fork_agent = fork_assembly.agent

            # CC-aligned: context=fork runs in FRESH context, not inheriting parent history
            fork_agent._goal_stop_hook = self._goal_stop_hook

            from agent.task import Task, TaskIntent
            task = Task(
                description=f"[Skill: {name}] {rendered[:500]}",
                repo_path=self.repo_path,
                intent=TaskIntent.EDIT,
                max_steps=self.config.agent.max_steps,
                budget_tokens=self.config.agent.budget_tokens,
                metadata={"entrypoint": "skill-fork", "skill": name, "agent_type": agent_type},
            )

            from agent.event_log import EventLog
            with EventLog.create(task, log_dir=self.log_dir) as log:
                result = fork_agent.run(task, log)

            if result.summary:
                from llm.base import LLMMessage
                self._shared_history.add(LLMMessage(
                    role="assistant", content=f"[Skill fork: {name}]\n{result.summary}"
                ))

            click.echo(
                click.style(f"  Skill '{name}' fork completed: {result.summary or 'done'}", fg="cyan")
            )
        except Exception as exc:
            click.echo(click.style(f"  Skill '{name}' fork failed: {exc}", fg="red"))

    def _build_fork_assembly(self, agent_type: str, meta):
        """Build an AgentAssembly for a skill fork, respecting model/effort overrides."""
        from agent.v2.agent_factory import AgentFactory

        agent_cfg = self._agent_cfg
        # SK-20: apply model/effort overrides from skill frontmatter
        if meta.model or meta.effort:
            from dataclasses import replace
            agent_cfg = replace(agent_cfg)
            # Note: actual model switching requires backend rebuild.
            # For now, effort override is passed through agent_config metadata.

        return AgentFactory.create(
            agent_name=agent_type,
            backend=self._backend,
            base_registry=self._registry,
            agent_registry=self._agent_registry,
            root_agent_config=agent_cfg,
            memory_context=self._memory_context,
            repo_path=self.repo_path,
        )

    def compact(self, focus: str = "") -> str:
        """
        压缩当前对话历史（/compact 命令）。
        调用 ConversationCompactor 把 shared_history 压缩，
        保留首条消息，其余生成 compact 摘要块。
        同时将摘要持久化到磁盘，以便跨 session 恢复上下文。

        增强（Phase 6）：
        - 注入 session rolling summary 作为 compaction 上下文
        - 压缩后触发 memory candidate 提取

        Args:
            focus: 可选的焦点指令，引导压缩时优先保留哪些信息

        Returns:
            提示文本（用于显示给用户）
        """
        from context.compaction import ConversationCompactor, persist_compaction_summary
        from llm.base import LLMMessage

        history_dicts = self._shared_history.to_dicts()
        if len(history_dicts) < 4:
            return "Conversation is too short to compact (minimum 4 messages)."

        compactor = ConversationCompactor(backend=self._backend)

        # 构建增强的 task_context：包含 focus + session rolling summary
        task_context_parts = []
        if focus:
            task_context_parts.append(f"[User focus] {focus}")
        session_summary = self._session_state.get_session_context_for_prompt(budget_tokens=3000)
        if session_summary:
            task_context_parts.append(f"[Session context — completed tasks]\n{session_summary}")
        if task_context_parts:
            compactor._task_context = "\n\n".join(task_context_parts)

        compacted = compactor.build_compact_block_for_history(history_dicts)

        # 重建 shared_history
        self._shared_history.clear_except_first()
        # 添加 compaction 块作为新的 user 消息
        self._shared_history.add(LLMMessage(
            role="user",
            content=compacted["content"],
        ))

        # 持久化摘要到磁盘
        if self._memory_store:
            persist_compaction_summary(
                compacted["content"],
                str(self._memory_store.store_dir.parent),
            )

        # Phase 6: 异步提取 memory candidates（如果达到提取阈值）
        self._maybe_extract_memory_from_compaction(history_dicts)

        count = len(history_dicts)
        return f"Conversation compacted: {count} messages → 2 messages."

    def _maybe_extract_memory_from_compaction(self, history_dicts: list[dict]) -> None:
        """
        Compaction 后尝试提取 memory candidates。

        只在满足条件时触发：
        - memory 系统启用且 auto_memory 开启
        - 历史足够长（>= 10 条消息，表明有实质工作）
        - 有 backend 可用
        - 有已完成的 TaskSummary（session state 有实质内容）

        使用 session state 中的 TaskSummary 作为提取输入，
        而非完整 history_dicts（避免重复和噪音）。
        """
        if not self._memory_store or not self.config.memory.auto_memory:
            return
        if len(history_dicts) < 10:
            return
        if not self._backend:
            return
        if not self._session_state.completed_tasks:
            return

        import logging
        logger = logging.getLogger(__name__)

        try:
            from memory.extractor import MemoryExtractor
            from agent.task import Task

            extractor = MemoryExtractor(backend=self._backend)

            # 用最近完成的 task 的 summary 构建一个轻量提取
            recent_task = self._session_state.completed_tasks[-1]
            dummy_task = Task(
                description=recent_task.user_goal,
                repo_path=self.repo_path,
                max_steps=1,
                budget_tokens=1,
            )

            # 创建一个空的 EventLog-like 对象
            class _NullLog:
                def replay(self):
                    return []

            candidates = extractor.extract(
                task=dummy_task,
                log=_NullLog(),
                summary=recent_task.to_text(),
            )

            written = 0
            for candidate in candidates:
                if candidate.confidence == "low":
                    continue
                try:
                    action = self._memory_store.consolidate(candidate)
                    if action != "NOOP":
                        written += 1
                except Exception:
                    pass

            if written:
                logger.info("Extracted %d memories from post-compaction reflection", written)
        except Exception as exc:
            logger.debug("Post-compaction memory extraction skipped: %s", exc)

    def _build_task_summary(self, task_ctx, result, elapsed: float):
        """从 TaskContext + RunResult 构建 TaskSummary。"""
        from context.session import TaskSummary

        changed = list(getattr(self.agent, "_accessed_files", set()))[:20]
        summary = TaskSummary(
            task_id=task_ctx.task_id,
            user_goal=task_ctx.user_goal,
            outcome=result.summary or "completed",
            changed_files=changed,
            steps_taken=result.steps_taken,
            tokens_spent=result.total_tokens,
            elapsed_seconds=elapsed,
            decisions=task_ctx.decisions[:10],
            unresolved=task_ctx.unresolved[:5],
        )
        return summary

    def _maybe_auto_compact_after_round(self, result) -> None:
        """
        任务边界自动压缩检查。

        触发条件（满足任一）：
        - shared_history token 数超过 session_compact_tokens
        - round_count 是 compact_every_rounds 的倍数
        - result.total_tokens 超过 budget_tokens * 0.7

        回写 _shared_history：压缩为 rolling session summary + 最近一轮。
        """
        cfg = self.config.context
        if not cfg.auto_compact_after_round:
            return

        from context.token_budget import estimate_tokens

        history_dicts = self._shared_history.to_dicts()
        history_tokens = sum(
            estimate_tokens(m.get("content", "") or "")
            for m in history_dicts
        )

        should_compact = False
        reason = ""

        if history_tokens > cfg.session_compact_tokens:
            should_compact = True
            reason = f"session history {history_tokens} > {cfg.session_compact_tokens} threshold"
        elif self.round_count > 1 and self.round_count % cfg.compact_every_rounds == 0:
            should_compact = True
            reason = f"periodic compact at round {self.round_count} (every {cfg.compact_every_rounds})"
        elif result.total_tokens > self.config.agent.budget_tokens * 0.7:
            should_compact = True
            reason = f"task spent {result.total_tokens} > 70% of budget {self.config.agent.budget_tokens}"

        if not should_compact:
            return

        if len(history_dicts) < 4:
            return

        import logging
        logger = logging.getLogger(__name__)
        logger.info("Auto-compact after round: %s", reason)

        msg = self.compact(focus=self._session_state.active_task.user_goal if self._session_state.active_task else "")
        self._session_state.compaction_count += 1
        self._session_state.last_compaction_reason = reason

        logger.info("Auto-compact result: %s", msg)

    def _inject_session_summary(self) -> None:
        """
        启动时注入上次 session 的 compaction 摘要（如果存在）。
        实现跨 session 上下文续传。
        """
        if not self._memory_store:
            return

        from context.compaction import load_session_summary
        from llm.base import LLMMessage

        summary = load_session_summary(str(self._memory_store.store_dir.parent))
        if not summary:
            return

        self._shared_history.add(LLMMessage(
            role="user",
            content=(
                "[Previous session context — restored from disk]\n\n"
                f"{summary}\n\n"
                "[End of previous session context. New conversation begins below.]"
            ),
        ))

    def print_stats(self) -> None:
        """打印会话总统计（含上下文分层信息）。"""
        from context.token_budget import estimate_tokens

        # 估算 shared_history 当前 token 量
        history_dicts = self._shared_history.to_dicts()
        shared_history_tokens = sum(
            estimate_tokens(m.get("content", "") or "")
            for m in history_dicts
        )

        # 获取最近一次 context stats
        last_ctx = getattr(self.agent, "_last_context_stats", None)
        context_line = last_ctx.summary_line() if last_ctx else None

        # Session state info
        ss = self._session_state
        session_info = (
            f"completed_tasks={len(ss.completed_tasks)}, "
            f"compactions={ss.compaction_count}, "
            f"session_summary_tokens={ss.estimated_tokens()}"
        )
        if ss.last_compaction_reason:
            session_info += f", last_compact_reason={ss.last_compaction_reason}"

        # Artifact store info
        art_store = self.agent.artifact_store
        artifact_info = f"artifacts={art_store.count}, stored_tokens={art_store.total_tokens_stored}"

        self._renderer.on_stats(
            rounds=self.round_count,
            total_steps=self.total_steps,
            total_tokens=self.total_tokens,
            shared_history_messages=len(history_dicts),
            shared_history_tokens=shared_history_tokens,
            context_summary=context_line,
            session_info=session_info,
            artifact_info=artifact_info,
        )
