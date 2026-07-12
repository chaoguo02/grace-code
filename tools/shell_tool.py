"""
tools/shell_tool.py

Shell 命令执行工具。四层防护：
1. 黑名单：拒绝明显破坏性命令（硬拦截，不可绕过）
2. 白名单：只读命令免确认直接执行
3. 权限确认：写操作等待用户 y/n（可通过 confirm_callback 注入）
4. Timeout + 输出截断：防挂起、防上下文爆炸

权限确认设计：
- confirm_callback 是一个 Callable[[str], bool]，返回 True 表示允许
- 默认 None（不确认，直接执行）——用于 run 模式
- chat 模式 / 交互模式传入真实的终端确认函数
- 测试时传入 mock，不需要真实终端
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Callable

from tools.base import BaseTool, ToolResult
from tools.runtime import LocalRuntime, Runtime
from tools.utils import truncate_output


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

MAX_OUTPUT_CHARS = 50_000

# 硬拦截黑名单（永不执行，不问用户）
_BLOCKED_PATTERNS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",       # fork bomb
    "chmod -R 777 /",
    "chown -R",
    "> /dev/sda",
    # Process-blocking commands (agent hangs forever)
    "sleep ",
    "tail -f",
    "watch ",
    "ping ",
    "tcpdump",
)

# 只读命令前缀白名单（直接执行，不询问）
_READONLY_PREFIXES: tuple[str, ...] = (
    "ls", "ll", "la",
    "cat", "head", "tail", "less", "more",
    "echo", "printf",
    "pwd", "whoami", "which", "type",
    "find", "locate",
    "grep", "egrep", "fgrep", "rg", "ag",
    "wc", "sort", "uniq", "cut", "awk", "sed -n",
    "diff", "diff3",
    "file", "stat",
    "python -c", "python3 -c",
    "python -m pytest", "python3 -m pytest", "pytest",
    "git status", "git diff", "git log", "git show",
    "git branch", "git tag", "git remote",
    "git stash list",
    "tree",
    "env", "printenv",
    "ps", "top", "htop",
    "df", "du",
    "uname", "hostname",
    "date", "cal",
    "man", "help",
)

# 需要确认的危险命令关键词（白名单之外且包含这些词时必须确认）
_CONFIRM_KEYWORDS: tuple[str, ...] = (
    "rm ", "rmdir",
    "mv ",
    "cp -r", "cp -f",
    "chmod", "chown",
    "pip install", "pip uninstall",
    "npm install", "npm uninstall",
    "git commit", "git push", "git reset",
    "git checkout", "git merge", "git rebase",
    "git clean",
    "sudo",
    "curl", "wget",            # 网络请求
    "kill", "pkill", "killall",
    "shutdown", "reboot",
    "docker", "kubectl",
    "make", "make install",
    "> ",                      # 重定向覆盖（>> 追加不拦截）
    "| tee ",
)

# ── Read-only Shell: patterns blocked in analysis/plan mode ──
# "Read-only" is NOT a boolean — it's a COMMAND-LEVEL whitelist.
# When shell_read_only is True, any command matching these patterns
# is blocked BEFORE execution. The LLM sees the Shell tool but
# cannot use it for writes.
_READ_ONLY_BLOCKED: tuple[str, ...] = (
    ">", ">>", "2>", "1>",           # output redirects
    "rm ", "del ", "rmdir",          # delete files/dirs
    "cp ", "copy ", "mv ", "move ",  # file operations
    "ren ", "rename ",
    "mkdir ", "md ",
    "curl ", "wget ",                # network downloads
    "pip install", "npm install",    # package installation
    "git add", "git commit",         # git writes
    "git push", "git stash push",
    "chmod", "chown", "attrib",     # permission changes
    "sudo ", "su ",                  # privilege escalation
    "shutdown", "reboot",
    "docker ", "kubectl ",
    "make ", "make install",
    "pip uninstall", "npm uninstall",
    "format ", "diskpart",
)

# 确认回调类型：接收命令字符串，返回 True=允许 / False=拒绝
ConfirmCallback = Callable[[str], bool]


# ---------------------------------------------------------------------------
# ShellTool
# ---------------------------------------------------------------------------

class ShellTool(BaseTool):
    """
    执行 shell 命令，返回 stdout + stderr。

    params:
        cmd (str):     shell 命令字符串
        timeout (int): 超时秒数（默认 30）
        cwd (str):     工作目录（默认使用当前目录）

    安全模型：
        - L0 黑名单硬拦截：execute() 内 _check_blocked()，defense-in-depth
        - 权限管道 Layer 1 也调用 _check_blocked()，双重保障
        - 其他权限决策由 PermissionPipeline 统一处理
    """

    def __init__(
        self,
        confirm_callback: ConfirmCallback | None = None,
        runtime: Runtime | None = None,
    ) -> None:
        self._confirm_callback = confirm_callback
        self._runtime = runtime or LocalRuntime()
        self.read_only = False  # set True by PhasePolicy for analysis tasks

    @property
    def name(self) -> str:
        return "shell"

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
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional)",
                },
            },
            "required": [],  # either command or cmd must be provided
        }

    @property
    def risk_level(self) -> str:
        from tools.base import RiskLevel
        return RiskLevel.HIGH

    def classify_risk(self, params: dict[str, Any]) -> str:
        """动态风险分类：根据命令内容决定实际风险等级。"""
        from tools.base import RiskLevel
        cmd = params.get("cmd", "") or self._build_cmd_repr(params)
        if not cmd.strip():
            return RiskLevel.NONE
        if _is_readonly(cmd):
            return RiskLevel.NONE
        if _needs_confirm(cmd):
            return RiskLevel.HIGH
        return RiskLevel.LOW

    def _build_cmd_repr(self, params: dict[str, Any]) -> str:
        """Build a string representation for safety checks (L0/L1/L2)."""
        command = params.get("command", "")
        args = params.get("args", [])
        if command:
            return f"{command} {' '.join(args)}" if args else command
        return params.get("cmd", "")

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

        # Read-only enforcement: block write operations at the COMMAND level
        if self.read_only:
            ro_blocked = _check_read_only_blocked(cmd_repr)
            if ro_blocked:
                return ToolResult(
                    success=False, output="",
                    error=f"[READ-ONLY SHELL] Command blocked: '{ro_blocked}' is not allowed in analysis mode. Use read-only commands only (dir, type, findstr, Get-ChildItem, Select-String).",
                )

        # L0 blacklist check (operates on command semantics, same as legacy)
        blocked = _check_blocked(cmd_repr)
        if blocked:
            return ToolResult(
                success=False, output="",
                error=f"Command blocked for safety: matched '{blocked}'",
            )

        # L2 confirm check
        if self._confirm_callback is not None and _needs_confirm(cmd_repr):
            allowed = self._confirm_callback(cmd_repr)
            if not allowed:
                return ToolResult(
                    success=False, output="",
                    error=f"Command rejected by user: {cmd_repr!r}",
                )

        from tools.runtime import RunResult
        run_result: RunResult = self._runtime.execute(
            command, args=args, cwd=cwd, timeout=timeout,
        )

        return self._build_result(run_result, cmd_repr)

    def _execute_legacy(self, cmd: str, timeout: int, cwd: str | None) -> ToolResult:
        """Execute via Runtime.exec() — shell=True, backward compatible."""
        # Read-only enforcement: block write operations at the COMMAND level
        if self.read_only:
            ro_blocked = _check_read_only_blocked(cmd)
            if ro_blocked:
                return ToolResult(
                    success=False, output="",
                    error=f"[READ-ONLY SHELL] Command blocked: '{ro_blocked}' is not allowed in analysis mode. Use read-only commands only.",
                )

        # L0 blacklist hard intercept
        blocked = _check_blocked(cmd)
        if blocked:
            return ToolResult(
                success=False, output="",
                error=f"Command blocked for safety: matched '{blocked}'",
            )

        # L2 confirm check
        if self._confirm_callback is not None and _needs_confirm(cmd):
            allowed = self._confirm_callback(cmd)
            if not allowed:
                return ToolResult(
                    success=False, output="",
                    error=f"Command rejected by user: {cmd!r}",
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
        from tools.base import classify_runtime_error
        output = truncate_output(run_result.output, MAX_OUTPUT_CHARS)
        if not run_result.success:
            _tool_err = classify_runtime_error(
                run_result.returncode, run_result.stderr, run_result.stdout, cmd_repr,
            )
            return ToolResult(
                success=False, output=output,
                error=_tool_err.to_message() if _tool_err else f"Exit code: {run_result.returncode}",
                tool_error=_tool_err,
            )
        return ToolResult(success=True, output=output)


# ---------------------------------------------------------------------------
# 辅助函数（对外暴露供测试）
# ---------------------------------------------------------------------------

def _check_read_only_blocked(cmd: str) -> str | None:
    """Check if cmd contains write operations blocked in read-only mode."""
    cmd_lower = cmd.lower()
    for pattern in _READ_ONLY_BLOCKED:
        if pattern.lower() in cmd_lower:
            return pattern
    return None


def _check_blocked(cmd: str) -> str | None:
    """返回匹配到的黑名单 pattern，没有匹配返回 None。"""
    cmd_lower = cmd.lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern.lower() in cmd_lower:
            return pattern
    return None


def _is_readonly(cmd: str) -> bool:
    """
    判断命令是否在只读白名单里。
    包含 > 写重定向的命令不算只读（即使命令名在白名单里）。
    """
    # 包含写重定向（> 但不是 >>）时不算只读
    if re.search(r'(?<![>])>(?![>])', cmd):
        return False
    stripped = cmd.strip().lower()
    for prefix in _READONLY_PREFIXES:
        if stripped == prefix or stripped.startswith(prefix + " "):
            return True
    return False


def _needs_confirm(cmd: str) -> bool:
    """
    判断命令是否需要用户确认。
    不在白名单 且 包含危险关键词 → 需要确认。
    """
    if _is_readonly(cmd):
        return False
    cmd_lower = cmd.lower()
    return any(kw in cmd_lower for kw in _CONFIRM_KEYWORDS)


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