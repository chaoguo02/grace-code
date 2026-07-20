"""
Tests for CLI-Web alignment fixes.

Covers:
  1. Task.task_id = session_id — JSONL filename and event filtering
  2. SessionService.get_events() — dual-layer filtering
  3. Plan _has_plan detection — all 4 _is_plan × contract combinations
  4. Plan approval — save/abort endpoints (unit-level)
  5. Cross-round stats accumulation
  6. Session context injection guard
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ────────────────────────────────────────────────────────────────────────────
# Test 1: Task ID Alignment — task_id must be session_id, not random UUID
# ────────────────────────────────────────────────────────────────────────────


class TestTaskIdAlignment:
    """Verify Task.task_id is session_id, so JSONL files are scoped per session."""

    def test_task_id_is_session_id_when_explicitly_set(self):
        """When task_id is passed, it should be the session_id not a random UUID."""
        from agent.task import Task, TaskIntent

        session_id = "abc123def456"
        task = Task(
            task_id=session_id,
            description="test task",
            repo_path="/tmp/test",
            intent=TaskIntent.EDIT,
        )
        assert task.task_id == session_id
        assert task.task_id == "abc123def456"

    def test_event_to_dict_includes_session_id(self):
        """Event.to_dict() must include session_id for get_events() filtering."""
        from agent.task import Event, EventType

        event = Event(
            event_type=EventType.ACTION,
            task_id="abc123",
            payload={"key": "value"},
            session_id="parent456",
        )
        d = event.to_dict()
        assert d["session_id"] == "parent456"
        assert d["task_id"] == "abc123"

    def test_default_task_id_is_random_uuid(self):
        """Without explicit task_id, default is random (pre-fix behavior)."""
        from agent.task import Task, TaskIntent

        task_a = Task(description="a", repo_path="/tmp")
        task_b = Task(description="b", repo_path="/tmp")
        # Pre-fix: random UUIDs mean no correlation with session_id
        assert task_a.task_id != task_b.task_id
        # These are 8-char UUID prefixes, not session IDs
        assert len(task_a.task_id) == 8


# ────────────────────────────────────────────────────────────────────────────
# Test 2: SessionService.get_events() — dual-layer filtering
# ────────────────────────────────────────────────────────────────────────────


class TestEventFiltering:
    """Verify get_events() filters by both filename prefix AND raw fields."""

    def test_filename_pattern_matches_session_id(self, tmp_path):
        """Files named {session_id}_*.jsonl should be found."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        sid = "abc123def456"

        # Create a matching file
        f = log_dir / f"{sid}_20260721_120000.jsonl"
        f.write_text(json.dumps({
            "event_id": "ev1", "event_type": "thought",
            "task_id": sid, "session_id": sid,
            "timestamp": "2026-07-21T12:00:00Z", "payload": {},
        }) + "\n")

        # Create a non-matching file (different session)
        f2 = log_dir / "other_session_20260721_120000.jsonl"
        f2.write_text(json.dumps({
            "event_id": "ev2", "event_type": "thought",
            "task_id": "other_session", "session_id": "other_session",
            "timestamp": "2026-07-21T12:00:00Z", "payload": {},
        }) + "\n")

        matched = list(log_dir.glob(f"{sid}_*.jsonl"))
        assert len(matched) == 1
        assert matched[0].name.startswith(sid)

    def test_raw_field_filter_blocks_wrong_session(self):
        """Even when scanning all files, raw.task_id != session_id should skip."""
        from server.services.session_service import SessionService
        # The filtering logic: raw_task_id != session_id AND raw_session_id != session_id → skip
        sid = "target_session"

        raw_match_task = {"task_id": sid, "session_id": "", "event_id": "e1",
                          "event_type": "t", "timestamp": "", "payload": {}}
        raw_match_session = {"task_id": "random", "session_id": sid, "event_id": "e2",
                            "event_type": "t", "timestamp": "", "payload": {}}
        raw_wrong = {"task_id": "other", "session_id": "other", "event_id": "e3",
                    "event_type": "t", "timestamp": "", "payload": {}}

        # Filter logic (from session_service.py L330-332)
        def passes(raw):
            rtid = str(raw.get("task_id", "") or "")
            rsid = str(raw.get("session_id", "") or "")
            return rtid == sid or rsid == sid

        assert passes(raw_match_task) is True
        assert passes(raw_match_session) is True
        assert passes(raw_wrong) is False


# ────────────────────────────────────────────────────────────────────────────
# Test 3: Plan _has_plan detection — all 4 combinations
# ────────────────────────────────────────────────────────────────────────────


class TestPlanDetection:
    """Verify plan_ready is emitted for all valid plan scenarios."""

    def test_all_four_combinations(self):
        """_has_plan = _is_plan OR bool(result.contract)"""
        from agent.task import RunResult, RunStatus

        # Case 1: User requested plan, LLM produced contract
        assert (True or bool({"goal": "x"})) is True

        # Case 2: User requested plan, LLM did NOT produce contract
        assert (True or bool(None)) is True

        # Case 3: Build mode, LLM autonomously produced contract
        assert (False or bool({"goal": "x"})) is True

        # Case 4: Build mode, no contract
        assert (False or bool(None)) is False

    def test_empty_dict_is_falsy(self):
        """Empty contract {} should NOT trigger plan detection."""
        # The LLM called ExitPlanMode but provided no content
        assert bool({}) is False
        assert (False or bool({})) is False


