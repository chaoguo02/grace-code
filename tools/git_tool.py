"""
tools/git_tool.py

Git 操作工具，四个 action：
- git_status:  查看工作区状态（等同 git status --short）
- git_diff:    查看变更内容（等同 git diff 或 git diff HEAD）
- git_add:     暂存文件（等同 git add）
- git_commit:  提交（等同 git commit -m）

设计决策：
- 不封装 git push / PR 创建，这些由 entry/github_issue.py 负责
- git_diff 做输出截断，大型重构的 diff 可能很长
- 所有操作都通过 subprocess 调 git CLI，不用 gitpython
  （减少依赖，git CLI 输出 agent 更容易理解）
"""

from __future__ import annotations

import subprocess
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.runtime import LocalRuntime, Runtime


MAX_DIFF_CHARS = 8_000


def _run_git(
    args: list[str],
    cwd: str | None = None,
    runtime: "Runtime | None" = None,
) -> tuple[bool, str]:
    """
    运行 git 命令，返回 (success, output)。
    runtime 为 None 时直接用 subprocess（向后兼容）。
    """
    from tools.runtime import LocalRuntime
    rt = runtime or LocalRuntime()
    cmd = "git " + " ".join(
        f'"{a}"' if " " in a else a for a in args
    )
    result = rt.exec(cmd, cwd=cwd, timeout=30)
    output = result.output.strip()
    return result.success, output


class GitStatusTool(BaseTool):
    is_read_only = True
    """
    (see class docstring below)
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    """
    查看工作区状态。

    params:
        cwd (str): repo 根目录（默认当前目录）
    """

    @property
    def name(self) -> str:
        return "git_status"

    @property
    def description(self) -> str:
        return (
            "Show the working tree status (modified, untracked, staged files). "
            "Run this before committing to see what has changed."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd")
        success, output = _run_git(["status", "--short", "--branch"], cwd=cwd, runtime=self._runtime)
        if not output:
            output = "Nothing to commit, working tree clean"
        return ToolResult(success=success, output=output, error=None if success else output)


class GitDiffTool(BaseTool):
    is_read_only = True
    """
    (see class docstring below)
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    """
    查看变更 diff。

    params:
        staged (bool): True 则查看已暂存的 diff（git diff --cached），默认 False
        path (str):    只查看特定文件的 diff
        cwd (str):     repo 根目录
    """

    @property
    def name(self) -> str:
        return "git_diff"

    @property
    def description(self) -> str:
        return (
            "Show changes in the working tree or staging area. "
            "Use staged=true to see what will be committed. "
            "Use path to diff a specific file."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes (git diff --cached). Default false.",
                },
                "path": {
                    "type": "string",
                    "description": "Specific file to diff (optional)",
                },
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd")
        staged = params.get("staged", False)
        path = params.get("path")

        args = ["diff"]
        if staged:
            args.append("--cached")
        if path:
            args += ["--", path]

        success, output = _run_git(args, cwd=cwd, runtime=self._runtime)

        if not output:
            label = "staged" if staged else "unstaged"
            return ToolResult(success=True, output=f"No {label} changes.")

        # 截断超长 diff
        if len(output) > MAX_DIFF_CHARS:
            kept = MAX_DIFF_CHARS
            omitted = len(output) - kept
            output = output[:kept] + f"\n... [{omitted} chars truncated]"

        return ToolResult(success=success, output=output, error=None if success else output)


class GitAddTool(BaseTool):
    """
    (see class docstring below)
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    """
    暂存文件。

    params:
        paths (list[str]): 要暂存的文件路径列表，默认 ["."]（暂存所有）
        cwd (str):         repo 根目录
    """

    @property
    def name(self) -> str:
        return "git_add"

    @property
    def risk_level(self) -> str:
        from tools.base import RiskLevel
        return RiskLevel.LOW

    @property
    def description(self) -> str:
        return (
            "Stage files for commit. "
            "Pass a list of paths, or omit to stage all changes (git add .)."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to stage. Default: ['.'] (all changes)",
                },
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd")
        paths: list[str] = params.get("paths", ["."])
        if not paths:
            paths = ["."]

        success, output = _run_git(["add"] + paths, cwd=cwd, runtime=self._runtime)
        if success:
            return ToolResult(success=True, output=f"Staged: {', '.join(paths)}")
        return ToolResult(success=False, output=output, error=output)


class GitCommitTool(BaseTool):
    """
    (see class docstring below)
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    """
    提交暂存的变更。

    params:
        message (str): commit message（必填）
        cwd (str):     repo 根目录
    """

    @property
    def name(self) -> str:
        return "git_commit"

    @property
    def risk_level(self) -> str:
        from tools.base import RiskLevel
        return RiskLevel.HIGH

    @property
    def description(self) -> str:
        return (
            "Commit staged changes with a message. "
            "Always run git_add before git_commit. "
            "Write a clear, descriptive commit message."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Commit message (be descriptive)",
                },
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": ["message"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd")
        message = params.get("message", "").strip()

        if not message:
            return ToolResult(
                success=False, output="", error="commit message is required"
            )

        success, output = _run_git(["commit", "-m", message], cwd=cwd, runtime=self._runtime)
        return ToolResult(
            success=success,
            output=output,
            error=None if success else output,
        )