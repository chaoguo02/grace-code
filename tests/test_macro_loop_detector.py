"""Unit tests for agent/v2/macro_loop_detector.py — Macro-action loop detection."""

import pytest
from agent.v2.macro_loop_detector import (
    MacroActionType,
    MacroActionRecord,
    MacroLoopDetector,
    MacroLoopDetectorConfig,
    MacroLoopTripped,
    _TOOL_TO_MACRO,
)


class TestMacroActionMapping:
    def test_tool_to_macro_mapping(self):
        assert _TOOL_TO_MACRO["task"] == MacroActionType.SPAWN_SUBAGENT
        assert _TOOL_TO_MACRO["file_read"] == MacroActionType.READ_FILE
        assert _TOOL_TO_MACRO["file_view"] == MacroActionType.READ_FILE
        assert _TOOL_TO_MACRO["search_text"] == MacroActionType.SEARCH_CODE
        assert _TOOL_TO_MACRO["find_files"] == MacroActionType.SEARCH_CODE
        assert _TOOL_TO_MACRO["file_write"] == MacroActionType.WRITE_FILE
        assert _TOOL_TO_MACRO["file_edit"] == MacroActionType.WRITE_FILE
        assert _TOOL_TO_MACRO["bash"] == MacroActionType.RUN_SHELL
        assert _TOOL_TO_MACRO["shell"] == MacroActionType.RUN_SHELL
        assert _TOOL_TO_MACRO["test"] == MacroActionType.VALIDATE
        assert _TOOL_TO_MACRO["finish"] == MacroActionType.FINISH

    def test_unknown_tool_maps_to_other(self):
        assert _TOOL_TO_MACRO.get("unknown_tool", MacroActionType.OTHER) == MacroActionType.OTHER


class TestMacroActionRecord:
    def test_signature_format(self):
        record = MacroActionRecord(
            action_type=MacroActionType.SPAWN_SUBAGENT,
            tool_name="task",
            detail="explore",
        )
        assert record.signature() == "spawn_subagent:task"

    def test_signature_immutable(self):
        record = MacroActionRecord(
            action_type=MacroActionType.READ_FILE,
            tool_name="file_read",
        )
        # frozen dataclass — cannot modify
        with pytest.raises(Exception):
            record.action_type = MacroActionType.WRITE_FILE  # type: ignore[misc]


class TestMacroLoopDetectorBasic:
    def test_initial_state(self):
        detector = MacroLoopDetector()
        assert not detector.is_tripped
        assert detector.trip_reason == ""
        assert len(detector._history) == 0

    def test_disabled_never_trips(self):
        detector = MacroLoopDetector(config=MacroLoopDetectorConfig(enabled=False))
        for _ in range(20):
            detector.record_tool_call("task", {"subagent_type": "explore"})
        assert not detector.is_tripped

    def test_single_tool_call_does_not_trip(self):
        detector = MacroLoopDetector()
        detector.record_tool_call("file_read", {"path": "a.py"})
        assert not detector.is_tripped


class TestMacroLoopDetectorRepeatingPattern:
    def test_spawn_read_spawn_read_spawn_trips(self):
        """Pattern: SPAWN → READ → SPAWN → READ → SPAWN — should detect."""
        detector = MacroLoopDetector(config=MacroLoopDetectorConfig(
            window_size=6, min_repetitions=2, min_pattern_length=2,
        ))
        # Fill enough history for window
        detector.record_tool_call("file_read", {"path": "setup.py"})
        detector.record_tool_call("search_text", {"pattern": "foo"})
        # Pattern starts: SPAWN → READ → SPAWN → READ → SPAWN (3 reps if len=2)
        detector.record_tool_call("task", {"subagent_type": "explore"})
        detector.record_tool_call("file_read", {"path": "result1.txt"})
        detector.record_tool_call("task", {"subagent_type": "explore"})
        detector.record_tool_call("file_read", {"path": "result2.txt"})
        detector.record_tool_call("task", {"subagent_type": "explore"})
        detector.record_tool_call("file_read", {"path": "result3.txt"})
        assert detector.is_tripped
        assert "spawn_subagent" in detector.trip_reason.lower()

    def test_varied_actions_do_not_trip(self):
        """Normal workflow with diverse actions should not trip."""
        detector = MacroLoopDetector(config=MacroLoopDetectorConfig(window_size=6))
        detector.record_tool_call("file_read", {"path": "a.py"})
        detector.record_tool_call("search_text", {"pattern": "foo"})
        detector.record_tool_call("file_write", {"path": "b.py", "content": "x"})
        detector.record_tool_call("bash", {"command": "pytest"})
        detector.record_tool_call("file_read", {"path": "c.py"})
        detector.record_tool_call("file_edit", {"path": "b.py", "old": "x", "new": "y"})
        assert not detector.is_tripped

    def test_reflection_resets_pattern(self):
        """Reflection injections should break repetition patterns."""
        detector = MacroLoopDetector(config=MacroLoopDetectorConfig(
            window_size=6, min_repetitions=2, min_pattern_length=2,
        ))
        detector.record_tool_call("file_read", {"path": "setup.py"})
        detector.record_tool_call("search_text", {"pattern": "foo"})
        detector.record_tool_call("task", {"subagent_type": "explore"})
        detector.record_tool_call("file_read", {"path": "r1.txt"})
        detector.record_tool_call("task", {"subagent_type": "explore"})
        # Inject reflection — should break the pattern
        detector.record_reflection("test_failed")
        detector.record_tool_call("file_read", {"path": "r2.txt"})
        detector.record_tool_call("file_write", {"path": "fix.py"})
        detector.record_tool_call("task", {"subagent_type": "explore"})
        assert not detector.is_tripped


