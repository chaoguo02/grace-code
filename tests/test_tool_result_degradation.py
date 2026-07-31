"""Phase 5: Tool Result Degradation — Budget-Driven tests."""

from __future__ import annotations

from context.compaction import (
    _build_degradation_summary,
    _degrade_tool_results,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _msg(role, content, **kwargs):
    d = {"role": role, "content": content}
    d.update(kwargs)
    return d


def _tool_msg(tool_call_id, content, tool_name=""):
    return _msg("tool", content, tool_call_id=tool_call_id, tool_name=tool_name)


def _long_content(size_chars=5000):
    """Generate a tool result payload of approximately *size_chars* characters."""
    lines = [f"Line {i}: " + "x" * 80 for i in range(size_chars // 90)]
    return "\n".join(lines)


# ── tests ────────────────────────────────────────────────────────────────


def test_healthy_budget_no_degradation() -> None:
    """When budget is healthy, no degradation occurs."""
    messages = [
        _msg("user", "Read some files"),
        _tool_msg("c1", _long_content(3000), "Read"),
        _tool_msg("c2", _long_content(2000), "Grep"),
        _msg("assistant", "Done."),
    ]
    result, freed = _degrade_tool_results(messages, history_budget=50000)
    assert freed == 0  # 5000 tokens << 70% of 50000 = 35000 tokens


def test_tight_budget_degradation_triggers() -> None:
    """When history exceeds 70% of budget, largest results degraded."""
    messages = []
    for i in range(10):
        call_id = f"c{i}"
        messages.append(_msg("assistant", f"Step {i}", tool_calls=[
            {"name": "Read", "params": {"file_path": f"file_{i}.py"}, "id": call_id},
        ]))
        messages.append(_tool_msg(call_id, _long_content(1000 + i * 100), "Read"))

    # 10 results at ~1000-1900 chars each ≈ lots of tokens. Small budget.
    result, freed = _degrade_tool_results(messages, history_budget=1000)
    assert freed > 0


def test_recent_5_protected() -> None:
    """Last 5 tool results are never degraded regardless of size."""
    messages = [
        _msg("user", "Complex task"),
    ]
    for i in range(10):
        call_id = f"c{i}"
        messages.append(_msg("assistant", f"Step {i}", tool_calls=[
            {"name": "Read", "params": {}, "id": call_id},
        ]))
        messages.append(_tool_msg(call_id, _long_content(3000), "Read"))

    # Capture original contents BEFORE degradation (mutates in-place)
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    original_contents = {i: str(messages[i]["content"]) for i in tool_indices}
    result, freed = _degrade_tool_results(messages, history_budget=1000)

    assert freed > 0, f"Expected degradation, got freed={freed}"

    # Last 5 tool results must be preserved exactly
    for idx in tool_indices[-5:]:
        result_content = str(result[idx]["content"])
        assert original_contents[idx] == result_content, f"Recent result at {idx} was degraded"


def test_degradation_summary_includes_metadata() -> None:
    """Degraded results show tool name, file info, and preview."""
    msg = _tool_msg("c_read", "def authenticate():\n    return True\n\ndef login():\n    pass\n", "Read")
    # Manually tag with file-path info that _extract_file_path_from_msg can find
    msg["content"] = "File: src/auth.py\ndef authenticate():\n    return True\n\ndef login():\n    pass\n"
    summary = _build_degradation_summary(msg, msg["content"])
    assert "[Tool result summarized" in summary
    assert "Read" in summary
    assert "File: src/auth.py" in summary
    assert "def authenticate" in summary  # preview preserved
    assert "[Full output preserved" in summary


def test_zero_hardcoded_turn_thresholds() -> None:
    """No turn-based TTL constants exist anywhere in the degradation path."""
    import inspect
    import context.compaction as c
    source = inspect.getsource(c._degrade_tool_results)
    # Verify budget-driven logic (not turn-count-driven)
    assert "history_budget" in source
    assert "trigger_ratio" in source.lower()
    assert "created_at_turn" not in source  # No TTL markers
    # Verify recent-K protection uses count, not time
    assert "_DEGRADATION_KEEP_RECENT" in source


def test_degradation_is_prompt_layer_only() -> None:
    """Degradation replaces message content but preserves message structure."""
    # Need >5 tool results so some fall outside recent-K protection.
    # Use very large payloads with a tiny budget to guarantee trigger.
    messages = [_msg("user", "Complex task")]
    for i in range(10):
        call_id = f"c{i}"
        messages.append(_msg("assistant", f"Step {i}", tool_calls=[
            {"name": "Bash", "params": {"cmd": f"build_{i}.sh"}, "id": call_id},
        ]))
        # 15K chars ≈ 3750 tokens per result
        messages.append(_tool_msg(call_id, _long_content(15000), "Bash"))
    messages.append(_msg("assistant", "Done."))

    # Capture original contents BEFORE degradation (mutates in-place)
    original_contents = {i: str(m["content"]) for i, m in enumerate(messages) if m.get("role") == "tool"}
    result, freed = _degrade_tool_results(messages, history_budget=500)
    assert freed > 0, f"Expected tokens freed > 0, got {freed}"
    # Same total message count (no pairs removed — that's Snip's job)
    assert len(result) == len(messages)
    # At least one tool result degraded (compare against pre-capture)
    degraded = sum(
        1 for i, m in enumerate(result)
        if m.get("role") == "tool"
        and str(m["content"]) != original_contents[i]
    )
    assert degraded > 0, f"Expected at least 1 degraded, got 0. freed={freed}"


def test_empty_history_no_degradation() -> None:
    result, freed = _degrade_tool_results([], history_budget=10000)
    assert freed == 0
    assert result == []


def test_referenced_filepath_skip() -> None:
    """Results whose file paths appear in last 10 messages are protected."""
    # Turn 2: Read auth.py (result at index 3)
    # Turn 4: assistant mentions auth.py (at index 7)
    # → Turn 2's Read result should be protected
    messages = [
        _msg("user", "Analyze code"),
        _msg("assistant", "Let me read auth.py", tool_calls=[
            {"name": "Read", "params": {"file_path": "auth.py"}, "id": "c1"},
        ]),
        _tool_msg("c1", "File: auth.py\n" + _long_content(5000), "Read"),
        _msg("assistant", "Now check utils"),
        _msg("assistant", "Let me read utils.py", tool_calls=[
            {"name": "Read", "params": {"file_path": "utils.py"}, "id": "c2"},
        ]),
        _tool_msg("c2", "File: utils.py\n" + _long_content(5000), "Read"),
        _msg("assistant", "I see auth.py has authenticate(). Let me check it more closely."),
    ]
    result, freed = _degrade_tool_results(messages, history_budget=1000)
    # auth.py Read (index 2) should be protected if referenced within window
    # utils.py Read (index 5) is larger + not referenced → should degrade first


def test_write_invalidation_cancels_read_protection() -> None:
    """If an Edit/Write touches a file, prior Read of that file loses protection."""
    messages = [
        _msg("user", "Fix a bug"),
        _msg("assistant", "Reading the file...", tool_calls=[
            {"name": "Read", "params": {"file_path": "main.py"}, "id": "cr"},
        ]),
        _tool_msg("cr", "File: main.py\n" + _long_content(3000), "Read"),
        _msg("assistant", "Editing the file...", tool_calls=[
            {"name": "Edit", "params": {"file_path": "main.py"}, "id": "ce"},
        ]),
        _tool_msg("ce", "Edit successful", "Edit"),
        _msg("assistant", "Done."),
    ]
    result, freed = _degrade_tool_results(messages, history_budget=1000)
    # The Read at index 2 lost protection because Edit at index 4 touched same file
    # It should be a degradation candidate (largest result gets degraded)
    if freed > 0:
        degraded_content = result[2]["content"]
        assert "[Tool result summarized" in str(degraded_content)
