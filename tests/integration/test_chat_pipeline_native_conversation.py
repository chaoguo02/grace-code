"""Phase 3: ChatPipeline Native 会话构建富列 — Target 测试。

对齐 CC List[ContentBlock]：_execute_native 重建 conversation 时，从
session_service.get_messages(limit=...) 拿到的历史消息必须保留
tool_calls / tool_call_id（不再因 limit TypeError 被吞成空）。
"""

from __future__ import annotations

import pytest

from server.services.chat_pipeline import (
    ChatPipeline, ChatPipelinePorts, PreparedChatRun,
)
from server.schemas.session import ChatRequest


class _RecordingCoordinator:
    def __init__(self):
        self.executed_conv = None
        self.caps = None
        self.finalized = False

    def execute(self, cmd, *, conversation=None, capabilities=None, max_steps=25, workspace=""):
        self.executed_conv = conversation
        self.caps = capabilities
        from runtime_core.outcome import RuntimeOutcome
        return RuntimeOutcome.completed(
            run_id=cmd.run_id, steps=1, tokens=10, summary="done",
        )

    async def aexecute(self, cmd, *, conversation=None, capabilities=None, max_steps=25, workspace="", event_handler=None):
        """Phase F: async execute (aiterate)."""
        self.executed_conv = conversation
        self.caps = capabilities
        from runtime_core.outcome import RuntimeOutcome
        outcome = RuntimeOutcome.completed(
            run_id=cmd.run_id, steps=1, tokens=10, summary="done",
        )
        if event_handler:
            event_handler({"type": "completed", "outcome": outcome})
        return outcome

    def finalize(self, cmd, *, session_id=None):
        self.finalized = True
        return None


def _rich_session_service():
    return type("S", (), {"get_messages": lambda self, sid, limit=None: [
        {"role": "assistant", "content": "thinking",
         "tool_calls": [{"id": "c1", "name": "Read", "params": {"path": "a.py"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "file content",
         "is_error": False},
    ]})()


def _make_pipeline(coordinator, session_service):
    ports = ChatPipelinePorts(
        runtime=None,
        session_service=session_service,
        backend=None, config={}, effective_llm_config={},
        repo_path="/tmp",
        build_confirm_callback=lambda x: lambda: None,
        reload_rules=lambda: None, loaded_rules=lambda: [],
        accumulate_session_stats=lambda s, r: None,
        compact_session_async=lambda s: None,
        coordinator=coordinator,
    )
    return ChatPipeline(ports)


def test_execute_native_preserves_rich_conversation():
    """历史消息的 tool_calls/tool_call_id 传入 native conversation。"""
    coord = _RecordingCoordinator()
    pipeline = _make_pipeline(coord, _rich_session_service())

    request = ChatRequest(prompt="do it", agent_name="build")
    object.__setattr__(request, "session_id", "s-native")
    object.__setattr__(request, "display_prompt", "do it")
    prepared = PreparedChatRun(request=request, resolved_prompt="do it")

    result = pipeline.execute(prepared)

    assert result.status in ("success", "blocked", "failed")
    assert coord.executed_conv is not None, "coordinator 必须收到 conversation"
    msgs = coord.executed_conv.messages
    # 历史消息（非当前 prompt）含 tool_calls / tool_call_id
    hist = [m for m in msgs if m.get("content") != "do it"]
    assert any(m.get("tool_calls") for m in hist), (
        f"历史 assistant 消息必须保留 tool_calls，got {msgs}"
    )
    assert any(m.get("tool_call_id") for m in hist), (
        f"历史 tool 消息必须保留 tool_call_id，got {msgs}"
    )


def test_execute_native_conversation_not_empty():
    """conversation 非空（修复 get_messages limit TypeError 被吞的问题）。"""
    coord = _RecordingCoordinator()
    # get_messages 无 limit 参数但被调用 limit=50 → 修复后不再抛 TypeError
    pipeline = _make_pipeline(coord, _rich_session_service())

    request = ChatRequest(prompt="hi", agent_name="build")
    object.__setattr__(request, "session_id", "s-native2")
    object.__setattr__(request, "display_prompt", "hi")
    prepared = PreparedChatRun(request=request, resolved_prompt="hi")

    pipeline.execute(prepared)

    msgs = coord.executed_conv.messages
    assert len(msgs) >= 1, "conversation 不应为空（含当前 user prompt）"
