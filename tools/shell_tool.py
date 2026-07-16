"""
tools/shell_tool.py

Shell 命令执行工具。

安全模型：
- L0 安全底线：拒绝明显破坏性命令（硬拦截，防御纵深）
- 读写权限判断：不再使用工具内部字符串白名单/黑名单。
  改为框架层通过 PhasePolicy.allowed_effects 声明式控制——
  PolicyAwareToolRegistry._is_tool_visible() 在注册时过滤。
- 用户确认：PermissionPipeline 统一处理，工具层不自行判断。
- Timeout + 输出截断：防挂起、防上下文爆炸。
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Callable

from core.base import BaseTool, ToolEffect, ToolMetadata, ToolResult
from runtime.process import LocalRuntime, Runtime
from core.utils import truncate_output


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

MAX_OUTPUT_CHARS = 50_000

# L0 安全底线 — 硬拦截破坏性命令（永不执行，防御纵深最后一层）
_BLOCKED_PATTERNS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",       # fork bomb
    "chmod -R 777 /",
    "chown -R",
    "> /dev/sda",
)

# 确认回调类型：接收命令字符串，返回 True=允许 / False=拒绝
ConfirmCallback = Callable[[str], bool]


# ---------------------------------------------------------------------------
# ShellTool
# ---------------------------------------------------------------------------

class ShellTool(BaseTool):
    metadata = ToolMetadata(effects=frozenset({ToolEffect.EXECUTE}))
    """
    执行 shell 命令，返回 stdout + stderr。

    params:
        command (str): 可执行程序名（推荐，shell=False，参数隔离）
        args (list):   参数列表
        cmd (str):     shell 命令字符串（legacy，shell=True）
        timeout (int): 超时秒数（默认 30）
        cwd (str):     工作目录（默认使用当前目录）

    安全模型：
        - L0 安全底线硬拦截：execute() 内 _check_blocked()
        - 读写权限由框架层 PhasePolicy.allowed_effects 声明式控制
        - 用户确认由 PermissionPipeline 统一处理
    """

    def __init__(
        self,
        confirm_callback: ConfirmCallback | None = None,
        runtime: Runtime | None = None,
    ) -> None:
        self._confirm_callback = confirm_callback
        self._runtime = runtime or LocalRuntime()

    aliases = ("shell",)

    @property
    def name(self) -> str:
        return "Bash"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output (stdout + stderr combined). "
            "Timeout is 30s by default. "
            "CRITICAL: Use the 'cwd' parameter to set the working directory. "
            "Never write 'cd /some/path' in the command string — use cwd instead. "
            "RESTRICTION: Do NOT use this tool to read files (use file_read instead) "
            "or modify files (use file_edit / file_write instead). "
            "Use shell ONLY for operations that have no dedicated tool: "
            "running tests, git commands, builds, package managers, etc."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The program to run (e.g., 'pytest', 'git', 'ls'). PREFERRED over 'cmd' — uses parameterized execution with shell=False.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments as separate items. Each item is ONE argument, never parsed by shell.",
                },
                "cmd": {
                    "type": "string",
                    "description": "DEPRECATED. Use 'command' + 'args' instead. Shell command string (legacy).",
                    "deprecated": True,
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of what this command does (shown in permission prompts)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120, max 600)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional)",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Set to true to run this command in the background. For long-running processes like dev servers.",
                },
                "dangerouslyDisableSandbox": {
                    "type": "boolean",
                    "description": "Set to true to override sandbox mode and run without sandboxing. Requires explicit user confirmation.",
                },
            },
            "required": [],
        }

    @property
    def risk_level(self) -> str:
        from core.base import RiskLevel
        return RiskLevel.HIGH

    def classify_risk(self, params: dict[str, Any]) -> str:
        """Dynamic risk classification: shell is always HIGH by default.

        PermissionPipeline refines this with per-command rules.
        """
        from core.base import RiskLevel
        return RiskLevel.HIGH

    def _build_cmd_repr(self, params: dict[str, Any]) -> str:
        """Build a string representation for safety checks (L0/L1/L2)."""
        command = params.get("command", "")
        args = params.get("args", [])
        if command:
            return f"{command} {' '.join(args)}" if args else command
        return params.get("cmd", "")

    def permission_denial_reason(self, params: dict[str, Any]) -> str | None:
        cmd = self._build_cmd_repr(params)
        blocked = _check_blocked(cmd)
        if blocked:
            return f"Blocked by safety floor: matched '{blocked}'"
        if "\x00" in cmd or len(cmd) > 10_000:
            return "Blocked: malicious input detected"
        return None

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cmd: str = params.get("cmd", "").strip()
        command: str = params.get("command", "").strip()
        args: list[str] = params.get("args", [])
        timeout: int = int(params.get("timeout", 30))
        cwd: str | None = params.get("cwd", None)

        # Prefer parameterized execution (command+args) over legacy (cmd)
        if command:
            return self._execute_parameterized(command, args, timeout, cwd)
        if cmd:
            return self._execute_legacy(cmd, timeout, cwd)

        return ToolResult(success=False, output="", error="Either 'command' or 'cmd' is required")

    def _execute_parameterized(self, command: str, args: list[str], timeout: int, cwd: str | None) -> ToolResult:
        """Execute via Runtime.execute() — shell=False, physically isolated parameters."""
        cmd_repr = f"{command} {' '.join(args)}" if args else command

        # L0 safety floor
        blocked = _check_blocked(cmd_repr)
        if blocked:
            return ToolResult(
                success=False, output="",
                error=f"Command blocked for safety: matched '{blocked}'",
            )

        from runtime.process import RunResult
        run_result: RunResult = self._runtime.execute(
            command, args=args, cwd=cwd, timeout=timeout,
        )

        return self._build_result(run_result, cmd_repr)

    def _execute_legacy(self, cmd: str, timeout: int, cwd: str | None) -> ToolResult:
        """Execute via Runtime.exec() — shell=True, backward compatible."""
        # L0 safety floor
        blocked = _check_blocked(cmd)
        if blocked:
            return ToolResult(
                success=False, output="",
                error=f"Command blocked for safety: matched '{blocked}'",
            )

        return self._run(cmd, timeout, cwd)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _run(self, cmd: str, timeout: int, cwd: str | None) -> ToolResult:
        """Execute via Runtime.exec() — legacy path with shell=True."""
        result = self._runtime.exec(cmd, cwd=cwd, timeout=timeout)
        return self._build_result(result, cmd)

    def _build_result(self, run_result: "Any", cmd_repr: str) -> ToolResult:
        """Convert RunResult to ToolResult with proper error classification."""
        from core.base import classify_runtime_error
        output = truncate_output(run_result.output, MAX_OUTPUT_CHARS)
        if not run_result.success:
            _tool_err = classify_runtime_error(run_result, cmd_repr)
            return ToolResult(
                success=False, output=output,
                error=_tool_err.to_message() if _tool_err else f"Exit code: {run_result.returncode}",
                tool_error=_tool_err,
            )
        return ToolResult(success=True, output=output)


# ---------------------------------------------------------------------------
# 辅助函数（对外暴露供测试）
# ---------------------------------------------------------------------------

def _check_blocked(cmd: str) -> str | None:
    """返回匹配到的 L0 安全底线 pattern，没有匹配返回 None。"""
    cmd_lower = cmd.lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern.lower() in cmd_lower:
            return pattern
    return None


# ---------------------------------------------------------------------------
# 终端确认函数（在 cli/chat 里直接使用）
# ---------------------------------------------------------------------------

def terminal_confirm(cmd: str) -> bool:
    """
    在终端显示命令并等待用户确认。
    返回 True 表示允许，False 表示拒绝。

    显示格式：
        ⚠  Agent wants to run:
           $ git commit -m "fix parser"
        Allow? [y/N/a(lways)] _
    """
    import sys

    # 判断是否在交互式终端
    if not sys.stdin.isatty():
        # 非交互式（pipe / CI）：默认拒绝，避免意外执行
        print(f"\n[confirm] Non-interactive terminal, rejecting: {cmd!r}", flush=True)
        return False

    print(f"\n\033[33m  ⚠  Agent wants to run:\033[0m")
    print(f"     \033[1m$ {cmd}\033[0m")

    while True:
        try:
            ans = input("  Allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            print("  \033[31m✗ Rejected\033[0m")
            return False
        print("  Please enter y or n.")


def always_allow(cmd: str) -> bool:
    """跳过确认，直接允许（用于 --no-confirm 模式）。"""
    return True


def always_deny(cmd: str) -> bool:
    """跳过确认，直接拒绝（用于测试或 CI 模式）。"""
    return False
