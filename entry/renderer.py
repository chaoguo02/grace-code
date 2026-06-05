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
import shutil
import sys
import threading
import time
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
    ) -> None:
        """单轮结束统计。"""

    @abc.abstractmethod
    def on_stats(self, rounds: int, total_steps: int, total_tokens: int) -> None:
        """会话总统计。"""

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
    - 流式文本实时输出
    """

    def __init__(self, model: str = "?", mode: str = "react") -> None:
        super().__init__(model, mode)
        self._current_step = 0
        self._tool_panels: list[dict] = []
        self._panels_collapsed = True
        self._streaming = False
        self._stream_line_count = 0
        self._status_visible = False
        self._lock = threading.Lock()
        self._round_tokens = 0
        self._round_steps = 0
        self._round_start = time.time()

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
            if not self._streaming:
                self._streaming = True
                self._clear_status_bar()
            sys.stdout.write(token)
            sys.stdout.flush()

    def stream_thought(self, token: str) -> None:
        with self._lock:
            if not self._streaming:
                self._streaming = True
                self._clear_status_bar()
            sys.stdout.write(_dim(token))
            sys.stdout.flush()

    # ── 工具面板 ──────────────────────────────────────────────────

    def _format_tool_header(self, step: int, name: str, key_info: str) -> str:
        icon = "─"
        prefix = _yellow(f"  {icon} ")
        label = _bold(_yellow(f"[{step}] {name}"))
        if key_info:
            label += _dim(f" {key_info}")
        return f"{prefix}{label}"

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
                sys.stdout.write("\n\n")
                self._streaming = False

            self._clear_status_bar()

            key = ""
            for k in ("cmd", "path", "pattern", "symbol", "message", "query"):
                if k in params:
                    key = str(params[k])[:60]
                    break

            header = self._format_tool_header(step, name, key)
            sys.stdout.write(f"{header}\n")
            sys.stdout.flush()

            self._tool_panels.append({
                "step": step, "name": name, "key": key,
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

            if status == "success":
                if silent:
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
            if self._streaming:
                sys.stdout.write("\n")
                self._streaming = False
            sys.stdout.write(
                _yellow(f"\n  ⟳ Reflection ({reason}) — reconsidering...\n\n")
            )
            sys.stdout.flush()
            self._draw_status_bar()

    def on_finish(self, step: int, message: str) -> None:
        with self._lock:
            self._clear_status_bar()
            if self._streaming:
                sys.stdout.write("\n")
                self._streaming = False
            sys.stdout.write(_green(f"\n  ✓ Done (step {step})\n"))
            sys.stdout.flush()

    def on_give_up(self, step: int, message: str) -> None:
        with self._lock:
            self._clear_status_bar()
            if self._streaming:
                sys.stdout.write("\n")
                self._streaming = False
            sys.stdout.write(_red(f"\n  ✗ Gave up (step {step})\n"))
            if message:
                sys.stdout.write(_red(f"  {message}\n"))
            sys.stdout.flush()

    def on_error(self, message: str) -> None:
        with self._lock:
            self._clear_status_bar()
            sys.stdout.write(_red(f"\n  ❌ Error: {message}\n"))
            sys.stdout.flush()

    # ── 统计 ──────────────────────────────────────────────────────

    def on_round_end(
        self, round_num: int, steps: int, tokens: int, elapsed: float,
    ) -> None:
        with self._lock:
            self._total_steps += steps
            self._total_tokens += tokens
            self._round_tokens = 0
            self._round_steps = 0
            self._clear_status_bar()

            w = self._terminal_width()
            bar_char = "─"
            label = (
                f" Round {round_num} · "
                f"{steps} steps · {tokens:,} tokens · {elapsed:.1f}s "
            )
            side = (w - len(label)) // 2
            if side < 2:
                side = 2
            line = _dim(f"  {bar_char * side}{label}{bar_char * side}")
            sys.stdout.write(f"\n{line}\n\n")
            sys.stdout.flush()

            self._tool_panels.clear()
            self._round_start = time.time()

    def on_stats(self, rounds: int, total_steps: int, total_tokens: int) -> None:
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
            sys.stdout.write(f"{border}\n\n")
            sys.stdout.flush()

    # ── Plan Mode ──────────────────────────────────────────────────

    def on_plan_generated(self, plan_text: str) -> None:
        with self._lock:
            self._clear_status_bar()
            if self._streaming:
                sys.stdout.write("\n")
                self._streaming = False

            w = self._terminal_width()
            border = _bold(_cyan(f"{'━' * w}"))
            sys.stdout.write(f"\n{border}\n")
            sys.stdout.write(_bold(_cyan("  📋 Implementation Plan\n")))
            sys.stdout.write(_cyan(f"  {'─' * (w - 4)}\n"))
            for line in plan_text.splitlines():
                sys.stdout.write(f"  {line}\n")
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
