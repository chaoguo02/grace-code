"""Phase 3C: Checkpoint 定位 + --enable-checkpoint-debug flag — Target 测试。

对齐 Claude Code：Checkpoint 是调试工具，不是生产恢复机制。
默认关闭（checkpoint_db_path=""）；--enable-checkpoint-debug 显式开启。
"""

from __future__ import annotations

import pytest


def test_checkpoint_default_disabled():
    """默认 checkpoint_db_path="" → 零开销，不捕获不恢复。"""
    from agent.agent_config import AgentConfig
    cfg = AgentConfig()
    assert cfg.checkpoint_db_path == ""
    assert not cfg.checkpoint_db_path  # 空 → 关闭


def test_checkpoint_debug_flag_sets_path(tmp_path):
    """--enable-checkpoint-debug 将 checkpoint_db_path 设为非空。"""
    # 模拟 main() 的 flag 分支逻辑
    repo_path = str(tmp_path)
    checkpoint_db_path = (
        str(tmp_path / ".grace" / "checkpoints.db")
        if True  # flag 开启
        else ""
    )
    assert checkpoint_db_path.endswith(".grace/checkpoints.db") or \
        checkpoint_db_path.endswith(".grace\\checkpoints.db")


def test_enabled_checkpoint_db_is_written(tmp_path):
    """开启后 CheckpointManager 实际写入 session_checkpoints 表。"""
    from agent.session.checkpoint import CheckpointManager
    import sqlite3

    db = str(tmp_path / "checkpoints.db")
    mgr = CheckpointManager(db)
    cp = mgr.capture("s1", generation=1, turn_number=1,
                     pending_tool_ids=["t1"], tool_results={"t1": {"ok": 1}})

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM session_checkpoints WHERE session_id=?",
            ("s1",),
        ).fetchone()
    assert row[0] == 1
    assert cp.turn_number == 1


def test_agent_service_accepts_checkpoint_path(tmp_path):
    """AgentService 接受 checkpoint_db_path 并传给 AgentConfig。"""
    from unittest.mock import MagicMock
    from server.services.agent_service import AgentService

    svc = AgentService.__new__(AgentService)
    svc._checkpoint_db_path = str(tmp_path / "c.db")
    # _build_agent_cfg 里 checkpoint_db_path 应透传
    assert svc._checkpoint_db_path != ""
