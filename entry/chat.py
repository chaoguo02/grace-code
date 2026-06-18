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
from pathlib import Path

import click

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent.factory import create_agent  # noqa: E402
from entry.renderer import InlineRenderer, create_renderer  # noqa: E402

# 兼容别名
Renderer = InlineRenderer
RendererBase = InlineRenderer


def _cyan_prompt(text: str) -> str:
    """为 prompt 文本加 cyan 颜色（仅 TTY）。"""
    if sys.stdout.isatty():
        return f"\033[36m{text}\033[0m"
    return text


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
        self._confirm_callback = confirm_callback
        self._mode = "react"
        self._model = getattr(backend, "model_name", "?")
        self._provider = "?"

        self._backend = backend
        self._registry = registry
        self._renderer = renderer or create_renderer(
            model=self._model, mode=self._mode,
        )

        # ── Skill 系统 ─────────────────────────────────────────────────
        self._skill_registry = skill_registry

        # ── 记忆系统 ──────────────────────────────────────────────────
        self._memory_store = memory_store
        self._memory_context = memory_context
        self._proactive_memory = None
        if memory_store:
            from memory.proactive import ProactiveMemory
            self._proactive_memory = ProactiveMemory(memory_store)

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
            history_max_messages=config.context.history_window * 2,
            llm_max_retries=3,
            llm_retry_delay=1.0,
            stream=True,
            stream_callback=_stream_cb,
            thought_callback=_thought_cb,
            confirm_dangerous=confirm_callback is not None,
            confirm_callback=confirm_callback,
        )
        multi_cfg = self._build_multi_config() if self._mode == "multi-agent" else None
        self.agent = create_agent(
            self._mode, self._backend, self._registry, self._agent_cfg,
            plan_approval_callback=self._plan_approval,
            multi_config=multi_cfg,
        )
        self._shared_history = ConversationHistory(
            max_messages=config.context.history_window * 2,
        )

        # ── 跨 session 上下文恢复（从持久化的 compaction 摘要）─────
        self._inject_session_summary()

        self.total_tokens = 0
        self.total_steps = 0
        self.round_count = 0

    def switch_mode(self, mode: str) -> None:
        """运行时切换 agent 模式（react / plan / dag / multi-agent / auto）。"""
        if mode not in ("react", "plan", "dag", "multi-agent", "auto"):
            raise ValueError(f"Unknown mode: {mode!r}")
        self._mode = mode
        self._renderer.mode = mode
        self._rebuild_agent()

    def switch_model(
        self, model: str,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """运行时切换 LLM 模型（保持对话历史）。"""
        from llm.router import create_backend

        provider = provider or self._provider or "openai"
        resolved_key = api_key or os.environ.get(
            {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
             "deepseek": "DEEPSEEK_API_KEY", "groq": "GROQ_API_KEY",
             "ollama": "OLLAMA_API_KEY", "openai-compat": "OPENAI_API_KEY"}.get(provider, ""), "",
        )
        self._backend = create_backend(
            provider=provider, model=model,
            api_key=resolved_key or None,
            base_url=base_url,
        )
        self._model = model
        self._provider = provider
        self._renderer.model = model
        self._rebuild_agent()

    def _rebuild_agent(self) -> None:
        """用当前的 backend + mode 重建 agent 实例。"""
        multi_cfg = self._build_multi_config() if self._mode == "multi-agent" else None
        self.agent = create_agent(
            self._mode, self._backend, self._registry, self._agent_cfg,
            plan_approval_callback=self._plan_approval,
            memory_context=self._memory_context,
            multi_config=multi_cfg,
        )

    def _build_multi_config(self):
        """从 AppConfig 构建 MultiAgentConfig。"""
        from agent.multi_agent import MultiAgentConfig
        ma = self.config.multi_agent
        return MultiAgentConfig(
            budget_ratio=(ma.coordinator_budget_ratio, ma.sub_agent_budget_ratio),
            max_agents=ma.max_retries + 6,
            coordinator_max_steps=ma.coordinator_max_steps,
            max_parallel=ma.max_parallel_executors,
            worker_model=ma.worker_model or None,
            worker_provider=ma.worker_provider or None,
            merge_approval_callback=self._merge_approval,
            log_dir=self.log_dir,
        )

    def _merge_approval(self, worktree_name: str, diff: str) -> bool:
        """HITL: 展示 worktree diff，请求用户确认合并。"""
        import sys
        print(f"\n  ─── Worktree '{worktree_name}' diff ───")
        if diff.strip():
            lines = diff.splitlines()
            if len(lines) > 60:
                print("\n".join(lines[:60]))
                print(f"  ... ({len(lines) - 60} more lines)")
            else:
                print(diff)
        else:
            print("  (no diff)")
        print("  ─────────────────────────────────────")
        try:
            resp = input(f"  Merge '{worktree_name}'? [y/n] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return resp in ("y", "yes", "")

    def _plan_approval(self, plan_text: str) -> bool:
        """
        交互式 Plan 审批。展示 plan，等待用户 approve/reject。
        返回 True 表示批准，False 表示拒绝。
        """
        self._renderer.on_plan_generated(plan_text)
        try:
            response = input(
                _cyan_prompt("  [approve(y)/reject(n)/edit(e)] > ")
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            self._renderer.on_plan_rejected()
            return False

        if response in ("y", "yes", "approve", "a", ""):
            self._renderer.on_plan_approved()
            return True
        elif response in ("n", "no", "reject", "r"):
            self._renderer.on_plan_rejected()
            return False
        else:
            # 任何其他输入视为批准（宽容处理）
            self._renderer.on_plan_approved()
            return True

    # ------------------------------------------------------------------
    # 核心循环
    # ------------------------------------------------------------------

    def run_round(self, user_input: str) -> bool:
        """
        执行一轮对话。返回 True 表示正常结束（含 gave_up）。
        """
        from agent.event_log import EventLog
        from agent.task import Task
        from llm.base import LLMMessage

        self.round_count += 1
        self._shared_history.add(LLMMessage(role="user", content=user_input))

        # 用户新一轮输入，重置 compaction thrashing 计数器
        if hasattr(self.agent, "compactor") and hasattr(self.agent.compactor, "reset_thrashing_counter"):
            self.agent.compactor.reset_thrashing_counter()

        # 主动记忆：检测用户修正/偏好模式
        if self._proactive_memory:
            self._proactive_memory.check_user_message(user_input)

        from agent.factory import classify_task_intent
        task = Task(
            description=user_input,
            repo_path=self.repo_path,
            intent=classify_task_intent(user_input),
            max_steps=self.config.agent.max_steps,
            budget_tokens=self.config.agent.budget_tokens,
        )

        self.agent._shared_history = self._shared_history

        t0 = time.time()
        with EventLog.create(task, log_dir=self.log_dir) as log:
            result = self._run_with_renderer(task, log)
            # 自动归档到历史目录
            try:
                from entry.history_viewer import archive_log
                archive_log(log.path)
            except Exception:
                pass

        elapsed = time.time() - t0
        self.total_tokens += result.total_tokens
        self.total_steps += result.steps_taken

        if result.summary:
            self._shared_history.add(LLMMessage(
                role="assistant",
                content=result.summary,
            ))

        sys.stdout.write("\n")
        sys.stdout.flush()

        self._renderer.on_round_end(
            round_num=self.round_count,
            steps=result.steps_taken,
            tokens=result.total_tokens,
            elapsed=elapsed,
            cache_stats=result.cache_stats,
        )

        # Plan/DAG 模式执行完成后自动切回 react（一次性规划任务）
        # Multi-Agent 不切回：用户显式选择的持久对话模式
        if self._mode in ("plan", "dag"):
            self._mode = "react"
            self._renderer.mode = "react"
            self._rebuild_agent()

        return result.is_success() or result.status.value == "gave_up"

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
                if self._proactive_memory and obs.get("status") == "success":
                    self._proactive_memory.check_tool_result(
                        tool_name=tool_name,
                        params=_last_tool_params[0],
                        output=obs.get("output", ""),
                        success=True,
                    )

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
        result = agent.run(task, log)
        if hasattr(agent, "_pending_history"):
            del agent._pending_history
        return result

    # ------------------------------------------------------------------
    # 统计 & 工具方法
    # ------------------------------------------------------------------

    def compact(self, focus: str = "") -> str:
        """
        压缩当前对话历史（/compact 命令）。
        调用 ConversationCompactor 把 shared_history 压缩，
        保留首条消息，其余生成 compact 摘要块。
        同时将摘要持久化到磁盘，以便跨 session 恢复上下文。

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
        if focus:
            compactor._task_context = f"[User focus] {focus}"
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

        count = len(history_dicts)
        return f"Conversation compacted: {count} messages → 2 messages."

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
        """打印会话总统计。"""
        self._renderer.on_stats(
            rounds=self.round_count,
            total_steps=self.total_steps,
            total_tokens=self.total_tokens,
        )
