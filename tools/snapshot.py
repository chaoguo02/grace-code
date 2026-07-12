"""
tools/snapshot.py

Worktree 管理器，为多 Agent 并行执行提供文件系统隔离。

核心职责：
- 创建 git worktree（独立工作目录 + 独立分支）
- 合并 worktree 的修改回主分支
- 清理/丢弃 worktree

设计：
- 每个并行 Executor 获得一个独立的 worktree
- Executor 在 worktree 中自由修改文件，不影响主分支
- Coordinator 按拓扑序合并各 worktree
- 合并冲突时由 Coordinator 裁决（或上报用户）

用法：
    manager = WorktreeManager(repo_path)
    wt = manager.create("feature-auth-fix")
    # ... Executor 在 wt.path 中工作 ...
    manager.merge(wt)       # 合并回主分支
    manager.discard(wt)     # 或丢弃
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Worktree:
    """一个 git worktree 实例。"""
    name: str
    path: str
    branch: str
    base_branch: str


class WorktreeError(Exception):
    """Worktree 操作失败。"""
    pass


class WorktreeManager:
    """
    Git Worktree 生命周期管理。

    为每个并行子 Agent 创建独立的工作目录，
    避免多个 Executor 同时修改同一目录造成冲突。
    """

    def __init__(self, repo_path: str, runtime: "Runtime | None" = None) -> None:
        self._repo_path = Path(repo_path).resolve()
        self._worktrees: dict[str, Worktree] = {}
        # Runtime injection: all git commands go through execute(), never raw subprocess.
        # This ensures Docker sandbox compatibility and audit trail.
        if runtime is None:
            from tools.runtime import LocalRuntime as _LR
            runtime = _LR()
        self._runtime = runtime

    @property
    def repo_path(self) -> str:
        return str(self._repo_path)

    @property
    def active_worktrees(self) -> list[Worktree]:
        return list(self._worktrees.values())

    def create(self, name: str, base_branch: str | None = None) -> Worktree:
        """
        创建一个新的 worktree。

        Args:
            name:        worktree 名称（也用作分支名前缀）
            base_branch: 从哪个分支创建（默认当前 HEAD）

        Returns:
            Worktree 实例

        Raises:
            WorktreeError: git 操作失败
        """
        if name in self._worktrees:
            raise WorktreeError(f"Worktree '{name}' already exists")

        # 确定 worktree 路径：放在 .worktrees/ 子目录下
        wt_dir = self._repo_path / ".worktrees"
        wt_dir.mkdir(exist_ok=True)
        wt_path = wt_dir / name

        if wt_path.exists():
            raise WorktreeError(f"Path already exists: {wt_path}")

        # 确定基础分支
        if base_branch is None:
            base_branch = self._current_branch()

        # 创建新分支名
        branch_name = f"multi-agent/{name}"

        try:
            self._run_git(
                ["worktree", "add", "-b", branch_name, str(wt_path), base_branch],
            )
        except subprocess.CalledProcessError as e:
            raise WorktreeError(f"Failed to create worktree: {e.stderr}") from e

        wt = Worktree(
            name=name,
            path=str(wt_path),
            branch=branch_name,
            base_branch=base_branch,
        )
        self._worktrees[name] = wt
        logger.info("Created worktree '%s' at %s (branch: %s)", name, wt_path, branch_name)
        return wt

    def merge(self, wt: Worktree, delete_after: bool = True) -> str:
        """
        将 worktree 的修改合并回基础分支。

        Args:
            wt:           要合并的 worktree
            delete_after: 合并后是否自动清理 worktree

        Returns:
            合并的 git log 摘要

        Raises:
            WorktreeError: 合并冲突或 git 操作失败
        """
        # 检查 worktree 是否有 commit
        try:
            diff_output = self._run_git(
                ["log", "--oneline", f"{wt.base_branch}..{wt.branch}"],
            )
        except subprocess.CalledProcessError:
            diff_output = ""

        if not diff_output.strip():
            logger.info("Worktree '%s' has no new commits, nothing to merge", wt.name)
            if delete_after:
                self.discard(wt)
            return "(no changes)"

        # 确保主分支当前是 base_branch
        current = self._current_branch()
        if current != wt.base_branch:
            try:
                self._run_git(["checkout", wt.base_branch])
            except subprocess.CalledProcessError as e:
                raise WorktreeError(f"Cannot switch to {wt.base_branch}: {e.stderr}") from e

        # 执行合并
        try:
            merge_output = self._run_git(
                ["merge", "--no-ff", wt.branch, "-m", f"Merge multi-agent/{wt.name}"],
            )
        except subprocess.CalledProcessError as e:
            # 合并冲突
            self._run_git(["merge", "--abort"])
            raise WorktreeError(
                f"Merge conflict when merging '{wt.name}': {e.stderr}"
            ) from e

        if delete_after:
            self.discard(wt)

        logger.info("Merged worktree '%s' into %s", wt.name, wt.base_branch)
        return diff_output.strip()

    def discard(self, wt: Worktree) -> None:
        """
        丢弃一个 worktree（删除目录 + 删除分支）。

        Args:
            wt: 要丢弃的 worktree
        """
        try:
            self._run_git(["worktree", "remove", "--force", str(wt.path)])
        except subprocess.CalledProcessError:
            # worktree remove 失败时尝试手动清理
            import shutil
            wt_path = Path(wt.path)
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
            try:
                self._run_git(["worktree", "prune"])
            except subprocess.CalledProcessError:
                pass

        # 删除分支
        try:
            self._run_git(["branch", "-D", wt.branch])
        except subprocess.CalledProcessError:
            pass  # 分支可能已被删除

        self._worktrees.pop(wt.name, None)
        logger.info("Discarded worktree '%s'", wt.name)

    def discard_all(self) -> None:
        """丢弃所有活跃的 worktree。"""
        for wt in list(self._worktrees.values()):
            self.discard(wt)

    def get_diff(self, wt: Worktree) -> str:
        """获取 worktree 相对于基础分支的 diff。"""
        try:
            return self._run_git(["diff", f"{wt.base_branch}...{wt.branch}"])
        except subprocess.CalledProcessError:
            return ""

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _run_git(self, args: list[str], cwd: str | None = None) -> str:
        """Execute a git command through Runtime (NOT raw subprocess).

        Uses Runtime.execute() with shell=False — works in Docker sandbox mode.
        """
        from tools.runtime import RunResult
        target_cwd = cwd or str(self._repo_path)
        result: RunResult = self._runtime.execute("git", args=args, cwd=target_cwd, timeout=30)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, ["git"] + args,
                output=result.stdout, stderr=result.stderr,
            )
        return result.stdout

    def _current_branch(self) -> str:
        """获取当前分支名。"""
        try:
            return self._run_git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        except subprocess.CalledProcessError:
            return "HEAD"
