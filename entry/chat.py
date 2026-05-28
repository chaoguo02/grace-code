"""
entry/chat.py

交互对话模式。持续会话，每轮用户输入后 agent 继续工作，
history 跨轮保留，像 Claude Code 一样可以持续对话。

架构设计：
- ChatSession 持有 backend / registry / history，跨轮复用
- 每轮创建一个新 Task，但 history 通过 agent._inject_history() 延续
- EventLog 每轮独立（方便单轮审计），但统计累计显示
- 实时打印：每条 event 写入 log 后立刻 echo，不等跑完

用法：
    agent chat --repo /path/to/repo
    agent chat --repo . --model deepseek-chat
"""

from __future__ import annotations

import time
import sys
from pathlib import Path
from typing import Callable

import click

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# 彩色输出（复用 cli.py 的风格）
# ---------------------------------------------------------------------------

def _c(t: str, code: str) -> str:
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t

def green(t: str) -> str:  return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str:    return _c(t, "31")
def cyan(t: str) -> str:   return _c(t, "36")
def bold(t: str) -> str:   return _c(t, "1")
def dim(t: str) -> str:    return _c(t, "2")
def magenta(t: str) -> str: return _c(t, "35")


# ---------------------------------------------------------------------------
# 实时 event 打印（比 cli.py 的版本更简洁，适合持续对话）
# ---------------------------------------------------------------------------

def _print_event_live(event) -> None:
    """每条 event 写入 log 后立刻调用，实时显示。"""
    from agent.task import EventType
    etype = event.event_type
    p = event.payload

    if etype == EventType.ACTION:
        step = p["step"]
        action = p["action"]
        thought = (action.get("thought") or "").strip()
        atype = action.get("action_type", "")
        tc = action.get("tool_call")

        # 流式模式：thought 已经被 stream_callback 实时打印出来了
        # 非流式模式或 thought 为空时，在这里补打
        # finish/give_up 的 thought 就是回答内容，已在 TASK_COMPLETE 里显示，不重复
        if thought and thought != "(no thought)" and atype not in ("finish", "give_up"):
            # 流式时 thought 已打印，只需换行；非流式时完整打印
            import sys
            sys.stdout.write("\n")   # 确保工具调用从新行开始
            sys.stdout.flush()

        if tc:
            _print_event_live._last_tool_name = tc['name']  # 供 observation 判断
            click.echo(cyan(f"  [{step}] {tc['name']}"), nl=False)
            # 打印关键参数
            params = tc.get("params", {})
            key_param = (
                params.get("cmd")
                or params.get("path")
                or params.get("pattern")
                or params.get("symbol")
                or params.get("message")
                or ""
            )
            if key_param:
                short_param = str(key_param)[:60]
                suffix = "..." if len(str(key_param)) > 60 else ""
                click.echo(cyan(f"  {short_param}{suffix}"))
            else:
                click.echo()
        elif atype == "finish":
            click.echo(green(f"\n  [{step}] ✓ finish"))
            # 把 message 存到全局，供 TASK_COMPLETE event 打印
            _finish_message = action.get("message", "") or ""
            _print_event_live._pending_message = _finish_message
        elif atype == "give_up":
            click.echo(red(f"\n  [{step}] ✗ give_up"))

    elif etype == EventType.OBSERVATION:
        obs = p["observation"]
        status = obs.get("status", "")
        output = (obs.get("output") or "").strip()
        error = obs.get("error")

        # 从上一条 action event 取工具名（_last_tool_name 由 ACTION 分支设置）
        tool_name = getattr(_print_event_live, "_last_tool_name", "")

        # 只读类工具：只显示 ✓ 或 ✗，不打印内容（内容已被模型读取，用户不需要看）
        SILENT_TOOLS = {"file_read", "file_view", "file_write", "find_files", "find_symbol"}
        silent = tool_name in SILENT_TOOLS

        if status == "success":
            if silent:
                click.echo(green("  ✓"))
            else:
                lines = output.splitlines()
                MAX_PREVIEW = 20
                preview = "\n".join(f"    {l}" for l in lines[:MAX_PREVIEW])
                if lines:
                    click.echo(green("  ✓") + dim(f"\n{preview}"))
                    if len(lines) > MAX_PREVIEW:
                        click.echo(dim(f"    ... ({len(lines)-MAX_PREVIEW} more lines)"))
                else:
                    click.echo(green("  ✓"))
        else:
            click.echo(red(f"  ✗ {error or output[:120]}"))

    elif etype == EventType.REFLECTION:
        reason = p.get("reason", "")
        click.echo(yellow(f"\n  ⟳ Reflection ({reason}) — reconsidering approach...\n"))

    elif etype == EventType.TASK_COMPLETE:
        # 取出 finish action 存的 message
        message = getattr(_print_event_live, "_pending_message", "")
        _print_event_live._pending_message = ""

        if message:
            # 获取流式打印的 thought（存在 stream_callback 里）
            streamed = getattr(_print_event_live, "_streamed_thought", "").strip()
            msg_stripped = message.strip()

            if msg_stripped and msg_stripped != streamed:
                # thought 和 message 不同（如 Claude）→ 单独打印最终回答
                import sys
                sys.stdout.write("\n")
                sys.stdout.flush()
                click.echo(msg_stripped)
            # thought == message（如 DeepSeek flash）→ 已经流式打印过，不重复

    elif etype == EventType.TASK_FAILED:
        reason = p.get("reason", "")
        click.echo(red(bold(f"\n  ❌ Failed: {reason}")))


