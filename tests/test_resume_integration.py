"""Phase 3A: 断点续传自动跳转集成 — Target 测试。

验证 RESUME_MARKER 记录（agent turn 边界）+ 恢复判定（evaluate_resume）
的完整闭环：marker 写入 run_evidence 表 → 重启后按 session 查询 →
比对 workspace 哈希 → 注入续传提示（跳过已完成 turns）。
"""

from __future__ import annotations

import pytest

from agent.session.models import SessionMode
from agent.session.run_evidence import (
    EvidenceStoreManager, evaluate_resume, workspace_files_hash,
)
from app.storage.sqlite import SqliteStorageBackend


def _setup(tmp_path):
    # DB 放在 .grace/ 下，避免 workspace_files_hash 把 DB 文件计入哈希
    (tmp_path / ".grace").mkdir(exist_ok=True)
    db = str(tmp_path / ".grace" / "resume.db")
    storage = SqliteStorageBackend(db)
    session = storage.create_session(
        agent_name="build", mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path), title="resume",
    )
    store = storage._store
    mgr = EvidenceStoreManager(
        persist_fn=store.create_evidence, list_fn=store.list_evidence,
    )
    return storage, store, session, mgr


def test_marker_record_then_evaluate_resume_matches(tmp_path):
    """记录 marker 后（workspace 未变）→ evaluate_resume 返回续传提示。"""
    storage, store, session, mgr = _setup(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    current_hash = workspace_files_hash(str(tmp_path))
    ev = mgr.acquire("run-1", root_session_id=session.id,
                     default_session_id=session.id)
    ev.record_resume_marker(session.id, turn_id="3",
                            tool_calls_hash="abc", files_hash=current_hash)

    msg = evaluate_resume(store, session.id, str(tmp_path))
    assert msg is not None
    assert "[RESUME]" in msg
    assert "turn 3" in msg


def test_evaluate_resume_none_without_marker(tmp_path):
    """无 marker → evaluate_resume 返回 None（不注入续传）。"""
    storage, store, session, mgr = _setup(tmp_path)
    assert evaluate_resume(store, session.id, str(tmp_path)) is None


def test_evaluate_resume_none_when_workspace_changed(tmp_path):
    """workspace 已变 → 放弃恢复（宁可重跑，R-D）。"""
    storage, store, session, mgr = _setup(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    current_hash = workspace_files_hash(str(tmp_path))

    ev = mgr.acquire("run-1", root_session_id=session.id,
                     default_session_id=session.id)
    ev.record_resume_marker(session.id, turn_id="3",
                            tool_calls_hash="abc", files_hash=current_hash)

    # 修改 workspace → hash 变化
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")

    msg = evaluate_resume(store, session.id, str(tmp_path))
    assert msg is None


def test_evaluate_resume_picks_latest_marker(tmp_path):
    """多个 marker → 选最近的一个（更高 turn）。"""
    storage, store, session, mgr = _setup(tmp_path)
    current_hash = workspace_files_hash(str(tmp_path))

    ev = mgr.acquire("run-1", root_session_id=session.id,
                     default_session_id=session.id)
    ev.record_resume_marker(session.id, turn_id="2", tool_calls_hash="h",
                            files_hash=current_hash)
    ev.record_resume_marker(session.id, turn_id="5", tool_calls_hash="h",
                            files_hash=current_hash)

    msg = evaluate_resume(store, session.id, str(tmp_path))
    assert msg is not None
    assert "turn 5" in msg


def test_agent_records_resume_marker(tmp_path):
    """ReActAgent 在 turn 边界记录 marker（_record_resume_marker）。"""
    from agent.core import ReActAgent

    calls = []

    class _FakeEvidenceStore:
        def record_resume_marker(self, session_id, turn_id,
                                 tool_calls_hash, files_hash):
            calls.append((session_id, turn_id, tool_calls_hash, files_hash))

    agent = ReActAgent.__new__(ReActAgent)
    agent._evidence_store = _FakeEvidenceStore()
    agent._current_repo_path = str(tmp_path)
    (tmp_path / "f.py").write_text("pass\n", encoding="utf-8")

    agent._record_resume_marker("s1", 7, None)

    assert len(calls) == 1
    assert calls[0][0] == "s1"
    assert calls[0][1] == "7"
    assert calls[0][3]  # files_hash 非空
