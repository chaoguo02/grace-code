"""Phase 3A: Evidence 断点续传 — Target 测试。

不维护 step counter（对齐 CC）。用 workspace 文件快照哈希 + turn 边界
RESUME_MARKER 判定"已完成 turns 是否可跳过"。宁可重跑，不可错误跳过。
"""

from __future__ import annotations

import pytest

from agent.session.run_evidence import (
    EvidenceKind, EvidenceStatus, EvidenceEntry, RunEvidenceStore,
    workspace_files_hash, should_resume_from_marker,
)


def _make_store() -> RunEvidenceStore:
    return RunEvidenceStore(
        root_run_id="run-1",
        root_session_id="s-root",
        default_session_id="s1",
    )


def test_record_resume_marker_creates_marker():
    store = _make_store()
    entry = store.record_resume_marker(
        "s1", turn_id="t1",
        tool_calls_hash="abc123",
        files_hash="ws-hash-1",
    )
    assert entry.kind is EvidenceKind.RESUME_MARKER
    assert entry.status is EvidenceStatus.SUCCEEDED
    assert entry.turn_id == "t1"
    assert entry.metadata["tool_calls_hash"] == "abc123"
    assert entry.metadata["files_hash"] == "ws-hash-1"


def test_find_last_resume_marker_returns_most_recent():
    store = _make_store()
    store.record_resume_marker("s1", turn_id="t1", tool_calls_hash="h1", files_hash="f1")
    store.record_resume_marker("s1", turn_id="t2", tool_calls_hash="h2", files_hash="f2")

    last = store.find_last_resume_marker("s1")
    assert last is not None
    assert last.turn_id == "t2"


def test_find_last_resume_marker_none_when_absent():
    store = _make_store()
    assert store.find_last_resume_marker("s1") is None


def test_resume_marker_idempotent_same_turn():
    store = _make_store()
    e1 = store.record_resume_marker("s1", "t1", "h", "f")
    e2 = store.record_resume_marker("s1", "t1", "h", "f")
    assert e1.evidence_id == e2.evidence_id  # 同 turn 幂等，不重复


def test_workspace_hash_stable_then_changes(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    h1 = workspace_files_hash(str(tmp_path))
    h2 = workspace_files_hash(str(tmp_path))
    assert h1 == h2, "同一 workspace 快照哈希必须稳定"

    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    h3 = workspace_files_hash(str(tmp_path))
    assert h3 != h1, "文件变化后哈希必须变化"


def test_workspace_hash_skips_git_and_deps(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("repo\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("dep\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("pass\n", encoding="utf-8")

    h1 = workspace_files_hash(str(tmp_path))
    # 修改被跳过的目录内容 → 哈希不变
    (tmp_path / ".git" / "config").write_text("changed\n", encoding="utf-8")
    h2 = workspace_files_hash(str(tmp_path))
    assert h1 == h2


def test_should_resume_matches_only_when_hash_equal():
    store = _make_store()
    store.record_resume_marker("s1", "t1", "h", files_hash="ws-A")

    marker = store.find_last_resume_marker("s1")
    # workspace 未变 → 可跳过
    assert should_resume_from_marker(marker, "ws-A") is True
    # workspace 已变 → 放弃恢复（宁可重跑，R-D）
    assert should_resume_from_marker(marker, "ws-B") is False
    # 无 marker → 不可跳过
    assert should_resume_from_marker(None, "ws-A") is False
