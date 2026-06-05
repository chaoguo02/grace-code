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
        self.agent = create_agent(
            self._mode, self._backend, self._registry, self._agent_cfg,
            plan_approval_callback=self._plan_approval,
        )
        self._shared_history = ConversationHistory(
            max_messages=config.context.history_window * 2,
        )

        self.total_tokens = 0
        self.total_steps = 0
        self.round_count = 0

    def switch_mode(self, mode: str) -> None:
        """运行时切换 agent 模式（react / plan / auto）。"""
        if mode not in ("react", "plan", "auto"):
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
        self.agent = create_agent(
            self._mode, self._backend, self._registry, self._agent_cfg,
            plan_approval_callback=self._plan_approval,
        )

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

        task = Task(
            description=user_input,
            repo_path=self.repo_path,
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
                content=f"[Round {self.round_count} complete]\n{result.summary}",
            ))

        sys.stdout.write("\n")
        sys.stdout.flush()

        self._renderer.on_round_end(
            round_num=self.round_count,
            steps=result.steps_taken,
            tokens=result.total_tokens,
            elapsed=elapsed,
        )

        return result.is_success() or result.status.value == "gave_up"

    def _run_with_renderer(self, task, log):
        """运行 agent，通过 monkey-patch EventLog 实现事件实时输出。"""
        from agent.task import EventType

        original_append = log._append
        # 记录当前工具名，供 observation 使用
        _last_tool_name = [""]

        def _handle_event(event):
            etype = event.event_type
            p = event.payload

            if etype == EventType.ACTION:
                action = p["action"]
                atype = action.get("action_type", "")
                tc = action.get("tool_call")

                if tc:
                    _last_tool_name[0] = tc["name"]
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
                self._renderer.on_observation(
                    step=p["step"],
                    tool_name=obs.get("tool_name", _last_tool_name[0]),
                    status=obs.get("status", ""),
                    output=obs.get("output", ""),
                    error=obs.get("error"),
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
        """注入共享 history 后运行 agent。"""
        agent = self.agent
        agent._pending_history = self._shared_history
        result = agent.run(task, log)
        if hasattr(agent, "_pending_history"):
            del agent._pending_history
        return result

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def print_stats(self) -> None:
        """打印会话总统计。"""
        self._renderer.on_stats(
            rounds=self.round_count,
            total_steps=self.total_steps,
            total_tokens=self.total_tokens,
        )
