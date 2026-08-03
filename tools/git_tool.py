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

import os
import subprocess
from typing import Any

from core.base import BaseTool, PathAccess, ToolEffect, ToolMetadata, ToolResult
from core.process import LocalRuntime, Runtime


MAX_DIFF_CHARS = 8_000


def _run_git(
    args: list[str],
    cwd: str | None = None,
    runtime: "Runtime | None" = None,
) -> tuple[bool, str, Any | None]:
    """运行 git 命令，返回 (success, output, tool_error)。

    CC-aligned: worktree subagents pin cwd to workspace_root.
    If the caller provides a cwd outside the runtime's workspace,
    fall back to workspace root to prevent cross-repo contamination.
    """
    import logging, platform, shutil
    _log = logging.getLogger(__name__)

    # On Windows, ensure git is available in subprocess PATH
    if platform.system() == "Windows":
        _git_path = shutil.which("git")
        if _git_path is None:
            # Common Git Bash paths
            for _p in [r"C:\Program Files\Git\cmd\git.exe",
                       r"C:\Program Files (x86)\Git\cmd\git.exe"]:
                if os.path.exists(_p):
                    _git_path = _p
                    break
        if _git_path is None:
            return (False,
                    "Git not found. Install Git for Windows (https://git-scm.com) "
                    "or ensure it's in the system PATH.", None)
        _git_cmd = _git_path
    else:
        _git_cmd = "git"

    from core.process import LocalRuntime
    rt = runtime or LocalRuntime()
    _final_cwd = cwd
    if cwd is not None and hasattr(rt, '_resolve_cwd'):
        try:
            rt._resolve_cwd(cwd)
        except ValueError:
            _final_cwd = None  # use workspace root
    result = rt.execute(_git_cmd, args=args, cwd=_final_cwd, timeout=30)
    output = result.output.strip()
    if not result.success:
        from core.base import classify_runtime_error
        cmd_repr = f"git {' '.join(args)}"
        _err = classify_runtime_error(result, cmd_repr)
        return False, output, _err
    return True, output, None


class GitStatusTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_VCS}),
        path_access=PathAccess.WORKSPACE_WIDE,
    )
    """
    (see class docstring below)
    """

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(self, runtime: Runtime | None = None) -> None:
        from core.process import LocalRuntime
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
        success, output, _tool_err = _run_git(["status", "--short", "--branch"], cwd=cwd, runtime=self._runtime)
        if not output:
            output = "Nothing to commit, working tree clean"
        return ToolResult(success=success, output=output,
                          error=None if success else output, tool_error=_tool_err)


class GitDiffTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_VCS}),
        path_access=PathAccess.DIFF,
        path_parameter="path",
    )
    """
    (see class docstring below)
    """

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(self, runtime: Runtime | None = None) -> None:
        from core.process import LocalRuntime
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

        success, output, _tool_err = _run_git(args, cwd=cwd, runtime=self._runtime)

        if not output:
            label = "staged" if staged else "unstaged"
            return ToolResult(success=True, output=f"No {label} changes.")

        # 截断超长 diff
        if len(output) > MAX_DIFF_CHARS:
            kept = MAX_DIFF_CHARS
            omitted = len(output) - kept
            output = output[:kept] + f"\n... [{omitted} chars truncated]"

        return ToolResult(success=success, output=output,
                          error=None if success else output, tool_error=_tool_err)


class GitAddTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.WRITE_VCS}),
        path_access=PathAccess.WORKSPACE_WIDE,
    )
    """
    (see class docstring below)
    """

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return False

    def __init__(self, runtime: Runtime | None = None) -> None:
        from core.process import LocalRuntime
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
        from core.base import RiskLevel
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

        success, output, _tool_err = _run_git(["add"] + paths, cwd=cwd, runtime=self._runtime)
        if success:
            return ToolResult(success=True, output=f"Staged: {', '.join(paths)}")
        return ToolResult(success=False, output=output, error=output, tool_error=_tool_err)


class GitCommitTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.WRITE_VCS}),
        path_access=PathAccess.WORKSPACE_WIDE,
    )
    """
    (see class docstring below)
    """

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return False

    def __init__(self, runtime: Runtime | None = None) -> None:
        from core.process import LocalRuntime
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
        from core.base import RiskLevel
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

        success, output, _tool_err = _run_git(["commit", "-m", message], cwd=cwd, runtime=self._runtime)
        return ToolResult(
            success=success,
            output=output,
            error=None if success else output,
        )


