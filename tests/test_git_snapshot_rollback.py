"""Phase 3 补充: Git 快照/回滚 — Target 测试。

对齐架构 S1 "Git 作为唯一状态源 + 快照回滚"：
- git_snapshot: git add -A + commit，返回快照 hash（关键修改前调用）
- git_revert:  mode=workspace 丢弃未提交改动 / mode=commit 恢复快照内容
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.process import LocalRuntime
from tools.git_tool import GitRevertTool, GitSnapshotTool


_NEED_GIT = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not available",
)


def _snapshot_tool(tmp_path):
    return GitSnapshotTool(runtime=LocalRuntime(workspace_root=str(tmp_path)))


def _revert_tool(tmp_path):
    return GitRevertTool(runtime=LocalRuntime(workspace_root=str(tmp_path)))


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    _run(["git", "init"], path)
    _run(["git", "config", "user.email", "test@test.local"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "-A"], path)
    _run(["git", "commit", "-m", "init"], path)


def _head(path: Path) -> str:
    r = _run(["git", "rev-parse", "HEAD"], path)
    return r.stdout.strip()


@_NEED_GIT
def test_snapshot_creates_commit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "base.txt").write_text("changed\n", encoding="utf-8")

    result = GitSnapshotTool(runtime=LocalRuntime(workspace_root=str(tmp_path))).execute({"cwd": str(tmp_path)})

    assert result.success is True, f"snapshot 应成功，got {result.output}"
    # 生成了新 commit（HEAD 变化）
    assert "Snapshot created:" in result.output
    assert _head(tmp_path) == result.metadata["evidence"]["snapshot_commit"]


@_NEED_GIT
def test_snapshot_no_changes_returns_head(tmp_path):
    _init_repo(tmp_path)

    result = GitSnapshotTool(runtime=LocalRuntime(workspace_root=str(tmp_path))).execute({"cwd": str(tmp_path)})

    assert result.success is True
    assert "No changes to snapshot" in result.output


@_NEED_GIT
def test_revert_workspace_discards_uncommitted_changes(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "base.txt").write_text("dirty\n", encoding="utf-8")

    result = GitRevertTool(runtime=LocalRuntime(workspace_root=str(tmp_path))).execute({
        "cwd": str(tmp_path), "mode": "workspace",
    })

    assert result.success is True
    assert (tmp_path / "base.txt").read_text(encoding="utf-8") == "base\n"
    assert "Reverted workspace" in result.output


@_NEED_GIT
def test_revert_commit_restores_snapshot(tmp_path):
    _init_repo(tmp_path)
    # 快照 v1（base.txt = "v1"）
    (tmp_path / "base.txt").write_text("v1\n", encoding="utf-8")
    GitSnapshotTool(runtime=LocalRuntime(workspace_root=str(tmp_path))).execute({"cwd": str(tmp_path), "message": "v1"})
    v1_hash = _head(tmp_path)

    # 快照 v2（base.txt = "v2"）
    (tmp_path / "base.txt").write_text("v2\n", encoding="utf-8")
    GitSnapshotTool(runtime=LocalRuntime(workspace_root=str(tmp_path))).execute({"cwd": str(tmp_path), "message": "v2"})

    # 再修改（污染）
    (tmp_path / "base.txt").write_text("polluted\n", encoding="utf-8")

    # 回滚到 v1
    result = GitRevertTool(runtime=LocalRuntime(workspace_root=str(tmp_path))).execute({
        "cwd": str(tmp_path), "mode": "commit", "commit": v1_hash,
    })

    assert result.success is True, f"revert 应成功，got {result.output}"
    assert (tmp_path / "base.txt").read_text(encoding="utf-8") == "v1\n"
    # HEAD 未变（checkout <hash> -- . 不改历史）
    assert _head(tmp_path) != v1_hash


@_NEED_GIT
def test_revert_commit_requires_hash(tmp_path):
    _init_repo(tmp_path)
    result = GitRevertTool(runtime=LocalRuntime(workspace_root=str(tmp_path))).execute({"cwd": str(tmp_path), "mode": "commit"})
    assert result.success is False
    assert "commit hash is required" in (result.error or "")


def test_revert_is_requires_user_interaction():
    """回滚是危险操作，必须 requires_user_interaction=True（强制确认）。"""
    assert GitRevertTool.metadata.requires_user_interaction is True


def test_revert_risk_level_high():
    from core.base import RiskLevel
    assert GitRevertTool().risk_level == RiskLevel.HIGH
    assert GitSnapshotTool().risk_level == RiskLevel.HIGH
