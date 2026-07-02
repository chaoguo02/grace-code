"""
tests/test_session_memory.py

Tests for SessionMemoryTracker (session-level auto-extraction).
"""

import time
from pathlib import Path

from memory.session_memory import (
    INIT_TOKEN_THRESHOLD,
    UPDATE_TOKEN_DELTA,
    UPDATE_TOOL_CALLS,
    SESSION_NOTES_TEMPLATE,
    SessionMemoryTracker,
    ThreadedSessionMemorySubagent,
)


class FakeLLMResponse:
    def __init__(self, text: str):
        self.text = text


class FakeBackend:
    def __init__(self, response_text: str = ""):
        self._response_text = response_text
        self.call_count = 0

    def complete(self, messages, tools=None):
        self.call_count += 1
        return FakeLLMResponse(self._response_text)


class RecordingRunner:
    allowed_tools = ("file_write",)

    def __init__(self):
        self.allowed_paths: tuple[Path, ...] = ()
        self.calls = []
        self.running = False

    def fork(self, *, prompt: str, notes_path: Path, current_notes: str) -> None:
        self.calls.append({"prompt": prompt, "notes_path": notes_path, "current_notes": current_notes})


class TestTriggerConditions:
    def test_no_trigger_below_init_threshold(self, tmp_path):
        runner = RecordingRunner()
        tracker = SessionMemoryTracker(
            backend=FakeBackend(),
            notes_path=tmp_path / "session.md",
            runner=runner,
        )
        triggered = tracker.tick(
            current_tokens=9999,
            current_tool_calls=50,
            context_summary="some context",
        )
        assert triggered is False
        assert runner.calls == []

    def test_initial_trigger_at_threshold(self, tmp_path):
        runner = RecordingRunner()
        tracker = SessionMemoryTracker(
            backend=FakeBackend(),
            notes_path=tmp_path / "session.md",
            runner=runner,
        )
        triggered = tracker.tick(
            current_tokens=INIT_TOKEN_THRESHOLD,
            current_tool_calls=0,
            context_summary="Initial work done",
        )
        assert triggered is True
        assert len(runner.calls) == 1
        assert "Initial work done" in runner.calls[0]["prompt"]

    def test_token_delta_trigger(self, tmp_path):
        runner = RecordingRunner()
        tracker = SessionMemoryTracker(
            backend=FakeBackend(),
            notes_path=tmp_path / "session.md",
            runner=runner,
        )
        tracker.tick(current_tokens=10_000, current_tool_calls=0, context_summary="ctx")
        assert len(runner.calls) == 1

        triggered = tracker.tick(current_tokens=14_000, current_tool_calls=0, context_summary="ctx")
        assert triggered is False

        triggered = tracker.tick(current_tokens=15_000, current_tool_calls=0, context_summary="ctx2")
        assert triggered is True
        assert len(runner.calls) == 2

    def test_tool_call_trigger(self, tmp_path):
        runner = RecordingRunner()
        tracker = SessionMemoryTracker(
            backend=FakeBackend(),
            notes_path=tmp_path / "session.md",
            runner=runner,
        )
        tracker.tick(current_tokens=10_000, current_tool_calls=0, context_summary="ctx")
        assert len(runner.calls) == 1

        triggered = tracker.tick(current_tokens=10_001, current_tool_calls=2, context_summary="ctx")
        assert triggered is False

        triggered = tracker.tick(current_tokens=10_002, current_tool_calls=3, context_summary="ctx2")
        assert triggered is True
        assert len(runner.calls) == 2

    def test_idle_turn_is_not_independent_trigger(self, tmp_path):
        runner = RecordingRunner()
        tracker = SessionMemoryTracker(
            backend=FakeBackend(),
            notes_path=tmp_path / "session.md",
            runner=runner,
        )
        tracker.tick(current_tokens=10_000, current_tool_calls=0, context_summary="ctx")
        assert len(runner.calls) == 1

        triggered = tracker.tick(
            current_tokens=10_100,
            current_tool_calls=0,
            context_summary="summary turn ctx",
            last_turn_had_tools=False,
        )
        assert triggered is False
        assert len(runner.calls) == 1

    def test_extraction_not_triggered_without_context(self, tmp_path):
        runner = RecordingRunner()
        tracker = SessionMemoryTracker(
            backend=FakeBackend(),
            notes_path=tmp_path / "session.md",
            runner=runner,
        )
        triggered = tracker.tick(
            current_tokens=20_000,
            current_tool_calls=10,
            context_summary="",
        )
        assert triggered is False
        assert runner.calls == []


class TestRestrictedSubagentRunner:
    def test_default_runner_declares_strict_permissions(self, tmp_path):
        notes_path = tmp_path / "session.md"
        runner = ThreadedSessionMemorySubagent(FakeBackend(), notes_path)
        assert runner.allowed_tools == ("file_write",)
        assert runner.allowed_paths == (notes_path.resolve(),)

    def test_default_runner_rejects_other_write_paths(self, tmp_path):
        runner = ThreadedSessionMemorySubagent(FakeBackend(), tmp_path / "session.md")
        try:
            runner.fork(prompt="x", notes_path=tmp_path / "other.md", current_notes="")
        except ValueError as exc:
            assert "outside allowed path" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_default_runner_writes_valid_notes_only(self, tmp_path):
        notes_path = tmp_path / "session.md"
        valid_output = SESSION_NOTES_TEMPLATE.format(title="My Session") + "\nUpdated."
        backend = FakeBackend(response_text=valid_output)
        runner = ThreadedSessionMemorySubagent(backend, notes_path)
        notes_path.write_text(SESSION_NOTES_TEMPLATE.format(title="My Session"), encoding="utf-8")
        runner.fork(prompt="ctx", notes_path=notes_path, current_notes=notes_path.read_text(encoding="utf-8"))
        time.sleep(0.3)
        assert backend.call_count == 1
        assert "# 当前状态" in notes_path.read_text(encoding="utf-8")


class TestFinalize:
    def test_finalize_creates_template_if_no_extraction(self, tmp_path):
        notes_path = tmp_path / "notes" / "session.md"
        tracker = SessionMemoryTracker(
            backend=FakeBackend(),
            notes_path=notes_path,
            session_title="Final Session",
        )
        tracker.finalize()
        content = notes_path.read_text(encoding="utf-8")
        assert "# Final Session" in content
        assert "# 当前状态" in content

    def test_finalize_noop_if_file_exists(self, tmp_path):
        notes_path = tmp_path / "session.md"
        notes_path.write_text("custom content", encoding="utf-8")
        tracker = SessionMemoryTracker(backend=FakeBackend(), notes_path=notes_path)
        tracker.finalize()
        assert notes_path.read_text(encoding="utf-8") == "custom content"


class TestThresholdConstants:
    def test_init_threshold(self):
        assert INIT_TOKEN_THRESHOLD == 10_000

    def test_update_delta(self):
        assert UPDATE_TOKEN_DELTA == 5_000

    def test_update_tool_calls(self):
        assert UPDATE_TOOL_CALLS == 3