class GitSnapshotTool(BaseTool):
    """创建工作区快照：git add -A + git commit，返回 commit hash。

    对齐架构 S1 "关键修改前快照"：在执行批量重构等高风险操作前调用，
    之后可用 git_revert 恢复快照，而不是让 LLM 重新生成反向 Diff。
    """

    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.WRITE_VCS}),
        path_access=PathAccess.WORKSPACE_WIDE,
    )

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return False

    def __init__(self, runtime: Runtime | None = None) -> None:
        from core.process import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    @property
    def name(self) -> str:
        return "git_snapshot"

    @property
    def risk_level(self) -> str:
        from core.base import RiskLevel
        return RiskLevel.HIGH

    @property
    def description(self) -> str:
        return (
            "Create a workspace snapshot (git add -A + git commit). "
            "Run this BEFORE risky refactors so changes can be rolled back "
            "via git_revert. Returns the snapshot commit hash."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Snapshot message (default: 'agent checkpoint snapshot')",
                },
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd")
        message = params.get("message", "agent checkpoint snapshot").strip()

        ok, output, _tool_err = _run_git(["add", "-A"], cwd=cwd, runtime=self._runtime)
        if not ok:
            return ToolResult(success=False, output=output, error=output, tool_error=_tool_err)

        ok, output, _tool_err = _run_git(
            ["commit", "-m", message], cwd=cwd, runtime=self._runtime,
        )
        if not ok:
            # Nothing to commit → HEAD is still a valid snapshot point
            if "nothing to commit" in output or "no changes added" in output:
                ok2, hout, _ = _run_git(
                    ["rev-parse", "HEAD"], cwd=cwd, runtime=self._runtime,
                )
                return ToolResult(
                    success=True,
                    output=f"No changes to snapshot. Current HEAD={hout.strip()}",
                )
            return ToolResult(success=False, output=output, error=output, tool_error=_tool_err)

        ok, hout, _ = _run_git(["rev-parse", "HEAD"], cwd=cwd, runtime=self._runtime)
        commit_hash = hout.strip() if ok else "?"
        return ToolResult(
            success=True,
            output=f"Snapshot created: {commit_hash}\n{output}",
            metadata={"evidence": {"snapshot_commit": commit_hash}},
        )


class GitRevertTool(BaseTool):
    """回滚工作区到快照。

    mode=workspace → 丢弃所有未提交改动（git checkout -- .）
    mode=commit   → 恢复指定快照 commit 的内容（git checkout <hash> -- .）

    DANGEROUS：workspace 模式会丢失未提交工作。requires_user_interaction=True
    强制走人机确认（Phase 2 权限管线）。
    """

    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.WRITE_VCS}),
        path_access=PathAccess.WORKSPACE_WIDE,
        requires_user_interaction=True,
    )

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return False

    def __init__(self, runtime: Runtime | None = None) -> None:
        from core.process import LocalRuntime
        self._runtime = runtime or LocalRuntime()

    @property
    def name(self) -> str:
        return "git_revert"

    @property
    def risk_level(self) -> str:
        from core.base import RiskLevel
        return RiskLevel.HIGH

    @property
    def description(self) -> str:
        return (
            "Roll back the workspace to a snapshot. "
            "mode='workspace' discards ALL uncommitted changes (git checkout -- .). "
            "mode='commit' restores a snapshot commit's content (git checkout <hash> -- .). "
            "DANGEROUS: uncommitted work is lost. Requires confirmation."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["workspace", "commit"],
                    "default": "workspace",
                    "description": "workspace: discard uncommitted changes; commit: restore a snapshot",
                },
                "commit": {
                    "type": "string",
                    "description": "Snapshot commit hash (required when mode=commit)",
                },
                "cwd": {"type": "string", "description": "Repository root directory"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd")
        mode = params.get("mode", "workspace")

        if mode == "commit":
            commit = params.get("commit", "").strip()
            if not commit:
                return ToolResult(
                    success=False, output="",
                    error="commit hash is required when mode='commit'",
                )
            ok, output, _tool_err = _run_git(
                ["checkout", commit, "--", "."], cwd=cwd, runtime=self._runtime,
            )
            label = f"snapshot {commit}"
        else:
            ok, output, _tool_err = _run_git(
                ["checkout", "--", "."], cwd=cwd, runtime=self._runtime,
            )
            label = "last commit"

        return ToolResult(
            success=ok,
            output=f"Reverted workspace to {label}.\n{output}" if ok else output,
            error=None if ok else output,
            tool_error=_tool_err,
        )
