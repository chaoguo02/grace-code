"""P1: BufferedConversationStore — Target 测试。"""

import os
import tempfile
import time

import pytest
from runtime_core.conversation_store import ConversationStore
from runtime_core.write_behind_buffer import BufferedConversationStore
from runtime_core.native_message import NativeMessage


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "test_buf.db")
    yield db_path
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def buffered_store(temp_db):
    store = BufferedConversationStore(
        temp_db, session_id="s1", run_id="r1",
        batch_size=5, flush_interval_ms=100,
    )
    yield store
    store.close()


class TestBuffering:
    def test_single_message_buffered_not_flushed(self, buffered_store):
        """单条消息进入缓冲区，未达到阈值不刷盘。"""
        buffered_store.append_message(NativeMessage.user("hi"))
        assert buffered_store.buffered_count == 1
        # DB 中尚未出现（未刷盘）
        conv = buffered_store.rebuild_conversation()
        assert len(conv) == 0

    def test_batch_size_triggers_flush(self, buffered_store):
        """达到 batch_size 时自动刷盘。"""
        for i in range(5):
            buffered_store.append_message(NativeMessage.user(f"msg{i}"))

        assert buffered_store.buffered_count == 0
        conv = buffered_store.rebuild_conversation()
        assert len(conv) == 5

    def test_explicit_flush(self, buffered_store):
        """显式 flush() 立即刷盘。"""
        buffered_store.append_message(NativeMessage.user("a"))
        buffered_store.append_message(NativeMessage.user("b"))
        assert buffered_store.buffered_count == 2

        buffered_store.flush()
        assert buffered_store.buffered_count == 0
        conv = buffered_store.rebuild_conversation()
        assert len(conv) == 2

    def test_append_tool_result(self, buffered_store):
        """append_tool_result 也进入缓冲区。"""
        buffered_store.append_tool_result("c1", "output")
        assert buffered_store.buffered_count == 1
        buffered_store.flush()
        conv = buffered_store.rebuild_conversation()
        assert conv.messages[0].has_tool_results

    def test_flush_returns_count(self, buffered_store):
        buffered_store.append_message(NativeMessage.user("a"))
        buffered_store.append_message(NativeMessage.user("b"))
        n = buffered_store.flush()
        assert n == 2

    def test_multiple_batches(self, buffered_store, temp_db):
        """多批次写入后重建完整。"""
        for i in range(12):
            buffered_store.append_message(NativeMessage.user(f"msg{i}"))
        buffered_store.flush()

        # 新 store 重建
        store2 = ConversationStore(temp_db, session_id="s1", run_id="r1")
        conv = store2.rebuild_conversation()
        assert len(conv) == 12

    def test_close_flushes_remaining(self, temp_db):
        """close() 自动刷盘剩余缓冲区。"""
        store = BufferedConversationStore(
            temp_db, session_id="s1", run_id="r1",
        )
        store.append_message(NativeMessage.user("before close"))
        store.close()

        conv = store.rebuild_conversation()
        assert len(conv) == 1
