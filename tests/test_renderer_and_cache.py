"""
tests/test_renderer_and_cache.py

Renderer and cache-stat regression tests.
"""

from __future__ import annotations


def test_cache_hit_rate_includes_non_cached_input_tokens():
    """cache hit rate uses all input tokens as denominator."""
    from llm.base import CacheStats

    stats = CacheStats(
        cache_read_tokens=80,
        cache_creation_tokens=10,
        non_cached_input_tokens=10,
    )

    assert stats.cache_hit_rate == 0.8


def test_file_read_summary_shows_read_range():
    """file_read success output displays path and line range, not just a check mark."""
    from entry.renderer import InlineRenderer

    renderer = InlineRenderer()
    output = "File: entry/cli.py (1200 lines total)\n   1 | line"

    summary = renderer._format_file_read_summary("file_read", output)

    assert "entry/cli.py: lines 1-500 of 1200" in summary


def test_file_view_summary_shows_window_range():
    """file_view success output displays the viewed line window."""
    from entry.renderer import InlineRenderer

    renderer = InlineRenderer()
    output = " 101 | line\n[Lines 101–200 of 1200. Next: file_view path=entry/cli.py start_line=201]"

    summary = renderer._format_file_read_summary("file_view", output)

    assert "lines 101-200 of 1200" in summary


def test_renderer_labels_thought_stream(capsys):
    from entry.renderer import InlineRenderer

    renderer = InlineRenderer()
    renderer.stream_thought("I should inspect files.")
    out = capsys.readouterr().out

    assert "Think" in out
    assert "I should inspect files" in out


def test_renderer_formats_task_tool_call(capsys):
    from entry.renderer import InlineRenderer

    renderer = InlineRenderer()
    renderer.on_tool_call(1, "task", {
        "subagent_type": "general",
        "description": "review task tool",
        "prompt": "Review agent/v2/task_tool.py for bugs",
    })
    out = capsys.readouterr().out

    assert "ToolCall [1] task" in out
    assert "general" in out
    assert "review task tool" in out


def test_renderer_formats_task_notification(capsys):
    from entry.renderer import InlineRenderer

    renderer = InlineRenderer()
    output = """<task-notification>
  <agent-type>general</agent-type>
  <session-id>abc123</session-id>
  <status>completed</status>
  <turns-used>2</turns-used>
  <summary>
Found one issue.
  </summary>
</task-notification>"""
    renderer.on_observation(1, "task", "success", output, None)
    out = capsys.readouterr().out

    assert "Subagent" in out
    assert "general" in out
    assert "completed" in out
    assert "Found one issue" in out


def test_streamed_answer_outputs_immediately_and_finish_does_not_duplicate(capsys):
    """streamed answer text is displayed immediately and finish only prints a footer."""
    from entry.renderer import InlineRenderer

    renderer = InlineRenderer()
    renderer.stream_text("# Title\n\n- item")
    streamed = capsys.readouterr().out

    assert "Answer" in streamed
    assert "# Title" in streamed
    assert "- item" in streamed

    renderer.on_finish(1, "# Title\n\n- item")
    out = capsys.readouterr().out

    assert "# Title" not in out
    assert "- item" not in out
    assert "Finish [1]" in out


def test_token_update_waits_for_action_event_before_redraw(monkeypatch):
    from entry import renderer as renderer_module
    from entry import _terminal

    renderer = renderer_module.InlineRenderer()
    refreshes = []
    monkeypatch.setattr(_terminal, "_IS_TTY", True)
    monkeypatch.setattr(renderer, "_refresh_status", lambda: refreshes.append(True))

    renderer.update_tokens(20_705)

    assert renderer._round_tokens == 20_705
    assert refreshes == []
