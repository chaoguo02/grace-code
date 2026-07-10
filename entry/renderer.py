"""
entry/renderer.py

TUI 渲染器体系。Claude Code 风格内联流式界面。

架构:
- RendererBase — 抽象接口
- InlineRenderer — 完整 TUI：底部状态栏、可折叠工具面板、diff 高亮、诊断着色
- PlainRenderer — 非 TTY 降级：纯文本无 ANSI

ChatSession / CLI 通过 create_renderer() 工厂函数获取实例。
"""

from __future__ import annotations

import abc
import os
import re
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any


# ---------------------------------------------------------------------------
# ANSI 工具函数
# ---------------------------------------------------------------------------

_IS_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def _green(t: str) -> str:  return _c(t, "32")
def _yellow(t: str) -> str: return _c(t, "33")
def _red(t: str) -> str:    return _c(t, "31")
def _cyan(t: str) -> str:   return _c(t, "36")
def _bold(t: str) -> str:   return _c(t, "1")
def _dim(t: str) -> str:    return _c(t, "2")
def _magenta(t: str) -> str: return _c(t, "35")
def _bg_yellow(t: str) -> str: return _c(t, "43;30")
def _bg_red(t: str) -> str: return _c(t, "41;37")


def _move_up(n: int) -> str:
    return f"\033[{n}A" if n > 0 else ""


def _clear_line() -> str:
    return "\033[2K"


def _hide_cursor() -> str:
    return "\033[?25l"


def _show_cursor() -> str:
    return "\033[?25h"


def _save_cursor() -> str:
    return "\033[s"


def _restore_cursor() -> str:
    return "\033[u"


# ---------------------------------------------------------------------------
# RendererBase — 抽象接口
# ---------------------------------------------------------------------------

class RendererBase(abc.ABC):
    """所有渲染器的基类。定义 ChatSession/CLI 所需的回调接口。"""

    def __init__(self, model: str = "?", mode: str = "react") -> None:
        self.model = model
        self.mode = mode
        self._total_steps = 0
        self._total_tokens = 0
        self._start_time = time.time()

    @abc.abstractmethod
    def stream_text(self, token: str) -> None:
        """流式输出最终回答 token。"""

    @abc.abstractmethod
    def stream_thought(self, token: str) -> None:
        """流式输出推理过程 token。"""

    @abc.abstractmethod
    def on_tool_call(self, step: int, name: str, params: dict[str, Any]) -> None:
        """工具调用开始。"""

    @abc.abstractmethod
    def on_observation(
        self, step: int, tool_name: str, status: str,
        output: str, error: str | None,
    ) -> None:
        """工具执行结果。"""

    @abc.abstractmethod
    def on_reflection(self, reason: str) -> None:
        """Agent 反思。"""

    @abc.abstractmethod
    def on_finish(self, step: int, message: str) -> None:
        """Agent 完成。"""

    @abc.abstractmethod
    def on_give_up(self, step: int, message: str) -> None:
        """Agent 放弃。"""

    @abc.abstractmethod
    def on_error(self, message: str) -> None:
        """错误。"""

    @abc.abstractmethod
    def on_round_end(
        self, round_num: int, steps: int, tokens: int, elapsed: float,
        cache_stats=None,
    ) -> None:
        """单轮结束统计。cache_stats: CacheStats | None。"""

    @abc.abstractmethod
    def on_stats(self, rounds: int, total_steps: int, total_tokens: int, **kwargs) -> None:
        """会话总统计。kwargs 可含 shared_history_messages, shared_history_tokens, context_summary。"""

    # ── Plan Mode 事件（可选，有默认实现）──────────────────────────

    def on_plan_generated(self, plan_text: str) -> None:
        """Plan 生成完毕，展示给用户审阅。"""

    def on_plan_approved(self) -> None:
        """用户批准了 Plan。"""

    def on_plan_rejected(self) -> None:
        """用户拒绝了 Plan。"""

    def on_plan_executing(self) -> None:
        """Plan 开始执行。"""


