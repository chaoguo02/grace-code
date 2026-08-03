"""P1-1: Step Checkpoint + Idempotent Tool Cache — acceptance tests.

AC mappings:
  AC-1  capture → restore returns last checkpoint
  AC-2  IdempotentToolCache returns cached result for same invocation_id
  AC-3  prune(keep_last_n=3) keeps only last 3 checkpoints
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "test_checkpoint.db")
    yield p


class TestCheckpointCaptureRestore:

    def test_capture_and_restore(self, db_path):
        from agent.session.checkpoint import CheckpointManager

        mgr = CheckpointManager(db_path)
        mgr.capture(
            "s1", generation=1, turn_number=5,
            pending_tool_ids=["t1", "t2"],
            tool_results={"t1": {"success": True, "output": "done"}},
        )
        cp = mgr.restore("s1")
        assert cp is not None
        assert cp.turn_number == 5
        pending = json.loads(cp.pending_tool_ids_json)
        assert "t1" in pending

    def test_restore_returns_latest(self, db_path):
        from agent.session.checkpoint import CheckpointManager

        mgr = CheckpointManager(db_path)
        mgr.capture("s1", 1, 1, pending_tool_ids=["old"])
        mgr.capture("s1", 1, 3, pending_tool_ids=["new"])

        cp = mgr.restore("s1")
        assert cp.turn_number == 3
        pending = json.loads(cp.pending_tool_ids_json)
        assert "new" in pending

    def test_restore_nonexistent(self, db_path):
        from agent.session.checkpoint import CheckpointManager

        mgr = CheckpointManager(db_path)
        assert mgr.restore("no_such_session") is None

    def test_get_tool_results(self, db_path):
        from agent.session.checkpoint import CheckpointManager

        mgr = CheckpointManager(db_path)
        mgr.capture("s1", 1, 1, tool_results={"inv_a": {"success": True, "output": "x"}})
        results = mgr.get_tool_results("s1")
        assert "inv_a" in results

    def test_get_pending_ids(self, db_path):
        from agent.session.checkpoint import CheckpointManager

        mgr = CheckpointManager(db_path)
        mgr.capture("s1", 1, 1, pending_tool_ids=["a", "b"])
        ids = mgr.get_pending_ids("s1")
        assert ids == ["a", "b"]


class TestCheckpointPrune:

    def test_prune_keeps_last_n(self, db_path):
        from agent.session.checkpoint import CheckpointManager

        mgr = CheckpointManager(db_path)
        for i in range(1, 8):
            mgr.capture("s1", 1, i)

        mgr.prune("s1", keep_last_n=3)
        # Only turns 5,6,7 should remain
        cp = mgr.restore("s1")
        assert cp.turn_number == 7
        # Verify turn 1 was pruned
        with mgr._connect() as conn:
            rows = conn.execute(
                "SELECT turn_number FROM session_checkpoints WHERE session_id=? ORDER BY turn_number",
                ("s1",),
            ).fetchall()
            turns = [r["turn_number"] for r in rows]
            assert len(turns) == 3
            assert 1 not in turns


class TestIdempotentToolCache:

    def test_load_from_checkpoint(self, db_path):
        from agent.session.checkpoint import IdempotentToolCache, CheckpointManager

        mgr = CheckpointManager(db_path)
        mgr.capture("s1", 1, 1, tool_results={"inv_x": {"success": True, "output": "cached"}})
        cp = mgr.restore("s1")

        cache = IdempotentToolCache()
        cache.load_from_checkpoint(cp)
        assert cache.get("inv_x") is not None

    def test_put_and_get(self):
        from agent.session.checkpoint import IdempotentToolCache

        cache = IdempotentToolCache()
        cache.put("inv_1", {"success": True})
        assert cache.get("inv_1") == {"success": True}

    def test_miss(self):
        from agent.session.checkpoint import IdempotentToolCache

        cache = IdempotentToolCache()
        assert cache.get("no_such") is None
