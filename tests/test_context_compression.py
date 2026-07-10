from __future__ import annotations

import asyncio

from runtime.context_compression import (
    AutoCompactTrackingState,
    ContentReplacementState,
    apply_tool_result_budget,
    compress_messages,
)


def _tool_result_message(content: str, *, tool_name: str = "read", tool_use_id: str = "t1") -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "content": content,
            }
        ],
    }


def test_apply_tool_result_budget_truncates_large_tool_result():
    messages = [_tool_result_message("x" * 100, tool_name="unknown")]

    compacted, freed = apply_tool_result_budget(messages, max_chars=50, preview_chars=10)

    content = compacted[0]["content"][0]["content"]
    assert content.startswith("x" * 10)
    assert "truncated 90 chars" in content
    assert freed > 0


def test_apply_tool_result_budget_uses_per_tool_limits():
    shell = _tool_result_message("s" * 31_000, tool_name="shell", tool_use_id="shell1")
    search = _tool_result_message("g" * 21_000, tool_name="search_text", tool_use_id="grep1")
    task = _tool_result_message("t" * 80_000, tool_name="task", tool_use_id="task1")

    compacted, _ = apply_tool_result_budget([shell, search, task], preview_chars=100)

    assert "truncated" in compacted[0]["content"][0]["content"]
    assert "truncated" in compacted[1]["content"][0]["content"]
    assert compacted[2]["content"][0]["content"] == "t" * 80_000


def test_apply_tool_result_budget_aggregate_replaces_largest_finite_first():
    messages = [
        _tool_result_message("a" * 90_000, tool_name="find_files", tool_use_id="a"),
        _tool_result_message("b" * 90_000, tool_name="find_files", tool_use_id="b"),
        _tool_result_message("c" * 90_000, tool_name="task", tool_use_id="c"),
    ]

    compacted, freed = apply_tool_result_budget(messages, preview_chars=100, max_total_chars=200_000)

    contents = [msg["content"][0]["content"] for msg in compacted]
    assert any("truncated" in content for content in contents[:2])
    assert contents[2] == "c" * 90_000
    assert freed > 0


def test_apply_tool_result_budget_aggregate_uses_infinite_budget_last():
    messages = [
        _tool_result_message("a" * 190_000, tool_name="task", tool_use_id="task1"),
        _tool_result_message("b" * 190_000, tool_name="file_read", tool_use_id="read1"),
    ]

    compacted, freed = apply_tool_result_budget(messages, preview_chars=100, max_total_chars=200_000)

    contents = [msg["content"][0]["content"] for msg in compacted]
    assert any("truncated" in content for content in contents)
    assert freed > 0


def test_apply_tool_result_budget_reuses_stable_replacement_decision():
    state = ContentReplacementState()
    first = [_tool_result_message("x" * 31_000, tool_name="shell", tool_use_id="stable")]
    compacted_first, _ = apply_tool_result_budget(first, preview_chars=100, replacement_state=state)
    first_content = compacted_first[0]["content"][0]["content"]

    second = [_tool_result_message("y" * 31_000, tool_name="shell", tool_use_id="stable")]
    compacted_second, _ = apply_tool_result_budget(second, preview_chars=100, replacement_state=state)
    second_content = compacted_second[0]["content"][0]["content"]

    assert second_content == first_content


def test_compress_messages_reports_budget_layer():
    async def scenario():
        result = await compress_messages(
            [_tool_result_message("x" * 60_000, tool_name="shell")],
            enable_snip=False,
            enable_microcompact=False,
            enable_collapse=False,
            enable_autocompact=False,
        )

        assert "budget" in result.layers_applied
        assert result.tokens_freed > 0

    asyncio.run(scenario())


def test_compress_messages_reports_blocking_limit():
    async def scenario():
        result = await compress_messages(
            [{"role": "user", "content": "x" * 100}],
            context_window=10,
            enable_budget=False,
            enable_snip=False,
            enable_microcompact=False,
            enable_collapse=False,
            enable_autocompact=False,
        )

        assert "blocking_limit" in result.layers_applied

    asyncio.run(scenario())


def test_autocompact_failure_circuit_breaker_stops_retrying():
    async def scenario():
        tracking = AutoCompactTrackingState()
        calls = 0

        async def fail_summary(_messages):
            nonlocal calls
            calls += 1
            raise RuntimeError("summary failed")

        messages = [{"role": "user", "content": "x" * 120}]
        for _ in range(4):
            await compress_messages(
                messages,
                context_window=40,
                call_model_for_summary=fail_summary,
                autocompact_tracking=tracking,
                enable_budget=False,
                enable_snip=False,
                enable_microcompact=False,
                enable_collapse=False,
                enable_autocompact=True,
            )

        assert tracking.consecutive_failures == 3
        assert calls == 3

    asyncio.run(scenario())