# ---------------------------------------------------------------------------
# InlineRenderer — Claude Code 风格 TUI
# ---------------------------------------------------------------------------

class InlineRenderer(RendererBase):
    """
    Claude Code 风格内联流式渲染器。

    特性：
    - 底部状态栏（模型名、token 用量、耗时、步数）
    - 工具调用黄色可折叠面板（Ctrl+O 展开/折叠）
    - diff 语法高亮（rich）
    - 诊断块红/黄 ANSI 渲染
    - 流式文本实时输出 + 完成后 markdown 重渲染
    """

    _MD_THEME = None

    def __init__(self, model: str = "?", mode: str = "react") -> None:
        super().__init__(model, mode)
        self._current_step = 0
        self._tool_panels: list[dict] = []
        self._panels_collapsed = True
        self._streaming = False
        self._stream_line_count = 0
        self._stream_buffer: list[str] = []
        self._stream_rendered_lines: int = 0
        self._status_visible = False
        self._lock = threading.Lock()
        self._round_tokens = 0
        self._round_steps = 0
        self._round_start = time.time()
        self._thought_active = False
        self._thought_line_start = True
        self._thought_buffer: list[str] = []
        self._answer_active = False
        self._answer_line_start = True
        self._init_md_theme()

    @classmethod
    def _init_md_theme(cls) -> None:
        if cls._MD_THEME is not None:
            return
        try:
            from rich.theme import Theme
            from rich.markdown import Markdown

            cls._MD_THEME = Theme({
                "markdown.h1": "bold blue",
                "markdown.h2": "bold dark_red",
                "markdown.h3": "bold dark_magenta",
                "markdown.h4": "bold black",
                "markdown.h5": "dim black",
                "markdown.h6": "dim black",
                "markdown.code": "grey37 on grey93",
                "markdown.codeblock": "grey15 on grey93",
                "markdown.block": "grey37 on grey89",
                "markdown.item.bullet": "dark_red",
                "markdown.item.number": "dark_red",
                "markdown.str": "green",
                "markdown.link": "blue underline",
                "markdown.bold": "bold black",
                "markdown.italic": "italic grey19",
            })
        except Exception:
            cls._MD_THEME = False

    # ── 状态栏 ─────────────────────────────────────────────────────

    def _terminal_width(self) -> int:
        try:
            return shutil.get_terminal_size().columns
        except Exception:
            return 80

    def _render_status_bar(self) -> str:
        elapsed = time.time() - self._round_start
        w = self._terminal_width()
        left = f" {self.model} │ {self.mode}"
        right = (
            f"step {self._current_step} │ "
            f"{self._round_tokens:,} tok │ "
            f"{elapsed:.0f}s "
        )
        padding = w - len(left) - len(right)
        if padding < 1:
            padding = 1
        bar = left + " " * padding + right
        return f"\033[7m{bar[:w]}\033[0m"

    def _draw_status_bar(self) -> None:
        if not _IS_TTY:
            return
        bar = self._render_status_bar()
        sys.stdout.write(f"\n{bar}")
        sys.stdout.write(f"\033[1A")  # move cursor back up
        sys.stdout.flush()
        self._status_visible = True

    def _clear_status_bar(self) -> None:
        if not _IS_TTY or not self._status_visible:
            return
        sys.stdout.write(f"\n{_clear_line()}\033[1A")
        sys.stdout.flush()
        self._status_visible = False

    def _refresh_status(self) -> None:
        if not _IS_TTY:
            return
        sys.stdout.write(_save_cursor())
        # move to next line, overwrite status bar
        sys.stdout.write(f"\n{_clear_line()}{self._render_status_bar()}")
        sys.stdout.write(f"\033[1A")  # back up
        sys.stdout.write(_restore_cursor())
        sys.stdout.flush()

    # ── 流式回调 ──────────────────────────────────────────────────

    def stream_text(self, token: str) -> None:
        with self._lock:
            if not self._answer_active:
                self._answer_active = True
                self._answer_line_start = True
                self._streaming = True
                self._finish_thought_block()
                self._clear_status_bar()
                self._stream_rendered_lines = 0
                sys.stdout.write("\n" + _bold(_green("💬 Answer")) + "\n")
            self._stream_buffer.append(token)
            self._stream_line_count += token.count("\n")
            for ch in token:
                if self._answer_line_start:
                    sys.stdout.write("  ")
                    self._answer_line_start = False
                sys.stdout.write(ch)
                if ch == "\n":
                    self._answer_line_start = True
            sys.stdout.flush()

    def stream_thought(self, token: str) -> None:
        with self._lock:
            if not self._thought_active:
                self._thought_active = True
                self._thought_line_start = True
                self._clear_status_bar()
                sys.stdout.write("\n" + _bold(_magenta("💭 Think")) + "\n")
            for ch in token:
                if self._thought_line_start:
                    sys.stdout.write(_dim("  "))
                    self._thought_line_start = False
                sys.stdout.write(_dim(ch))
                if ch == "\n":
                    self._thought_line_start = True
            sys.stdout.flush()

    def _render_markdown(self, text: str, *, indent: str = "") -> str:
        """Render markdown for terminal output with a polished theme."""
        if not text:
            return ""
        try:
            from rich.console import Console
            from rich.markdown import Markdown
            import io

            width = self._terminal_width()
            if indent:
                width = max(40, width - len(indent))

            buf = io.StringIO()
            kwargs: dict[str, Any] = {
                "file": buf,
                "force_terminal": _IS_TTY,
                "width": width,
                "legacy_windows": False,
            }
            if self._MD_THEME and self._MD_THEME is not False:
                kwargs["theme"] = self._MD_THEME

            console = Console(**kwargs)
            console.print(Markdown(text))
            rendered = buf.getvalue().rstrip("\n")

            if indent:
                lines = rendered.splitlines()
                indented = []
                for line in lines:
                    if line.strip():
                        indented.append(f"{indent}{line}")
                    else:
                        indented.append("")
                rendered = "\n".join(indented)
        except Exception:
            rendered = text
        return rendered

    def _erase_rendered_lines(self, line_count: int) -> None:
        """Erase previously streamed lines so markdown re-render can replace them."""
        if not _IS_TTY or line_count <= 0:
            return
        for _ in range(line_count):
            sys.stdout.write(f"\033[A\033[2K")
        sys.stdout.flush()

    # ── 工具面板 ──────────────────────────────────────────────────

    def _finish_thought_block(self) -> None:
        if self._thought_active:
            if not self._thought_line_start:
                sys.stdout.write("\n")
            sys.stdout.write("\n")
            self._thought_active = False
            self._thought_line_start = True

    def _finish_answer_block(self) -> None:
        if self._answer_active:
            if not self._answer_line_start:
                sys.stdout.write("\n")
            sys.stdout.write("\n")
            self._answer_active = False
            self._answer_line_start = True

    def _format_tool_header(self, step: int, name: str, key_info: str) -> str:
        label = _bold(_yellow(f"🛠 ToolCall [{step}] {name}"))
        if key_info:
            label += _dim(f" → {key_info}")
        return f"{label}"

    def _format_task_tool_header(self, step: int, params: dict[str, Any]) -> str:
        subagent = str(params.get("subagent_type", "?")).strip() or "?"
        description = str(params.get("description", "")).strip()
        prompt = str(params.get("prompt", "")).strip()
        lines = [f"{_bold(_yellow(f'🛠 ToolCall [{step}] task'))} {_dim('→')} {_bold(subagent)}"]
        if description:
            lines.append(_dim(f"    task: {description[:100]}"))
        if prompt:
            preview = prompt.replace("\n", " ")[:140]
            if len(prompt) > 140:
                preview += "..."
            lines.append(_dim(f"    prompt: {preview}"))
        return "\n".join(lines)

    def _parse_task_notification(self, output: str) -> dict[str, str] | None:
        text = output.strip()
        if not text.startswith("<task-notification>"):
            return None
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return self._parse_task_notification_lenient(text)
        if root.tag != "task-notification":
            return None
        def _text(tag: str) -> str:
            node = root.find(tag)
            return (node.text or "").strip() if node is not None else ""
        return {
            "agent_type": _text("agent-type"),
            "session_id": _text("session-id"),
            "status": _text("status"),
            "turns_used": _text("turns-used"),
            "error": _text("error"),
            "summary": _text("summary"),
        }

    def _parse_task_notification_lenient(self, text: str) -> dict[str, str] | None:
        """Best-effort parser for task notifications with unescaped summary text."""
        if "<task-notification>" not in text:
            return None

        def _tag(name: str) -> str:
            match = re.search(rf"<{re.escape(name)}>(.*?)</{re.escape(name)}>", text, re.DOTALL)
            return match.group(1).strip() if match else ""

        return {
            "agent_type": _tag("agent-type"),
            "session_id": _tag("session-id"),
            "status": _tag("status"),
            "turns_used": _tag("turns-used"),
            "error": _tag("error"),
            "summary": _tag("summary"),
        }

    def _format_task_observation(self, output: str, error: str | None) -> str:
        notification = self._parse_task_notification(output)
        if notification is None:
            return self._format_tool_output("success" if not error else "error", output, error)
        agent = notification.get("agent_type") or "subagent"
        status = notification.get("status") or "unknown"
        session = notification.get("session_id") or "?"
        turns = notification.get("turns_used") or "?"
        summary = notification.get("summary") or "(no summary)"
        err = notification.get("error") or error or ""
        icon = "✅" if status == "completed" else ("⚠️" if status == "partial" else "❌")
        status_text = _green(status) if status == "completed" else (_yellow(status) if status == "partial" else _red(status))
        lines = [
            f"{_bold(_cyan('🤖 Subagent'))} {_bold(agent)} {icon} {status_text}",
            _dim(f"    session: {session} · turns: {turns}"),
        ]
        if err:
            lines.append(_red(f"    error: {err}"))
        lines.append(_dim("    summary:"))
        for line in summary.splitlines()[:20]:
            lines.append(f"      {line}")
        if len(summary.splitlines()) > 20:
            lines.append(_dim(f"      ... ({len(summary.splitlines()) - 20} more lines)"))
        return "\n".join(lines)

    def _format_file_read_summary(self, tool_name: str, output: str) -> str:
        """Summarize file read/view output without dumping file contents."""
        import re

        if output.startswith("Skipped duplicate"):
            return _dim(f"    │ {output}") + "\n" + _green("    ╰─ ✓")

        first_line = output.splitlines()[0] if output else ""
        if tool_name == "file_read":
            match = re.match(r"File: (.+) \((\d+) lines total\)", first_line)
            if match:
                path, total = match.groups()
                shown = min(int(total), 500)
                return _dim(f"    │ {path}: lines 1-{shown} of {total}") + "\n" + _green("    ╰─ ✓")
        if tool_name == "file_view":
            nav_line = next((line for line in output.splitlines() if line.startswith("[Lines ")), "")
            match = re.search(r"\[Lines (\d+)[–-](\d+) of (\d+)", nav_line)
            if match:
                start, end, total = match.groups()
                return _dim(f"    │ lines {start}-{end} of {total}") + "\n" + _green("    ╰─ ✓")
        return _green("    ╰─ ✓")

    def _format_tool_output(self, status: str, output: str, error: str | None) -> str:
        lines: list[str] = []
        if status == "success":
            if output.strip():
                out_lines = output.splitlines()[:15]
                for ln in out_lines:
                    lines.append(_dim(f"    │ {ln}"))
                if len(output.splitlines()) > 15:
                    lines.append(_dim(
                        f"    │ ... ({len(output.splitlines()) - 15} more lines)"
                    ))
            lines.append(_green("    ╰─ ✓"))
        else:
            err_msg = error or output[:200]
            lines.append(_red(f"    ╰─ ✗ {err_msg}"))
        return "\n".join(lines)

    def on_tool_call(self, step: int, name: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._current_step = step
            self._round_steps = step

            if self._streaming:
                self._stream_buffer.clear()
                self._stream_rendered_lines = 0
                self._streaming = False

            self._clear_status_bar()
            self._finish_thought_block()
            self._finish_answer_block()

            key = ""
            for k in ("cmd", "path", "pattern", "symbol", "message", "query"):
                if k in params:
                    key = str(params[k])[:60]
                    break

            if name == "task":
                header = self._format_task_tool_header(step, params)
            else:
                header = self._format_tool_header(step, name, key)
            sys.stdout.write(f"{header}\n")
            sys.stdout.flush()

            self._tool_panels.append({
                "step": step, "name": name, "key": key, "params": params,
                "output": None, "status": None,
            })

            self._draw_status_bar()

    def on_observation(
        self, step: int, tool_name: str, status: str,
        output: str, error: str | None,
    ) -> None:
        with self._lock:
            self._clear_status_bar()

            silent = tool_name in {
                "file_read", "file_view", "file_write",
                "find_files", "find_symbol",
            }

            if tool_name == "task":
                sys.stdout.write(f"{self._format_task_observation(output, error)}\n")
            elif status == "success":
                if tool_name in {"file_read", "file_view"}:
                    sys.stdout.write(f"{self._format_file_read_summary(tool_name, output)}\n")
                elif silent:
                    sys.stdout.write(_green("    ╰─ ✓\n"))
                else:
                    formatted = self._format_tool_output(status, output, error)
                    sys.stdout.write(f"{formatted}\n")
                    # diff 高亮
                    if output.startswith("diff "):
                        sys.stdout.write(f"{_highlight_diff(output)}\n")
            else:
                sys.stdout.write(
                    _red(f"    ╰─ ✗ {error or output[:200]}\n")
                )

            # 诊断着色：error / warning 行
            if output and not silent:
                for line in output.splitlines()[:30]:
                    lower = line.lower()
                    if "error" in lower or "traceback" in lower:
                        # already printed in tool output
                        pass
                    elif "warning" in lower:
                        pass

            sys.stdout.flush()

            if self._tool_panels:
                self._tool_panels[-1]["output"] = output
                self._tool_panels[-1]["status"] = status

            self._draw_status_bar()

    # ── 事件回调 ──────────────────────────────────────────────────

    def on_reflection(self, reason: str) -> None:
        with self._lock:
            self._clear_status_bar()
            self._finish_thought_block()
            if self._streaming:
                if self._stream_rendered_lines > 0:
                    self._erase_rendered_lines(self._stream_rendered_lines)
                self._stream_buffer.clear()
                self._stream_rendered_lines = 0
                self._streaming = False
            sys.stdout.write(
                _yellow(f"\n  ⟳ Reflection ({reason}) — reconsidering...\n\n")
            )
            sys.stdout.flush()
            self._draw_status_bar()

    def on_finish(self, step: int, message: str) -> None:
        with self._lock:
            self._clear_status_bar()
            self._finish_thought_block()
            was_answer_streamed = self._answer_active
            self._finish_answer_block()
            streamed_text = "".join(self._stream_buffer)
            was_streaming = self._streaming
            if was_streaming:
                self._streaming = False
                self._stream_buffer.clear()

            if not was_answer_streamed:
                display_text = streamed_text or message
                if display_text:
                    rendered = self._render_markdown(display_text, indent="  ")
                    if rendered:
                        sys.stdout.write(f"\n{rendered}\n")
            sys.stdout.write(_green(f"\n✅ Finish [{step}]\n"))
            sys.stdout.flush()

    def on_give_up(self, step: int, message: str) -> None:
        with self._lock:
            self._clear_status_bar()
            self._finish_thought_block()
            streamed_text = "".join(self._stream_buffer)
            was_streaming = self._streaming
            if was_streaming:
                self._streaming = False
                self._stream_buffer.clear()

            sys.stdout.write(_red(f"\n  ✗ Gave up (step {step})\n"))
            display_text = streamed_text or message
            if display_text:
                rendered = self._render_markdown(display_text, indent="  ")
                if rendered:
                    sys.stdout.write(f"{rendered}\n")
            sys.stdout.flush()

    def on_error(self, message: str) -> None:
        with self._lock:
            self._clear_status_bar()
            sys.stdout.write(_red(f"\n  ❌ Error: {message}\n"))
            sys.stdout.flush()

    # ── 统计 ──────────────────────────────────────────────────────

    def on_round_end(
        self, round_num: int, steps: int, tokens: int, elapsed: float,
        cache_stats=None,
    ) -> None:
        with self._lock:
            self._total_steps += steps
            self._total_tokens += tokens
            self._round_tokens = 0
            self._round_steps = 0
            self._stream_buffer.clear()
            self._stream_rendered_lines = 0
            self._clear_status_bar()

            w = self._terminal_width()
            bar_char = "─"

            cache_part = ""
            if cache_stats and cache_stats.has_cache_activity:
                rate = cache_stats.cache_hit_rate
                cache_part = f" · cache {rate:.0%}"

            label = (
                f" Round {round_num} · "
                f"{steps} steps · {tokens:,} tokens{cache_part} · {elapsed:.1f}s "
            )
            side = (w - len(label)) // 2
            if side < 2:
                side = 2
            line = _dim(f"  {bar_char * side}{label}{bar_char * side}")
            sys.stdout.write(f"\n{line}\n\n")
            sys.stdout.flush()

            self._tool_panels.clear()
            self._round_start = time.time()

    def on_stats(self, rounds: int, total_steps: int, total_tokens: int, **kwargs) -> None:
        with self._lock:
            self._clear_status_bar()
            elapsed_total = time.time() - self._start_time

            w = self._terminal_width()
            border = _bold(f"{'━' * w}")
            sys.stdout.write(f"\n{border}\n")
            sys.stdout.write(_bold("  Session Summary\n"))
            sys.stdout.write(f"  {'─' * (w - 4)}\n")
            sys.stdout.write(f"    Model   : {self.model}\n")
            sys.stdout.write(f"    Mode    : {self.mode}\n")
            sys.stdout.write(f"    Rounds  : {rounds}\n")
            sys.stdout.write(f"    Steps   : {total_steps}\n")
            sys.stdout.write(f"    Tokens  : {total_tokens:,}\n")
            sys.stdout.write(f"    Time    : {elapsed_total:.1f}s\n")

            # Context breakdown (Phase 1 observability)
            hist_msgs = kwargs.get("shared_history_messages")
            hist_tokens = kwargs.get("shared_history_tokens")
            ctx_summary = kwargs.get("context_summary")
            if hist_msgs is not None:
                sys.stdout.write(f"  {'─' * (w - 4)}\n")
                sys.stdout.write(f"    History : {hist_msgs} messages, ~{hist_tokens:,} tokens\n")
            if ctx_summary:
                sys.stdout.write(f"    {ctx_summary}\n")

            sys.stdout.write(f"{border}\n\n")
            sys.stdout.flush()

    # ── Plan Mode ──────────────────────────────────────────────────

    def on_plan_generated(self, plan_text: str) -> None:
        with self._lock:
            self._clear_status_bar()
            if self._streaming:
                if self._stream_rendered_lines > 0:
                    self._erase_rendered_lines(self._stream_rendered_lines)
                self._stream_buffer.clear()
                self._stream_rendered_lines = 0
                self._streaming = False

            w = self._terminal_width()
            border = _bold(_cyan(f"{'━' * w}"))
            sys.stdout.write(f"\n{border}\n")
            sys.stdout.write(_bold(_cyan("  📋 Implementation Plan\n")))
            sys.stdout.write(_cyan(f"  {'─' * (w - 4)}\n"))
            rendered = self._render_markdown(plan_text, indent="  ")
            if rendered:
                sys.stdout.write(f"{rendered}\n")
            sys.stdout.write(f"{border}\n\n")
            sys.stdout.flush()

    def on_plan_approved(self) -> None:
        with self._lock:
            sys.stdout.write(_green(_bold("  ✓ Plan approved — executing...\n\n")))
            sys.stdout.flush()

    def on_plan_rejected(self) -> None:
        with self._lock:
            sys.stdout.write(_red(_bold("  ✗ Plan rejected\n\n")))
            sys.stdout.flush()

    def on_plan_executing(self) -> None:
        with self._lock:
            sys.stdout.write(_dim("  ▶ Executing plan...\n\n"))
            sys.stdout.flush()

    # ── 面板折叠控制 ──────────────────────────────────────────────

    def toggle_panels(self) -> None:
        """Ctrl+O 切换工具面板折叠/展开。"""
        self._panels_collapsed = not self._panels_collapsed

    def update_tokens(self, tokens: int) -> None:
        """外部更新 token 计数（供状态栏刷新）。"""
        self._round_tokens = tokens
        if _IS_TTY and not self._streaming:
            self._refresh_status()


# ---------------------------------------------------------------------------
# 兼容别名
# ---------------------------------------------------------------------------

Renderer = InlineRenderer


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def create_renderer(
    model: str = "?",
    mode: str = "react",
    **_kwargs,
) -> InlineRenderer:
    """创建渲染器实例（始终返回 InlineRenderer）。"""
    return InlineRenderer(model=model, mode=mode)


# ---------------------------------------------------------------------------
# HITL 确认 UI
# ---------------------------------------------------------------------------

def hitl_terminal_confirm(request: "Any") -> tuple[bool, str]:
    """
    Legacy 终端 HITL 确认 UI（向后兼容）。
    接收 HitlRequest，返回 (approved, note)。
    新代码应使用 permission_prompt()。
    """
    import sys

    if not sys.stdin.isatty():
        return (False, "Non-interactive terminal, auto-denied")

    params = request.params
    if "cmd" in params:
        params_display = f'cmd="{params["cmd"]}"'
    elif "path" in params:
        params_display = f'path="{params["path"]}"'
    else:
        params_str = str(params)
        if len(params_str) > 80:
            params_str = params_str[:80] + "..."
        params_display = params_str

    sys.stdout.write("\n")
    sys.stdout.write(_yellow("  ┌─ Confirmation Required ") + _yellow("─" * 34) + "\n")
    sys.stdout.write(_yellow("  │  ") + f"Tool:   {_bold(request.tool_name)}\n")
    sys.stdout.write(_yellow("  │  ") + f"Risk:   {_risk_color(request.risk_level)}\n")
    sys.stdout.write(_yellow("  │  ") + f"Params: {params_display}\n")
    if request.thought and request.thought != "(no thought)":
        thought_short = request.thought[:80]
        if len(request.thought) > 80:
            thought_short += "..."
        sys.stdout.write(_yellow("  │  ") + _dim(f'Agent:  "{thought_short}"') + "\n")
    sys.stdout.write(_yellow("  └") + _yellow("─" * 60) + "\n")
    sys.stdout.flush()

    while True:
        try:
            ans = input(_cyan("  [y]approve / [n]deny / [n: reason] > ")).strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return (False, "")

        if ans.lower() in ("y", "yes", ""):
            sys.stdout.write(_green("  ✓ Approved\n\n"))
            sys.stdout.flush()
            return (True, "")
        elif ans.lower() in ("n", "no"):
            sys.stdout.write(_red("  ✗ Denied\n\n"))
            sys.stdout.flush()
            return (False, "")
        elif ans.lower().startswith("n:") or ans.lower().startswith("n "):
            note = ans[2:].strip()
            sys.stdout.write(_red(f"  ✗ Denied") + _dim(f" ({note})\n\n"))
            sys.stdout.flush()
            return (False, note)
        else:
            sys.stdout.write(_dim("  (enter y, n, or n: <reason>)\n"))


def permission_prompt(request: "Any") -> "Any":
    """
    3-way terminal permission prompt (aligned with Claude Code).

    显示格式：
      ┌─ Permission Required ──────────────
      │  Tool:   shell
      │  Params: cmd="git commit -m 'fix'"
      │  Agent:  "I need to commit..."
      └──────────────────────────────────────
      [a]llow once / always [A]llow / [d]eny >

    Returns PromptDecision(action, note, inferred_rule).
    """
    import sys
    from hitl.pipeline import PromptDecision
    from hitl.pattern_inference import infer_permission_pattern

    if not sys.stdin.isatty():
        return PromptDecision(action="deny", note="Non-interactive terminal")

    params = request.params
    if "cmd" in params:
        params_display = f'cmd="{params["cmd"]}"'
    elif "path" in params:
        params_display = f'path="{params["path"]}"'
    else:
        params_str = str(params)
        if len(params_str) > 80:
            params_str = params_str[:80] + "..."
        params_display = params_str

    sys.stdout.write("\n")
    sys.stdout.write(_yellow("  ┌─ Permission Required ") + _yellow("─" * 36) + "\n")
    sys.stdout.write(_yellow("  │  ") + f"Tool:   {_bold(request.tool_name)}\n")
    sys.stdout.write(_yellow("  │  ") + f"Params: {params_display}\n")
    thought = getattr(request, "thought", "")
    if thought and thought != "(no thought)":
        thought_short = thought[:80]
        if len(thought) > 80:
            thought_short += "..."
        sys.stdout.write(_yellow("  │  ") + _dim(f'Agent:  "{thought_short}"') + "\n")
    sys.stdout.write(_yellow("  └") + _yellow("─" * 60) + "\n")
    sys.stdout.flush()

    while True:
        try:
            ans = input(_cyan("  [a]llow once / always [A]llow / [d]eny > ")).strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return PromptDecision(action="deny")

        if ans.lower() in ("a", "y", "yes", "allow", ""):
            sys.stdout.write(_green("  ✓ Allowed (once)\n\n"))
            sys.stdout.flush()
            return PromptDecision(action="allow_once")

        elif ans in ("A",) or ans.lower() in ("always", "aa"):
            rule = infer_permission_pattern(request.tool_name, request.params)
            sys.stdout.write(_green(f"  ✓ Always allow: ") + _dim(rule.raw) + "\n\n")
            sys.stdout.flush()
            return PromptDecision(action="always_allow", inferred_rule=rule)

        elif ans.lower() in ("d", "n", "no", "deny"):
            sys.stdout.write(_red("  ✗ Denied\n\n"))
            sys.stdout.flush()
            return PromptDecision(action="deny")

        elif ans.lower().startswith("d:") or ans.lower().startswith("n:"):
            note = ans[2:].strip()
            sys.stdout.write(_red(f"  ✗ Denied") + _dim(f" ({note})\n\n"))
            sys.stdout.flush()
            return PromptDecision(action="deny", note=note)

        else:
            sys.stdout.write(_dim("  (enter a, A, d, or d: <reason>)\n"))


def _risk_color(risk: str) -> str:
    """根据风险等级返回带颜色的标签。"""
    if risk == "high":
        return _red(_bold("HIGH"))
    elif risk == "medium":
        return _yellow("MEDIUM")
    elif risk == "low":
        return _dim("low")
    return _dim("none")


# ---------------------------------------------------------------------------
# diff 高亮（rich 可用时）
# ---------------------------------------------------------------------------

def _highlight_diff(text: str) -> str:
    """给 diff 文本加 ANSI 颜色。rich 不可用时静默降级。"""
    try:
        from rich.syntax import Syntax
        from rich.console import Console
        import io
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        syntax = Syntax(text, "diff", theme="monokai")
        console.print(syntax)
        return buf.getvalue().rstrip()
    except Exception:
        return text


# ---------------------------------------------------------------------------
# 诊断着色
# ---------------------------------------------------------------------------

def format_diagnostic(text: str) -> str:
    """对诊断输出中的 error/warning 行进行着色。"""
    lines = []
    for line in text.splitlines():
        lower = line.lower()
        if "error" in lower or "traceback" in lower or "failed" in lower:
            lines.append(_red(line))
        elif "warning" in lower or "warn" in lower:
            lines.append(_yellow(line))
        else:
            lines.append(line)
    return "\n".join(lines)