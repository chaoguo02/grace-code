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


def test_streamed_finish_outputs_only_rendered_markdown(capsys):
    """streamed answer text is buffered and only rendered markdown is displayed at finish."""
    from entry.renderer import InlineRenderer

    renderer = InlineRenderer()
    renderer.stream_text("# Title\n\n- item")
    assert capsys.readouterr().out == ""

    renderer.on_finish(1, "# Title\n\n- item")
    out = capsys.readouterr().out

    assert "# Title" not in out
    assert "Title" in out
    assert "- item" not in out
    assert "item" in out
    assert "Done" in out