class TestMacroLoopDetectorNoProgress:
    def test_no_progress_cycle_trips(self):
        """Many actions without writes/validates/finishes should trip."""
        detector = MacroLoopDetector(config=MacroLoopDetectorConfig(
            max_no_progress_cycles=2,  # 2 * 4 = 8 macro actions without progress
        ))
        for i in range(10):
            detector.record_tool_call("file_read", {"path": f"file_{i}.py"})
        assert detector.is_tripped
        assert "without progress" in detector.trip_reason.lower()

    def test_write_resets_no_progress(self):
        detector = MacroLoopDetector(config=MacroLoopDetectorConfig(
            max_no_progress_cycles=5,
            window_size=8,
        ))
        # Use TRULY varied tool calls — no repeating pattern
        detector.record_tool_call("file_read", {"path": "file_a.py"})
        detector.record_tool_call("search_text", {"pattern": "foo"})
        detector.record_tool_call("find_files", {"pattern": "*.ts"})
        detector.record_tool_call("file_read", {"path": "file_b.py"})
        detector.record_tool_call("bash", {"command": "pytest"})
        detector.record_tool_call("file_read", {"path": "file_c.py"})
        # Write resets both no_progress and pattern history
        detector.record_tool_call("file_write", {"path": "fix.py", "content": "ok"})
        # After write: few varied actions — no pattern to detect
        detector.record_tool_call("file_read", {"path": "result.py"})
        detector.record_tool_call("search_text", {"pattern": "bar"})
        detector.record_tool_call("find_files", {"pattern": "*.json"})
        assert not detector.is_tripped  # reset counter

    def test_finish_resets_no_progress(self):
        detector = MacroLoopDetector(config=MacroLoopDetectorConfig(
            max_no_progress_cycles=5,
            window_size=8,
        ))
        # Use TRULY varied tool calls — no repeating pattern
        detector.record_tool_call("file_read", {"path": "f1.py"})
        detector.record_tool_call("search_text", {"pattern": "a"})
        detector.record_tool_call("find_files", {"pattern": "b"})
        detector.record_tool_call("file_read", {"path": "f2.py"})
        detector.record_tool_call("bash", {"command": "ls"})
        detector.record_tool_call("file_read", {"path": "f3.py"})
        detector.record_finish()
        # After finish: few varied actions — no pattern to detect
        detector.record_tool_call("file_read", {"path": "g1.py"})
        detector.record_tool_call("search_text", {"pattern": "c"})
        detector.record_tool_call("file_read", {"path": "g2.py"})
        assert not detector.is_tripped


class TestMacroLoopDetectorPatternCounting:
    def test_count_pattern_repetitions_basic(self):
        # Pattern AB repeated 3 times
        sigs = ["a:x", "b:y", "a:x", "b:y", "a:x", "b:y"]
        count = MacroLoopDetector._count_pattern_repetitions(sigs, 2)
        assert count == 3

    def test_count_pattern_repetitions_no_repeat(self):
        sigs = ["a:x", "b:y", "c:z"]
        count = MacroLoopDetector._count_pattern_repetitions(sigs, 2)
        assert count == 0

    def test_count_pattern_repetitions_insufficient(self):
        """When there's only one instance of the pattern (2 sigs, pattern_len=2),
        there's no repetition — the minimum is pattern_len * 2 = 4 entries."""
        sigs = ["a:x", "b:y"]  # only one pattern instance, need 4 for detection
        count = MacroLoopDetector._count_pattern_repetitions(sigs, 2)
        assert count == 0  # not enough for even 1 repetition

    def test_count_pattern_repetitions_pattern_len_3(self):
        sigs = ["a:x", "b:y", "c:z", "a:x", "b:y", "c:z"]
        count = MacroLoopDetector._count_pattern_repetitions(sigs, 3)
        assert count == 2


class TestMacroLoopDetectorReset:
    def test_reset_clears_all(self):
        detector = MacroLoopDetector()
        for _ in range(15):
            detector.record_tool_call("file_read", {"path": "a.py"})
        assert detector.is_tripped
        detector.reset()
        assert not detector.is_tripped
        assert len(detector._history) == 0
        assert detector._no_progress_count == 0


class TestMacroLoopDetectorSerialization:
    def test_to_summary(self):
        detector = MacroLoopDetector()
        detector.record_tool_call("file_read", {"path": "a.py"})
        detector.record_tool_call("search_text", {"pattern": "test"})
        detector.record_tool_call("file_read", {"path": "b.py"})
        s = detector.to_summary()
        assert s["history_len"] == 3
        assert "distinct_files_read" in s
        assert s["trip_reason"] == ""


class TestMacroLoopTrippedException:
    def test_exception_carries_info(self):
        exc = MacroLoopTripped(
            pattern="spawn_subagent → file_read",
            repetitions=3,
            detail="Agent keeps delegating then reading results",
        )
        assert "spawn_subagent" in str(exc)
        assert exc.pattern == "spawn_subagent → file_read"
        assert exc.repetitions == 3
