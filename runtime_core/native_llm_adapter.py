"""runtime_core/native_llm_adapter.py

NativeBackendAdapter -- lets NativeBackend implement the LLMPort protocol.

This is the boundary adapter between Legacy (dict-based) and Native
(ContentBlock-based) message worlds.  Converts list[dict] -> NativeConversation
at the entry point, then delegates entirely to the Native pipeline.

Phase 7B: Wired into assemble() when the provider is Anthropic.
"""

from __future__ import annotations

from typing import Any

from runtime_core.model_actions import ModelAction
from runtime_core.native_backend import NativeBackend
from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


class NativeBackendAdapter:
    """LLMPort implementation backed by NativeBackend.

    Converts legacy list[dict] messages to NativeConversation at the boundary,
    then delegates to NativeBackend.invoke().

    This is the SINGLE conversion point between Legacy (dict) and Native
    (NativeMessage) worlds.  Everything downstream is pure Native.
    """

    def __init__(self, native_backend: NativeBackend):
        self._backend = native_backend

    def invoke(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> ModelAction:
        """LLMPort.invoke() -- dict boundary -> NativeConversation -> NativeBackend.

        tools parameter is ignored (NativeBackend has tools bound at init).
        """
        conv = _dicts_to_conversation(messages)
        return self._backend.invoke(
            conv,
            tool_choice=tool_choice,
        )

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
    ):
        """LLMPort.stream() -- dict boundary -> NativeConversation -> streaming."""
        conv = _dicts_to_conversation(messages)

        async def _stream():
            from llm.base import StreamEvent, StreamEventKind
            events = list(self._backend.stream_iter(
                conv, tool_choice=tool_choice,
            ))
            for ev in events:
                if ev.kind == StreamEventKind.FINISH:
                    # Return the final ModelAction from the finish event
                    return self._backend.invoke(conv, tool_choice=tool_choice)
            return self._backend.invoke(conv, tool_choice=tool_choice)

        return _stream()


# ---------------------------------------------------------------------------
# dict -> NativeConversation (boundary conversion)
# ---------------------------------------------------------------------------


def _dicts_to_conversation(messages: list[dict]) -> NativeConversation:
    """Convert legacy list[dict] messages to NativeConversation.

    Handles the 5 dict shapes (same as message_mapper._message_from_dict):
      1. {"role": "user", "content": "..."}
      2. {"role": "assistant", "content": "...", "tool_calls": [{id,name,params}]}
      3. {"role": "tool", "tool_call_id": "...", "content": "...", "is_error": ...}
      4. {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": ...}
      5. {"role": "user", "content": [{"type": "text", ...}]}
    """
    native_msgs: list[NativeMessage] = []

    for m in messages:
        if not isinstance(m, dict):
            continue

        # Shape 4: bare tool_result block
        if m.get("type") == "tool_result":
            native_msgs.append(NativeMessage.tool_result(
                tool_use_id=str(m.get("tool_use_id", "") or ""),
                content=str(m.get("content", "") or ""),
                is_error=bool(m.get("is_error", False)),
            ))
            continue

        role = m.get("role", "user") or "user"
        content = m.get("content", "")

        # Shape 2: assistant + tool_calls
        tool_calls = m.get("tool_calls")
        if role == "assistant" and tool_calls:
            tool_uses = tuple(
                ToolUseBlock(
                    id=tc.get("id", "") if isinstance(tc, dict) else "",
                    name=tc.get("name", "") if isinstance(tc, dict) else "",
                    input=tc.get("params", {}) if isinstance(tc, dict) else {},
                )
                for tc in tool_calls
                if isinstance(tc, dict)
            )
            text = content if isinstance(content, str) else ""
            native_msgs.append(
                NativeMessage.assistant_with_tools(text, tool_uses)
            )
            continue

        # Shape 3: role=tool + tool_call_id
        tool_call_id = m.get("tool_call_id")
        if role == "tool" and tool_call_id:
            native_msgs.append(NativeMessage.tool_result(
                tool_use_id=str(tool_call_id),
                content=str(content) if isinstance(content, str) else str(content),
                is_error=bool(m.get("is_error", False)),
            ))
            continue

        # Shape 5: content is list[dict] (ContentBlock)
        if isinstance(content, list):
            native_msgs.append(NativeMessage.from_api_dict(m))
            continue

        # Shape 1: plain text message
        if role == "system":
            native_msgs.append(NativeMessage.system(str(content)))
        elif role == "assistant":
            native_msgs.append(NativeMessage.assistant_text(str(content)))
        else:
            native_msgs.append(NativeMessage.user(str(content)))

    return NativeConversation(messages=tuple(native_msgs))
