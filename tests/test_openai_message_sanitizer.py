from llm.openai_backend import _sanitize_tool_pairs


def _assistant(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "file_read", "arguments": "{}"},
        }],
    }


def _tool(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": "ok"}


def test_sanitizer_removes_tool_response_separated_from_its_assistant():
    messages = [
        _assistant("call-1"),
        {"role": "user", "content": "revision"},
        _tool("call-1"),
    ]

    sanitized = _sanitize_tool_pairs(messages)

    assert sanitized == [
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "revision"},
    ]


def test_sanitizer_keeps_only_complete_contiguous_tool_pairs():
    messages = [
        _assistant("call-1"),
        _tool("call-1"),
        {"role": "user", "content": "continue"},
    ]

    assert _sanitize_tool_pairs(messages) == messages