# ---------------------------------------------------------------------------
# ChatSession — 跨轮持久化的会话状态
# ---------------------------------------------------------------------------

class ChatSession:
    """
    持久化会话。跨多轮对话保留：
    - backend / registry（不变）
    - ConversationHistory（核心：让 agent 记得之前做了什么）
    - 累计 token / 步数统计
    - repo_map 缓存（换 repo 时自动失效）
    """

    def __init__(self, backend, registry, config, repo_path: str, log_dir: str, confirm_callback=None) -> None:
        from agent.core import Agent, AgentConfig
        from context.history import ConversationHistory

        self.repo_path = repo_path
        self.log_dir = log_dir
        self.config = config
        self._confirm_callback = confirm_callback

        # 流式回调：每个 token 立刻 flush 到终端
        _stream_started = [False]
        _thought_printed = [False]  # 标记是否打过 thought，用于 message 前换行
        _streamed_buf = []   # 记录流式打印的内容，用于和 message 比较

        def _thought_cb(text: str) -> None:
            """推理过程：dim 暗色，表示模型在思考"""
            import sys
            if not _stream_started[0]:
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _stream_started[0] = True
            sys.stdout.write(dim(text))
            sys.stdout.flush()
            _thought_printed[0] = True

        def _stream_cb(text: str) -> None:
            """最终回答：正常亮色"""
            import sys
            if not _stream_started[0]:
                # 第一次打 message，之前没打过任何内容
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _stream_started[0] = True
            elif _thought_printed[0]:
                # 之前打过 thought，先换两行作为分隔再打 message
                sys.stdout.write("\n\n")
                sys.stdout.flush()
                _thought_printed[0] = False  # 只换一次
            sys.stdout.write(text)
            sys.stdout.flush()
            _streamed_buf.append(text)
            _print_event_live._streamed_thought = "".join(_streamed_buf)

        agent_cfg = AgentConfig(
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
        self.agent = Agent(backend, registry, agent_cfg)
        self._shared_history = ConversationHistory(
            max_messages=config.context.history_window * 2
        )

        # 累计统计
        self.total_tokens = 0
        self.total_steps = 0
        self.round_count = 0

    def run_round(self, user_input: str) -> bool:
        """
        执行一轮对话。

        Args:
            user_input: 用户这轮的输入

        Returns:
            True 表示成功/正常结束，False 表示失败
        """
        from agent.core import AgentConfig
        from agent.event_log import EventLog
        from agent.task import Task
        from llm.base import LLMMessage

        self.round_count += 1

        # 把用户输入追加到共享 history
        self._shared_history.add(LLMMessage(role="user", content=user_input))

        # 构建这轮的 Task（repo_path 固定，description 用用户输入）
        task = Task(
            description=user_input,
            repo_path=self.repo_path,
            max_steps=self.config.agent.max_steps,
            budget_tokens=self.config.agent.budget_tokens,
        )

        # 把共享 history 注入 agent（替换它内部的 history）
        # 通过 monkey-patch _shared_history 实现跨轮续接
        self.agent._shared_history = self._shared_history

        t0 = time.time()
        with EventLog.create(task, log_dir=self.log_dir) as log:
            # 实时打印：每条 event 写入后立刻 echo
            result = self._run_with_live_print(task, log)

        elapsed = time.time() - t0
        self.total_tokens += result.total_tokens
        self.total_steps += result.steps_taken

        # 把 agent 这轮的最后回复追加到共享 history
        # 这样下一轮 agent 能看到自己上一轮说了什么
        if result.summary:
            self._shared_history.add(LLMMessage(
                role="assistant",
                content=f"[Round {self.round_count} complete]\n{result.summary}",
            ))

        # 流式输出结束后打换行，重置 readline 的行状态
        import sys as _sys
        _sys.stdout.write("\n")
        _sys.stdout.flush()

        # 打印轮次统计
        click.echo(dim(
            f"  ─── Round {self.round_count} · "
            f"{result.steps_taken} steps · "
            f"{result.total_tokens:,} tokens · "
            f"{elapsed:.1f}s ───"
        ))

        return result.is_success() or result.status.value == "gave_up"

    def _run_with_live_print(self, task, log):
        """
        运行 agent，同时实时打印 event。

        因为 agent.run() 是同步的（跑完才返回），
        我们通过 monkey-patch EventLog._append 来实现"写入即打印"。
        """
        original_append = log._append

        def live_append(event):
            original_append(event)
            _print_event_live(event)

        log._append = live_append

        # 把共享 history 传给 agent
        # agent.run() 会重建 history，我们需要在它初始化后注入
        # 通过重写 _build_messages 的 history 参数实现
        return self._run_injecting_history(task, log)

    def _run_injecting_history(self, task, log):
        """
        运行 agent，并在第一步前把共享 history 注入进去。

        核心 trick：patch agent 的 run() 方法，在它初始化完
        ConversationHistory 之后、第一次调用 LLM 之前，
        把我们的共享 history 替换进去。
        """
        from context.history import ConversationHistory
        from agent.prompt import build_task_prompt
        from llm.base import LLMMessage

        agent = self.agent

        # 直接调用 agent.run()，但在它内部：
        # 1. agent.run() 会创建新的 ConversationHistory
        # 2. 我们通过 patch core.py 里的 ConversationHistory 构造器来替换它
        # 更简洁的方式：给 agent 加一个 _initial_history 属性，run() 里检查它

        # 方案：给 agent 设一个 _pending_history，
        # 然后在 core.py 里的 run() 读取它（见下面的 patch）
        agent._pending_history = self._shared_history

        result = agent.run(task, log)

        # 清除，避免影响下轮
        if hasattr(agent, "_pending_history"):
            del agent._pending_history

        return result

    def print_stats(self) -> None:
        """打印会话总统计。"""
        click.echo(bold(f"\n{'─'*50}"))
        click.echo(f"  Session stats:")
        click.echo(f"    Rounds  : {self.round_count}")
        click.echo(f"    Steps   : {self.total_steps}")
        click.echo(f"    Tokens  : {self.total_tokens:,}")
        click.echo(bold(f"{'─'*50}\n"))
