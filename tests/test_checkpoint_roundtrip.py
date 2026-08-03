"""U1: Checkpoint capture/restore round-trip.

Validates the previously-dead CheckpointManager path:
  - AgentConfig carries checkpoint_db_path (default "" = disabled)
  - capture() writes a row, restore() reads it back
  - IdempotentToolCache consumes the checkpoint for replay dedup
  - ReActAgent._capture_turn_checkpoint actually writes when configured
"""

from __future__ import annotations

from pathlib import Path

from agent.agent_config import AgentConfig
from agent.session.checkpoint import CheckpointManager, IdempotentToolCache


def test_agent_config_has_checkpoint_db_path_field():
    cfg = AgentConfig()
    assert cfg.checkpoint_db_path == ""


def test_checkpoint_capture_restore_roundtrip(tmp_path):
    db = str(tmp_path / "cp.db")
    mgr = CheckpointManager(db)
    mgr.capture(
        "sess-1", generation=0, turn_number=3,
        pending_tool_ids=["t2"],
        tool_results={"t1": {"success": True, "output": "hello"}},
        active_skills=[{"name": "skill-a"}],
    )
    restored = mgr.restore("sess-1")
    assert restored is not None
    assert restored.turn_number == 3
    assert restored.generation == 0

    results = mgr.get_tool_results("sess-1")
    assert results["t1"]["output"] == "hello"
    pending = mgr.get_pending_ids("sess-1")
    assert "t2" in pending


def test_idempotent_cache_loads_from_checkpoint(tmp_path):
    db = str(tmp_path / "cp.db")
    mgr = CheckpointManager(db)
    mgr.capture(
        "sess-1", generation=0, turn_number=1,
        tool_results={"tool_x": {"success": True, "output": "cached"}},
    )
    cache = IdempotentToolCache()
    cache.load_from_checkpoint(mgr.restore("sess-1"))
    assert cache.get("tool_x")["output"] == "cached"


def test_capture_turn_checkpoint_writes_db_when_configured(tmp_path):
    """Configured checkpoint_db_path → a row is written (was dead before U1)."""
    from agent.core import ReActAgent
    from types import SimpleNamespace

    db = str(tmp_path / "cp.db")
    agent = ReActAgent.__new__(ReActAgent)
    agent._db_path = None
    agent._cfg = SimpleNamespace(checkpoint_db_path=db)
    agent._capture_turn_checkpoint("sess-1", 0, 1, None)

    mgr = CheckpointManager(db)
    restored = mgr.restore("sess-1")
    assert restored is not None, "checkpoint must be written when db_path configured"
    assert restored.session_id == "sess-1"


def test_capture_turn_checkpoint_noop_when_disabled():
    """checkpoint_db_path="" (default) → no-op, zero overhead."""
    from agent.core import ReActAgent
    from types import SimpleNamespace

    agent = ReActAgent.__new__(ReActAgent)
    agent._db_path = None
    agent._cfg = SimpleNamespace(checkpoint_db_path="")
    # Must not raise and must not create any DB
    agent._capture_turn_checkpoint("sess-1", 0, 1, None)
