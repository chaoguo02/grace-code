"""
tests/test_renderer.py

测试 entry/renderer.py 的渲染器：
- RendererBase 接口
- InlineRenderer（Claude Code 风格 TUI）
- create_renderer 工厂函数
- diff 高亮、诊断着色
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from entry.renderer import (
    RendererBase,
    InlineRenderer,
    Renderer,
    create_renderer,
    _highlight_diff,
    format_diagnostic,
)


# ===========================================================================
# RendererBase — 抽象接口
# ===========================================================================

class TestRendererBase:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            RendererBase()

    def test_subclass_must_implement_all_methods(self):
        class Incomplete(RendererBase):
            pass
        with pytest.raises(TypeError):
            Incomplete()


# ===========================================================================
# InlineRenderer — 基本功能
# ===========================================================================

class TestInlineRenderer:
    def setup_method(self):
        self.r = InlineRenderer(model="gpt-4o", mode="plan")

    def test_all_methods_no_crash(self, capsys):
        r = self.r
        r.stream_text("Hello")
        r.stream_thought("thinking...")
        r.on_tool_call(1, "shell", {"cmd": "ls"})
        r.on_observation(1, "shell", "success", "file1\nfile2\n", None)
        r.on_observation(2, "shell", "error", "", "command not found")
        r.on_reflection("test_failed")
        r.on_finish(3, "All tests pass")
        r.on_give_up(4, "Cannot solve")
        r.on_round_end(1, 5, 1000, 2.5)
        r.on_error("something broke")
        r.on_stats(3, 15, 5000)
        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_stream_text_flushes(self, capsys):
        self.r.stream_text("hello")
        captured = capsys.readouterr()
        assert "hello" in captured.out

    def test_stream_thought_dim(self, capsys):
        with patch("entry.renderer._IS_TTY", True):
            self.r.stream_thought("thinking")
        captured = capsys.readouterr()
        assert "thinking" in captured.out

    def test_silent_tools_short_output(self, capsys):
        self.r.on_observation(1, "file_read", "success", "huge file content here...", None)
        captured = capsys.readouterr()
        assert "huge file content" not in captured.out

    def test_non_silent_tools_print_output(self, capsys):
        self.r.on_observation(1, "shell", "success", "output line", None)
        captured = capsys.readouterr()
        assert "output line" in captured.out

    def test_stats_shows_model_and_mode(self, capsys):
        self.r.on_stats(2, 10, 2000)
        captured = capsys.readouterr()
        assert "gpt-4o" in captured.out
        assert "plan" in captured.out

    def test_default_values(self):
        r = InlineRenderer()
        assert r.model == "?"
        assert r.mode == "react"

    def test_on_finish_does_not_repeat_message(self, capsys):
        self.r.stream_text("The")
        self.r.stream_text(" fix")
        self.r.on_finish(3, "The complete fix message")
        captured = capsys.readouterr()
        assert "The complete fix message" not in captured.out

    def test_on_give_up_prints_message(self, capsys):
        self.r.on_give_up(4, "Cannot solve this")
        captured = capsys.readouterr()
        assert "Cannot solve this" in captured.out

    def test_round_end_prints_stats(self, capsys):
        self.r.on_round_end(1, 5, 999, 3.2)
        captured = capsys.readouterr()
        assert "Round 1" in captured.out
        assert "999" in captured.out
        assert "3.2" in captured.out

    def test_round_end_accumulates_totals(self):
        self.r.on_round_end(1, 2, 100, 0.5)
        self.r.on_round_end(2, 3, 200, 1.0)
        assert self.r._total_steps == 5
        assert self.r._total_tokens == 300

    def test_tool_call_params_extract(self, capsys):
        self.r.on_tool_call(1, "shell", {"cmd": "pytest -v"})
        captured = capsys.readouterr()
        assert "shell" in captured.out
        assert "pytest" in captured.out

    def test_tool_call_long_params_truncate(self, capsys):
        long_cmd = "x" * 200
        self.r.on_tool_call(1, "shell", {"cmd": long_cmd})
        captured = capsys.readouterr()
        assert len(captured.out) < 500

    def test_observation_error(self, capsys):
        self.r.on_observation(1, "shell", "error", "", "No such file")
        captured = capsys.readouterr()
        assert "No such file" in captured.out

    def test_observation_diff_highlight(self, capsys):
        diff_text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new"
        self.r.on_observation(1, "git_diff", "success", diff_text, None)
        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_toggle_panels(self):
        assert self.r._panels_collapsed is True
        self.r.toggle_panels()
        assert self.r._panels_collapsed is False
        self.r.toggle_panels()
        assert self.r._panels_collapsed is True

    def test_update_tokens(self):
        self.r.update_tokens(500)
        assert self.r._round_tokens == 500

    def test_reflection(self, capsys):
        self.r.on_reflection("budget_exceeded")
        captured = capsys.readouterr()
        assert "budget_exceeded" in captured.out
        assert "Reflection" in captured.out

    def test_error(self, capsys):
        self.r.on_error("API timeout")
        captured = capsys.readouterr()
        assert "API timeout" in captured.out

    def test_plan_generated(self, capsys):
        self.r.on_plan_generated("### Analysis\nFound bug\n### Changes\nFix it")
        captured = capsys.readouterr()
        assert "Plan" in captured.out
        assert "Found bug" in captured.out

    def test_plan_approved(self, capsys):
        self.r.on_plan_approved()
        captured = capsys.readouterr()
        assert "approved" in captured.out.lower()

    def test_plan_rejected(self, capsys):
        self.r.on_plan_rejected()
        captured = capsys.readouterr()
        assert "rejected" in captured.out.lower()


# ===========================================================================
# Renderer 兼容别名
# ===========================================================================

class TestRendererAlias:
    def test_renderer_is_inline(self):
        assert Renderer is InlineRenderer

    def test_renderer_instantiation(self):
        r = Renderer(model="test", mode="plan")
        assert isinstance(r, InlineRenderer)
        assert isinstance(r, RendererBase)


# ===========================================================================
# create_renderer 工厂函数
# ===========================================================================

class TestCreateRenderer:
    def test_returns_inline_renderer(self):
        r = create_renderer(model="gpt-4", mode="plan")
        assert isinstance(r, InlineRenderer)
        assert r.model == "gpt-4"
        assert r.mode == "plan"

    def test_default_values(self):
        r = create_renderer()
        assert isinstance(r, InlineRenderer)
        assert r.model == "?"
        assert r.mode == "react"


# ===========================================================================
# diff 高亮
# ===========================================================================

class TestHighlightDiff:
    def test_no_crash(self):
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new"
        result = _highlight_diff(diff)
        assert len(result) > 0

    def test_non_diff_text_passes_through(self):
        result = _highlight_diff("not a diff")
        assert "not a diff" in result


# ===========================================================================
# 诊断着色
# ===========================================================================

class TestFormatDiagnostic:
    def test_error_line_colored(self):
        with patch("entry.renderer._IS_TTY", True):
            result = format_diagnostic("ERROR: something failed\nOK line")
            assert "ERROR" in result
            assert "OK line" in result

    def test_warning_line_colored(self):
        with patch("entry.renderer._IS_TTY", True):
            result = format_diagnostic("Warning: deprecated usage\nnormal")
            assert "Warning" in result
            assert "normal" in result

    def test_no_ansi_when_not_tty(self):
        with patch("entry.renderer._IS_TTY", False):
            result = format_diagnostic("Error: bad\nWarning: meh\nfine")
            assert "\033[" not in result


# ===========================================================================
# ChatSession 集成
# ===========================================================================

class TestChatSessionWithRenderer:
    def test_chat_session_accepts_renderer(self, tmp_path):
        from agent.task import Action, ActionType
        from config.schema import AppConfig
        from llm.base import MockBackend
        from tools.base import NoopTool, ToolRegistry
        from entry.chat import ChatSession

        cfg = AppConfig()
        cfg.agent.max_steps = 5
        cfg.agent.budget_tokens = 40_000
        cfg.agent.log_dir = str(tmp_path / "logs")

        registry = ToolRegistry().register(NoopTool("shell"))
        backend = MockBackend([
            Action(ActionType.FINISH, "done", message="ok"),
        ])

        import os
        os.makedirs(cfg.agent.log_dir, exist_ok=True)

        session = ChatSession(
            backend=backend, registry=registry, config=cfg,
            repo_path=str(tmp_path), log_dir=cfg.agent.log_dir,
        )
        assert isinstance(session._renderer, InlineRenderer)
        ok = session.run_round("do something")
        assert ok

    def test_chat_session_with_custom_renderer(self, tmp_path):
        from agent.task import Action, ActionType
        from config.schema import AppConfig
        from llm.base import MockBackend
        from tools.base import NoopTool, ToolRegistry
        from entry.chat import ChatSession

        cfg = AppConfig()
        cfg.agent.max_steps = 5
        cfg.agent.budget_tokens = 40_000
        cfg.agent.log_dir = str(tmp_path / "logs")

        registry = ToolRegistry().register(NoopTool("shell"))
        backend = MockBackend([
            Action(ActionType.FINISH, "done", message="ok"),
        ])

        import os
        os.makedirs(cfg.agent.log_dir, exist_ok=True)

        r = InlineRenderer(model="deepseek-chat", mode="react")
        session = ChatSession(
            backend=backend, registry=registry, config=cfg,
            repo_path=str(tmp_path), log_dir=cfg.agent.log_dir,
            renderer=r,
        )
        assert session._renderer is r
        ok = session.run_round("hello")
        assert ok


# ===========================================================================
# History Viewer
# ===========================================================================

class TestHistoryViewer:
    def test_get_history_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from entry.history_viewer import get_history_dir
        d = get_history_dir()
        assert d.exists()
        assert "forge-agent" in str(d)

    def test_archive_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from entry.history_viewer import archive_log, get_history_dir

        log_file = tmp_path / "test.jsonl"
        log_file.write_text('{"event_type":"task_start","timestamp":"2024-01-01T12:00:00","payload":{"task":{"description":"test"}}}\n')

        result = archive_log(log_file)
        assert result is not None
        assert result.exists()

    def test_list_history_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from entry.history_viewer import list_history
        results = list_history()
        assert results == []

    def test_list_history_with_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from entry.history_viewer import get_history_dir, list_history

        d = get_history_dir()
        log = d / "session_001.jsonl"
        log.write_text(
            '{"event_type":"task_start","timestamp":"2024-01-01T12:00:00","payload":{"task":{"description":"Fix bug","repo_path":"."}}}\n'
            '{"event_type":"action","timestamp":"2024-01-01T12:00:01","payload":{"step":1,"action":{"action_type":"tool_call","tool_call":{"name":"shell","params":{"cmd":"ls"}}}}}\n'
            '{"event_type":"task_complete","timestamp":"2024-01-01T12:00:02","payload":{"summary":"Done"}}\n'
        )
        results = list_history()
        assert len(results) == 1
        assert results[0]["task"] == "Fix bug"
        assert results[0]["status"] == "success"

    def test_render_history_detail(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from entry.history_viewer import render_history_detail

        log = tmp_path / "test.jsonl"
        log.write_text(
            '{"event_type":"task_start","timestamp":"2024-01-01T12:00:00","payload":{"task":{"description":"Fix tests","repo_path":"/project"}}}\n'
            '{"event_type":"action","timestamp":"2024-01-01T12:00:01","payload":{"step":1,"action":{"action_type":"tool_call","thought":"check files","tool_call":{"name":"shell","params":{"cmd":"pytest"}}}}}\n'
            '{"event_type":"observation","timestamp":"2024-01-01T12:00:02","payload":{"step":1,"observation":{"tool_name":"shell","status":"success","output":"1 passed"}}}\n'
            '{"event_type":"task_complete","timestamp":"2024-01-01T12:00:03","payload":{"summary":"All fixed"}}\n'
        )
        output = render_history_detail(log)
        assert "Fix tests" in output
        assert "shell" in output
        assert "COMPLETE" in output

    def test_search_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        from entry.history_viewer import get_history_dir, search_history

        d = get_history_dir()
        log1 = d / "session_001.jsonl"
        log1.write_text(
            '{"event_type":"task_start","timestamp":"2024-01-01T12:00:00","payload":{"task":{"description":"Fix authentication bug","repo_path":"."}}}\n'
            '{"event_type":"task_complete","timestamp":"2024-01-01T12:00:02","payload":{"summary":"Done"}}\n'
        )
        log2 = d / "session_002.jsonl"
        log2.write_text(
            '{"event_type":"task_start","timestamp":"2024-01-02T12:00:00","payload":{"task":{"description":"Add logging","repo_path":"."}}}\n'
            '{"event_type":"task_complete","timestamp":"2024-01-02T12:00:02","payload":{"summary":"Done"}}\n'
        )

        results = search_history("authentication")
        assert len(results) == 1
        assert "authentication" in results[0]["task"]

    def test_render_history_file_not_found(self):
        from entry.history_viewer import render_history_detail
        output = render_history_detail("/nonexistent/file.jsonl")
        assert "not found" in output.lower()
