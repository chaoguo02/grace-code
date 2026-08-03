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
from core.process import LocalRuntime, Runtime
from core.utils import truncate_output


MAX_OUTPUT_CHARS = 50_000

# ── Advisory safety floor ─────────────────────────────────────────────────
# IMPORTANT: These patterns are a HINT barrier, NOT a security boundary.
# They catch the most common destructive commands but can be bypassed by:
# - Using find/xargs/chmod variants not in the list
# - Calling interpreters (python -c, node -e, ruby -e)
# - Obfuscating command strings
#
# TRUE security isolation requires FORGE_SANDBOX=docker
# (DockerContainerRuntime), which provides overlay filesystem +
# no-new-privileges + CMD-INJ filtering.
#
# See audit finding P1-32 for the threat model and bypass documentation.
_BLOCKED_PATTERNS: tuple[str, ...] = (
    # Filesystem destruction
    "rm -rf /",          # recursive root removal
    "rm -rf ~",          # recursive home removal
    "rm -rf /*",         # root glob removal
    "rm -rf ~/*",        # home glob removal
    "rm -r /",           # non-force root removal
    "rm -r ~",           # non-force home removal
    "find / -delete",    # find-delete root bypass
    "find / -exec rm",   # find-exec root bypass
    "> /dev/sda",        # SATA disk overwrite
    "> /dev/hda",        # IDE disk overwrite
    "> /dev/nvme",       # NVMe disk overwrite
    # Privilege / system integrity
    "mkfs",              # filesystem format
    "dd if=",            # raw device access
    "chmod -R 000 /",    # revoke all permissions
    "chmod -R 777 /",    # world-writable root
    "chown -R",          # recursive ownership change
    # Fork bomb
    ":(){:|:&};:",       # classic fork bomb
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
                "For listing files, prefer Glob. "
                "For git operations, use git_status/git_diff/git_add/git_commit tools instead."
            )
        return (
            "Execute a shell command and return its output. "
            "Timeout is 30s by default. "
            "For reading files, prefer the Read tool. "
            "For searching file contents, prefer Grep. "
            "For listing files, prefer Glob. "
            "For git operations (status, diff, add, commit), use the git_status, "
            "git_diff, git_add, git_commit tools instead."
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
    def prompt_contract(self) -> tuple[str, ...]:
        return (
            "ALWAYS use `command` + `args` (not the deprecated `cmd` field). "
            "Each argument must be a separate list element, for example "
            "`{\"command\": \"pytest\", \"args\": [\"--tb=short\"]}`. "
            "Never embed flags or paths inside the `command` string.",
        )

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

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        cmd = (params or {}).get("command", "").strip()
        if not cmd:
            return False
        first_word = cmd.split()[0] if cmd else ""
        if first_word in _READ_ONLY_COMMANDS:
            return True
        # Check read-only prefixes (git status, git log, etc.)
        full_cmd_lower = cmd.lower().strip()
        for prefix in _READ_ONLY_PREFIXES:
            if full_cmd_lower.startswith(prefix):
                return True
        return False

    @property
    def supports_cancellation(self) -> bool:
        """Bash is the only built-in tool that supports cooperative cancellation.

        When ``CancellationToken.is_cancelled`` becomes True during a
        subprocess execution, ShellTool sends SIGTERM, then SIGKILL after
        a grace period.  This is the only "semi-forcible" cancellation
        path in the tool system.
        """
        return True

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
        use_shell: bool = params.get("use_shell", False)

        if not command and not cmd:
            return ToolResult(success=False, output="", error="Either 'command' or 'cmd' is required")

        # P0_3: workspace root must exist (fail closed).
        ws_root = getattr(self._runtime, "_workspace_root", None)
        if ws_root is None:
            return ToolResult(
                success=False, output="",
                error="Workspace root is not set. Shell execution requires a workspace boundary.",
            )

        if command:
            return self._execute_parameterized(command, args, timeout, cwd, use_shell=use_shell)
        return self._execute_legacy(cmd, timeout, cwd)

    # ── Parameterized execution (preferred) ──────────────────────────────

    def _execute_parameterized(
        self, command: str, args: list[str], timeout: int, cwd: str | None, *, use_shell: bool = False,
    ) -> ToolResult:
        import logging, platform, shutil
        _log = logging.getLogger(__name__)
        cmd_repr = f"{command} {' '.join(args)}" if args else command

        blocked = _check_blocked(cmd_repr)
        if blocked:
            return ToolResult(success=False, output="", error=f"Command blocked for safety: matched '{blocked}'")

        # P0_3: workspace root already validated in execute(); keep path sandbox.
        _ws_root = getattr(self._runtime, "_workspace_root", None)
        path_violation = _validate_workspace_paths(command, args, _ws_root)
        if path_violation:
            return ToolResult(success=False, output="", error=path_violation)

        cmd_name = command.split()[0] if command.split() else command

        # ── Step 1: Direct argv execution (safe, no shell interpretation) ──
        exe_path = shutil.which(cmd_name) if platform.system() != "Windows" else (
            shutil.which(cmd_name)
            or shutil.which(f"{cmd_name}.exe")
            or shutil.which(f"{cmd_name}.cmd")
            or shutil.which(f"{cmd_name}.bat")
        )
        if exe_path:
            try:
                run_result = self._runtime.execute(exe_path, args=args, cwd=cwd, timeout=timeout)
                return self._build_result(run_result, cmd_repr)
            except Exception as exc:
                _log.debug("direct execute failed for %s: %s", exe_path, exc)
                if not use_shell:
                    return ToolResult(
                        success=False, output="",
                        error=f"Command '{cmd_name}' failed to execute directly: {exc}. Use use_shell=true for shell fallback.",
                    )

        # ── Step 2/3: Shell fallback — requires explicit opt-in ──
        # P0_3: shell mode requires explicit use_shell=true.
        # This prevents injection via args containing ; | $() etc
        # when the command falls through to PowerShell -Command or cmd.exe.
        if not use_shell:
            return ToolResult(
                success=False, output="",
                error=(
                    f"Command '{cmd_name}' not found as a direct executable "
                    f"and use_shell is not set. Set use_shell=true to allow "
                    f"execution via shell."
                ),
            )

        full_cmd = f"{command} {' '.join(args)}" if args else command
        _log.warning("Shell fallback with use_shell=true: %r", full_cmd)

        if platform.system() == "Windows":
            ps_exe = shutil.which("powershell.exe") or shutil.which("powershell")
            if ps_exe is None:
                for _p in [r"C:\Windows\SysNative\WindowsPowerShell\v1.0\powershell.exe",
                           r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"]:
                    if os.path.exists(_p):
                        ps_exe = _p
                        break
            if ps_exe:
                ps_args = ["-NoProfile", "-NonInteractive", "-Command", full_cmd]
                try:
                    run_result = self._runtime.execute(ps_exe, args=ps_args, cwd=cwd, timeout=timeout)
                    return self._build_result(run_result, cmd_repr)
                except Exception as exc:
                    _log.debug("powershell fallback failed: %s", exc)

            try:
                run_result = self._runtime.exec(full_cmd, cwd=cwd, timeout=timeout)
                return self._build_result(run_result, cmd_repr)
            except Exception as exc:
                _log.debug("exec(shell=True) failed: %s", exc)

            return ToolResult(
                success=False, output="",
                error=(
                    f"Command '{cmd_name}' could not run on Windows. "
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
            # shutil.which bypasses WOW64 redirector; os.path.exists doesn't
            ps_exe = shutil.which("powershell.exe") or shutil.which("powershell")
            if ps_exe:
                try:
                    run_result = self._runtime.execute(
                        ps_exe, args=["-NoProfile", "-NonInteractive", "-Command", cmd],
                        cwd=cwd, timeout=timeout,
                    )
                    return self._build_result(run_result, cmd)
                except Exception:
                    pass

        run_result = self._runtime.exec(cmd, cwd=cwd, timeout=timeout)
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

        # Phase 2 #6: Check if cancellation token fired during execution
        cancelled = False
        token = getattr(self, "_cancellation_token", None)
        if token is not None:
            cancelled = token.is_cancelled

        return ToolResult(
            success=run_result.success and not cancelled,
            output=output,
            error=(
                getattr(run_result, "error", None)
                or ("Cancelled by user" if cancelled else None)
            ),
            metadata={
                "exit_code": str(getattr(run_result, "returncode", 0) or 0),
                **({"cancelled": True} if cancelled else {}),
            },
        )


def terminal_confirm(cmd: str) -> bool:
    """Display a command and prompt the user for confirmation."""
    import click
    click.echo(f"\nShell command: {cmd}")
    return click.confirm("Execute?", default=True)


def _check_blocked(cmd: str) -> str:
    """Check command against advisory blocked patterns.

    Returns the matched pattern string, or "" if no match.
    This is NOT a security boundary — see _BLOCKED_PATTERNS docstring.
    """
    for pattern in _BLOCKED_PATTERNS:
        if pattern in cmd:
            return pattern
    return ""


def _validate_workspace_paths(
    command: str,
    args: list[str],
    workspace_root: str | None,
) -> str | None:
    """Validate that file paths in command args stay within the workspace.

    Returns an error message if a path escapes the workspace, or None if all
    paths are safe.  Only checks paths that look like filesystem references
    (absolute paths, paths with ../ segments).

    This is a SOFT guard — it does not protect against interpreter-level
    escapes (e.g. python -c "open('/etc/shadow')").  True isolation requires
    Docker/Podman sandbox mode (FORGE_SANDBOX=docker).
    """
    if workspace_root is None:
        return None  # no workspace constraint — sandbox mode presumably active

    from pathlib import Path

    ws = Path(workspace_root).resolve()

    def _check_path(raw: str) -> str | None:
        """Check a single path candidate.  Returns error or None."""
        stripped = raw.strip()
        if not stripped:
            return None

        # Absolute paths (Unix /... or Windows C:\...)
        if stripped.startswith("/") or (len(stripped) >= 2 and stripped[1] == ":"):
            try:
                resolved = Path(stripped).resolve()
                resolved.relative_to(ws)
            except (ValueError, OSError):
                return (
                    f"Path '{stripped}' resolves outside the workspace. "
                    f"Use a workspace-relative path or switch to Read/Write/Edit tools."
                )
            return None

        # Paths escaping upward with ../
        normalized = stripped.replace("\\", "/")
        if normalized.startswith("..") or "/.." in normalized:
            segments = normalized.split("/")
            depth = 0
            max_depth = 0
            for seg in segments:
                if seg == "..":
                    depth += 1
                    max_depth = max(max_depth, depth)
                elif seg and seg != ".":
                    depth = max(0, depth - 1)
            if max_depth >= 3:  # "../../../" pattern
                return (
                    f"Path '{stripped}' attempts to escape the workspace "
                    f"({max_depth} levels up). Use a workspace-relative path."
                )

        return None

    for arg in args:
        err = _check_path(arg)
        if err:
            return err

    return None