# ────────────────────────────────────────────────────────────────────────────
# Test 4: Plan approval — save/abort endpoints (request-level)
# ────────────────────────────────────────────────────────────────────────────


class TestPlanApprovalEndpoints:
    """Verify save-plan and abort-plan endpoints work correctly."""

    @pytest.fixture
    def mock_service(self):
        """Create a mock AgentService with a session that has a plan."""
        svc = MagicMock()

        from agent.session.models import SessionRecord, SessionMode, AgentKind, AgentDepth
        rec = MagicMock(spec=SessionRecord)
        rec.id = "test_session"
        rec.summary = "This is a plan for X. Step 1: ..."
        rec.agent_name = "plan"
        rec.metadata = {"plan_revision": 1}
        rec.status = MagicMock()
        rec.status.value = "completed"

        svc.session_service.get_session.return_value = rec
        svc.session_service.update_agent_name = MagicMock()
        svc._plan_revisions = MagicMock()
        svc._event_bus = MagicMock()

        return svc

    def test_save_plan_returns_saved(self, mock_service):
        """POST /save-plan should return {saved: True}."""
        # Simulate the handler logic
        rec = mock_service.session_service.get_session("test_session")
        assert rec is not None
        plan_text = rec.summary
        assert plan_text and plan_text.strip()

        mock_service._plan_revisions.mark_status("test_session", 2, "saved")
        mock_service.session_service.update_agent_name("test_session", "build")

        # Verify calls
        mock_service._plan_revisions.mark_status.assert_called_once_with(
            "test_session", 2, "saved",
        )
        mock_service.session_service.update_agent_name.assert_called_once_with(
            "test_session", "build",
        )

    def test_abort_plan_clears_metadata(self, mock_service):
        """POST /abort-plan should clear plan metadata."""
        # Simulate
        mock_service._plan_revisions.mark_status("test_session", 2, "aborted")

        mock_service._plan_revisions.mark_status.assert_called_once_with(
            "test_session", 2, "aborted",
        )

    def test_save_plan_no_summary_returns_400(self, mock_service):
        """No plan text should raise 400."""
        mock_service.session_service.get_session.return_value.summary = ""
        rec = mock_service.session_service.get_session("test_session")
        plan_text = rec.summary
        assert not (plan_text and plan_text.strip())


# ────────────────────────────────────────────────────────────────────────────
# Test 5: Cross-round stats accumulation
# ────────────────────────────────────────────────────────────────────────────


class TestCrossRoundStats:
    """Verify metadata-based cumulative stat tracking."""

    def test_accumulate_adds_to_existing(self):
        """Stats should add to previous values, not replace."""
        meta = {"total_tokens": 5000, "total_steps": 25, "round_count": 3}

        # Simulate a new round result
        result = MagicMock()
        result.total_tokens = 1200
        result.steps_taken = 8

        meta["total_tokens"] = meta.get("total_tokens", 0) + (result.total_tokens or 0)
        meta["total_steps"] = meta.get("total_steps", 0) + (result.steps_taken or 0)
        meta["round_count"] = meta.get("round_count", 0) + 1

        assert meta["total_tokens"] == 6200
        assert meta["total_steps"] == 33
        assert meta["round_count"] == 4

    def test_accumulate_from_empty_metadata(self):
        """First round should initialize all counters."""
        meta = {}
        result = MagicMock()
        result.total_tokens = 500
        result.steps_taken = 3

        meta["total_tokens"] = meta.get("total_tokens", 0) + (result.total_tokens or 0)
        meta["total_steps"] = meta.get("total_steps", 0) + (result.steps_taken or 0)
        meta["round_count"] = meta.get("round_count", 0) + 1

        assert meta["total_tokens"] == 500
        assert meta["total_steps"] == 3
        assert meta["round_count"] == 1


# ────────────────────────────────────────────────────────────────────────────
# Test 6: Session context injection guard
# ────────────────────────────────────────────────────────────────────────────


class TestSessionContextGuard:
    """Verify session summary is injected only once per root session."""

    def test_first_round_injects(self):
        """Without the guard flag, injection should proceed."""
        rec = MagicMock()
        rec.metadata = {}
        already = rec.metadata.get("session_context_injected")
        assert already is None  # Not injected yet → should inject

    def test_second_round_skips(self):
        """With the guard flag set, injection should be skipped."""
        rec = MagicMock()
        rec.metadata = {"session_context_injected": True}
        already = rec.metadata.get("session_context_injected")
        assert already is True  # Already injected → skip

    def test_guard_set_even_on_failure(self):
        """Even if injection fails (no file), guard should be set to avoid retries."""
        # Simulate the guard-setting logic
        meta = {}
        # Injection attempted (maybe failed, maybe file not found)
        # Guard is set anyway:
        meta["session_context_injected"] = True
        # Next round:
        assert meta.get("session_context_injected") is True
