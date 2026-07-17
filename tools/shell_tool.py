"""
tools/shell_tool.py

Shell command execution tool. Platform-aware:
- Windows: uses powershell.exe or cmd.exe as appropriate
- Unix: uses /bin/sh

CC-aligned: the tool is named "Bash" for CC compatibility but adapts
to the platform. On Windows, commands execute via PowerShell.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Callable

from core.base import BaseTool, ToolEffect, ToolMetadata, ToolResult
from executor.process import LocalRuntime, Runtime
from core.utils import truncate_output


MAX_OUTPUT_CHARS = 50_000

_BLOCKED_PATTERNS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "chmod -R 777 /",
    "chown -R",
    "> /dev/sda",
)

ConfirmCallback = Callable[[str], bool]

_READ_ONLY_COMMANDS: frozenset[str] = frozenset({
    "ls", "dir", "cat", "head", "tail", "wc", "du", "df",
    "grep", "find", "locate", "which", "where", "whereis",
    "echo", "printf", "date", "uptime", "hostname", "uname",
    "pwd", "env", "printenv", "whoami", "id", "groups",
    "tree", "file", "stat", "readlink", "realpath",
    "sort", "uniq", "cut", "tr", "awk", "sed",
    "diff", "cmp", "comm", "join", "paste",
    "pgrep", "pidof", "ps", "top", "free", "vmstat",
    "lscpu", "lsblk", "lsusb", "lspci", "dmesg",
    "type", "help", "man", "info", "whatis",
    "Get-ChildItem", "Get-Content", "Get-Item", "Get-Command",
    "Get-Process", "Get-Service", "Select-String",
})

_READ_ONLY_PREFIXES: tuple[str, ...] = (
    "git status", "git log", "git diff", "git show",
    "git branch", "git tag", "git remote",
    "git config --get", "git config --list",
    "git ls-", "git rev-",
)


class ShellTool(BaseTool):
    metadata = ToolMetadata(effects=frozenset({ToolEffect.EXECUTE}))
    """
    Execute shell commands. Platform-aware execution:
    - Windows: PowerShell (Get-ChildItem) or cmd.exe (dir)
    - macOS/Linux: bash/sh
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
        import platform
        if platform.system() == "Windows":
            return (
                "Execute a shell command on Windows via PowerShell. "
                "Use standard PowerShell cmdlets (Get-ChildItem, Get-Content, Select-String). "
                "Timeout is 30s by default. "
                "For reading files, prefer the Read tool. "
                "For searching file contents, prefer Grep. "
                "For listing files, prefer Glob."
            )
        return (
            "Execute a shell command and return its output. "
            "Timeout is 30s by default. "
            "For reading files, prefer the Read tool. "
            "For searching file contents, prefer Grep. "
            "For listing files, prefer Glob."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to execute (e.g., 'Get-ChildItem' or 'ls')",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments passed as separate list items",
                },
                "cmd": {
                    "type": "string",
                    "description": "DEPRECATED. Full command string (legacy). Use command+args instead.",
                },
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                "cwd": {"type": "string", "description": "Working directory"},
            },
        }

    @property
    def risk_level(self) -> str:
        from core.base import RiskLevel
        return RiskLevel.HIGH

    def concurrency_mode(self, params: dict[str, Any]) -> Any:
        from core.base import ToolConcurrency
        command = (params.get("command") or "").strip()
        args = params.get("args", [])
        if not command:
            return ToolConcurrency.SERIAL
        full_cmd = f"{command} {' '.join(args)}" if args else command
        full_cmd_lower = full_cmd.lower().strip()
        base = command.lower().strip().split()[0] if command.split() else command
        if base in _READ_ONLY_COMMANDS:
            return ToolConcurrency.PARALLEL_SAFE
        if "/" in base:
            leaf = base.rsplit("/", 1)[-1]
            if leaf in _READ_ONLY_COMMANDS:
                return ToolConcurrency.PARALLEL_SAFE
        for prefix in _READ_ONLY_PREFIXES:
            if full_cmd_lower.startswith(prefix):
                return ToolConcurrency.PARALLEL_SAFE
        return ToolConcurrency.SERIAL

    def permission_denial_reason(self, params: dict[str, Any]) -> str | None:
        cmd = self._build_cmd_repr(params)
        if _check_blocked(cmd):
            return f"Blocked by safety floor: matched pattern"
        if "\x00" in cmd or len(cmd) > 10_000:
            return "Blocked: malicious input detected"
        return None

    def _build_cmd_repr(self, params: dict[str, Any]) -> str:
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

        if not command and not cmd:
            return ToolResult(success=False, output="", error="Either 'command' or 'cmd' is required")

        if command:
            return self._execute_parameterized(command, args, timeout, cwd)
        return self._execute_legacy(cmd, timeout, cwd)

    # ── Parameterized execution (preferred) ──────────────────────────────

    def _execute_parameterized(self, command: str, args: list[str], timeout: int, cwd: str | None) -> ToolResult:
        import logging, platform, shutil
        _log = logging.getLogger(__name__)
        cmd_repr = f"{command} {' '.join(args)}" if args else command

        blocked = _check_blocked(cmd_repr)
        if blocked:
            return ToolResult(success=False, output="", error=f"Command blocked for safety: matched '{blocked}'")

        cmd_name = command.split()[0] if command.split() else command

        # ── Windows: use PowerShell or cmd.exe ──
        if platform.system() == "Windows":
            _log.debug("shell cmd=%s args=%s cwd=%s PATH=%s", cmd_name, args, cwd,
                       os.environ.get("PATH", "")[:200])

            # Try direct execution first (for native exes like git, python)
            exe_path = shutil.which(cmd_name)
            if exe_path:
                try:
                    run_result = self._runtime.execute(exe_path, args=args, cwd=cwd, timeout=timeout)
                    return self._build_result(run_result, cmd_repr)
                except Exception as exc:
                    _log.debug("direct execute failed: %s", exc)

            # Try PowerShell (for PowerShell cmdlets like Get-ChildItem)
            ps_exe = shutil.which("powershell.exe") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            if os.path.exists(ps_exe):
                ps_cmd = f"& {{ {command} {' '.join(args)} }}" if args else f"& {{ {command} }}"
                try:
                    run_result = self._runtime.execute(
                        ps_exe, args=["-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                        cwd=cwd, timeout=timeout,
                    )
                    return self._build_result(run_result, cmd_repr)
                except Exception as exc:
                    _log.debug("powershell execute failed: %s", exc)

            # Try cmd.exe (for legacy DOS commands like dir, tree)
            comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            if os.path.exists(comspec):
                full_cmd = f"{command} {' '.join(args)}" if args else command
                try:
                    run_result = self._runtime.execute(
                        comspec, args=["/d", "/s", "/c", full_cmd],
                        cwd=cwd, timeout=timeout,
                    )
                    return self._build_result(run_result, cmd_repr)
                except Exception as exc:
                    _log.debug("cmd.exe execute failed: %s", exc)

            return ToolResult(
                success=False, output="",
                error=(
                    f"Command '{cmd_name}' could not run on Windows. "
                    f"PowerShell and cmd.exe both failed. "
                    f"Use Glob/Grep/Read tools instead of shell."
                ),
            )

        # ── Unix: direct execution ──
        try:
            run_result = self._runtime.execute(command, args=args, cwd=cwd, timeout=timeout)
        except FileNotFoundError:
            return ToolResult(
                success=False, output="",
                error=f"Command '{command}' not found. Make sure it is installed and in your PATH.",
            )
        return self._build_result(run_result, cmd_repr)

    # ── Legacy execution ─────────────────────────────────────────────────

    def _execute_legacy(self, cmd: str, timeout: int, cwd: str | None) -> ToolResult:
        blocked = _check_blocked(cmd)
        if blocked:
            return ToolResult(success=False, output="", error=f"Command blocked for safety: matched '{blocked}'")
        return self._run(cmd, timeout, cwd)

    def _run(self, cmd: str, timeout: int, cwd: str | None) -> ToolResult:
        import logging, platform

        if platform.system() == "Windows":
            _log = logging.getLogger(__name__)
            ps_exe = shutil.which("powershell.exe") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            if os.path.exists(ps_exe):
                try:
                    run_result = self._runtime.execute(
                        ps_exe, args=["-NoProfile", "-NonInteractive", "-Command", cmd],
                        cwd=cwd, timeout=timeout,
                    )
                    return self._build_result(run_result, cmd)
                except Exception:
                    pass

        run_result = self._runtime.run(cmd, shell=True, cwd=cwd, timeout=timeout)
        return self._build_result(run_result, cmd)

    def _build_result(self, run_result, cmd_repr: str) -> ToolResult:
        stdout = run_result.stdout or ""
        stderr = run_result.stderr or ""

        # Combine stdout + stderr (CC convention)
        output = stdout
        if stderr and stderr != stdout:
            output += "\n" + stderr

        # Truncate
        if len(output) > MAX_OUTPUT_CHARS:
            output = truncate_output(output, MAX_OUTPUT_CHARS)

        return ToolResult(
            success=run_result.success,
            output=output,
            error=run_result.error or None,
        )


def _check_blocked(cmd: str) -> str:
    for pattern in _BLOCKED_PATTERNS:
        if pattern in cmd:
            return pattern
    return ""
