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
        - L0 黑名单硬拦截：execute() 内 _check_blocked()，永远不执行
        - L1+ 确认：通过 classify_risk() 上报风险等级，由 HitlManager 统一处理
    """

    def __init__(
        self,
        confirm_callback: ConfirmCallback | None = None,
        runtime: Runtime | None = None,
    ) -> None:
        # confirm_callback 保留用于向后兼容（无 HitlManager 时的降级路径）
        self._confirm_callback = confirm_callback
        self._runtime = runtime or LocalRuntime()

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output (stdout + stderr combined). "
            "Timeout is 30s by default. Avoid long-running commands; "
            "prefer targeted commands like 'grep', 'pytest tests/foo.py', 'git diff'."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "description": "Shell command to execute",
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
            "required": ["cmd"],
        }

    @property
    def risk_level(self) -> str:
        from tools.base import RiskLevel
        return RiskLevel.HIGH

    def classify_risk(self, params: dict[str, Any]) -> str:
        """动态风险分类：根据命令内容决定实际风险等级。"""
        from tools.base import RiskLevel
        cmd = params.get("cmd", "").strip()
        if not cmd:
            return RiskLevel.NONE
        if _is_readonly(cmd):
            return RiskLevel.NONE
        if _needs_confirm(cmd):
            return RiskLevel.HIGH
        return RiskLevel.LOW

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cmd: str = params.get("cmd", "").strip()
        timeout: int = int(params.get("timeout", 30))
        cwd: str | None = params.get("cwd", None)

        if not cmd:
            return ToolResult(success=False, output="", error="cmd is required")

        # L0 黑名单硬拦截（永远在 execute 内，不可被 policy 覆盖）
        blocked = _check_blocked(cmd)
        if blocked:
            return ToolResult(
                success=False,
                output="",
                error=f"Command blocked for safety: matched '{blocked}'",
            )

        # 如果有 HitlManager（通过 ToolRegistry 接入），确认已在 execute 前完成。
        # 降级路径：无 HitlManager 时使用内部 confirm_callback（兼容旧代码/测试）
        if self._confirm_callback is not None and _needs_confirm(cmd):
            allowed = self._confirm_callback(cmd)
            if not allowed:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Command rejected by user: {cmd!r}",
                )

        return self._run(cmd, timeout, cwd)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _run(self, cmd: str, timeout: int, cwd: str | None) -> ToolResult:
        """通过 runtime 执行命令（本地或 Docker 沙箱）。"""
        result = self._runtime.exec(cmd, cwd=cwd, timeout=timeout)
        output = truncate_output(result.output, MAX_OUTPUT_CHARS)
        if not result.success:
            # 区分 timeout 和普通错误，error 字段包含可读原因
            if "timed out" in result.stderr.lower():
                error = result.stderr.strip()
            else:
                error = f"Exit code: {result.returncode}"
        else:
            error = None
        return ToolResult(success=result.success, output=output, error=error)


# ---------------------------------------------------------------------------
# 辅助函数（对外暴露供测试）
# ---------------------------------------------------------------------------

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