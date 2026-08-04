"""Phase 4: ConversationStore — Target 测试。

验证:
- 原子写入 — append_message 后立即可读
- 崩溃恢复 — rebuild_conversation() 从 DB 重建完整状态
- 增量查询 — messages_since(last_event_id)
- is_error 持久化
- tool_use / tool_result 往返保真
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from runtime_core.conversation_store import ConversationStore
from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    ToolUseBlock,
)


@pytest.fixture
def temp_db():
    """创建临时 SQLite 数据库。"""
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "test_conv.db")
    yield db_path
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(temp_db):
    """创建 ConversationStore 实例。"""
    return ConversationStore(temp_db, session_id="s1", run_id="r1")


# ── 原子写入 ──────────────────────────────────────────────────────────────

class TestAtomicWrite:
    def test_append_single_message(self, store):
        msg = NativeMessage.user("hello")
        event_id = store.append_message(msg)
        assert event_id == 1

    def test_append_multiple_messages(self, store):
        store.append_message(NativeMessage.user("hi"))
        store.append_message(NativeMessage.assistant_text("hello"))
        store.append_message(NativeMessage.user("thanks"))
        assert store.last_event_id == 3

    def test_append_tool_use_and_result(self, store):
        """tool_use + tool_result 配对持久化。"""
        # assistant tool_use
        tool_msg = NativeMessage.assistant_with_tools(
            "", (ToolUseBlock(id="c1", name="Read", input={"path": "a.py"}),),
        )
        e1 = store.append_message(tool_msg, turn_index=0)

        # tool_result
        result_msg = NativeMessage.tool_result("c1", "file content", is_error=False)
        e2 = store.append_message(result_msg, turn_index=0, parent_event_id=e1)

        assert e2 == e1 + 1
        assert store.last_event_id == 2

    def test_append_tool_result_convenience(self, store):
        """append_tool_result 便捷方法。"""
        e1 = store.append_tool_result("c1", "output", is_error=False)
        assert e1 == 1


# ── 重建（崩溃恢复） ─────────────────────────────────────────────────────

class TestRebuild:
    def test_rebuild_empty(self, store):
        conv = store.rebuild_conversation()
        assert conv.is_empty
        assert len(conv) == 0

    def test_rebuild_single_message(self, store):
        store.append_message(NativeMessage.user("hello"))
        conv = store.rebuild_conversation()
        assert len(conv) == 1
        assert conv.messages[0].role == "user"
        assert conv.messages[0].text == "hello"

    def test_rebuild_preserves_role(self, store):
        store.append_message(NativeMessage.system("sys"))
        store.append_message(NativeMessage.user("hi"))
        store.append_message(NativeMessage.assistant_text("hello"))

        conv = store.rebuild_conversation()
        assert len(conv) == 3
        assert conv.messages[0].role == "system"
        assert conv.messages[1].role == "user"
        assert conv.messages[2].role == "assistant"

    def test_rebuild_preserves_tool_blocks(self, store):
        """重建后 tool_use 和 tool_result blocks 保真。"""
        tool_msg = NativeMessage.assistant_with_tools(
            "calling",
            (ToolUseBlock(id="call_x", name="Read", input={"path": "test.py"}),),
        )
        e1 = store.append_message(tool_msg)
        store.append_message(
            NativeMessage.tool_result("call_x", "content here", is_error=False),
            parent_event_id=e1,
        )

        conv = store.rebuild_conversation()
        assert len(conv) == 2

        # tool_use 验证
        assert conv.messages[0].has_tool_uses
        assert conv.messages[0].tool_uses[0].id == "call_x"
        assert conv.messages[0].tool_uses[0].name == "Read"

        # tool_result 验证
        assert conv.messages[1].has_tool_results
        assert conv.messages[1].tool_results[0].tool_use_id == "call_x"
        assert conv.messages[1].tool_results[0].is_error is False
        assert "content here" in str(conv.messages[1].tool_results[0].content)

    def test_rebuild_preserves_is_error(self, store):
        """is_error=True 持久化并在重建时恢复。"""
        store.append_message(
            NativeMessage.tool_result("c1", "command failed", is_error=True),
        )
        conv = store.rebuild_conversation()
        assert conv.messages[0].tool_results[0].is_error is True

    def test_crash_recovery_simulation(self, store, temp_db):
        """模拟崩溃：写入后重建新 Store → 恢复完整状态。"""
        store.append_message(NativeMessage.user("task"))
        store.append_message(NativeMessage.assistant_text("working..."))

        # 模拟崩溃：创建新的 Store（同一 session）
        store2 = ConversationStore(temp_db, session_id="s1", run_id="r1")
        conv = store2.rebuild_conversation()
        assert len(conv) == 2
        assert conv.messages[0].text == "task"
        assert conv.messages[1].text == "working..."

    def test_rebuild_after_partial_tool_cycle(self, store, temp_db):
        """模拟 tool_use 已持久化但 tool_result 未持久化 → 可恢复。"""
        tool_msg = NativeMessage.assistant_with_tools(
            "", (ToolUseBlock(id="p1", name="Read", input={"path": "x.py"}),),
        )
        store.append_message(tool_msg, turn_index=0)

        # 模拟崩溃（未写 tool_result）
        store2 = ConversationStore(temp_db, session_id="s1", run_id="r1")
        conv = store2.rebuild_conversation()
        assert len(conv) == 1
        assert conv.messages[0].has_tool_uses
        assert conv.messages[0].tool_uses[0].id == "p1"


# ── 增量查询 ──────────────────────────────────────────────────────────────

class TestIncrementalQuery:
    def test_messages_since(self, store):
        store.append_message(NativeMessage.user("msg1"))
        store.append_message(NativeMessage.assistant_text("msg2"))
        store.append_message(NativeMessage.user("msg3"))

        # 增量查询（只获取 event_id > 1 的）
        new_msgs = store.messages_since(1)
        assert len(new_msgs) == 2
        assert new_msgs[0].text == "msg2"
        assert new_msgs[1].text == "msg3"

    def test_messages_since_none(self, store):
        store.append_message(NativeMessage.user("msg1"))
        new_msgs = store.messages_since(1)
        assert len(new_msgs) == 0

    def test_messages_since_zero(self, store):
        """event_id=0 → 获取所有消息。"""
        store.append_message(NativeMessage.user("m1"))
        store.append_message(NativeMessage.user("m2"))
        assert len(store.messages_since(0)) == 2


# ── 事件 ID 属性 ──────────────────────────────────────────────────────────

class TestEventId:
    def test_last_event_id_empty(self, store):
        assert store.last_event_id == 0

    def test_last_event_id_after_writes(self, store):
        store.append_message(NativeMessage.user("a"))
        store.append_message(NativeMessage.user("b"))
        store.append_message(NativeMessage.user("c"))
        assert store.last_event_id == 3


# ── 多 session 隔离 ───────────────────────────────────────────────────────

class TestMultiSession:
    def test_sessions_isolated(self, temp_db):
        store_a = ConversationStore(temp_db, session_id="sA", run_id="r1")
        store_b = ConversationStore(temp_db, session_id="sB", run_id="r1")

        store_a.append_message(NativeMessage.user("A1"))
        store_b.append_message(NativeMessage.user("B1"))
        store_b.append_message(NativeMessage.user("B2"))

        conv_a = store_a.rebuild_conversation()
        conv_b = store_b.rebuild_conversation()

        assert len(conv_a) == 1
        assert conv_a.messages[0].text == "A1"
        assert len(conv_b) == 2
