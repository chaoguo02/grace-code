"""runtime_core/write_behind_buffer.py

写入缓冲层 — 批量异步刷盘，消除每消息 COMMIT 的 IO 瓶颈。

CC 对齐：CC 的持久化是批量+异步的。它在内存中维护一个 write-behind buffer，
按固定时间窗口（100ms）或数量阈值（5 条）批量刷盘。

事实来源的唯一性 ≠ 每次写入都同步刷盘。
崩溃恢复粒度：最多丢失最近 100ms 的写入（可接受的权衡）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from runtime_core.conversation_store import ConversationStore
from runtime_core.native_message import NativeMessage

logger = __import__("logging").getLogger(__name__)


@dataclass
class _BufferedEntry:
    message: NativeMessage
    turn_index: int = 0
    parent_event_id: int | None = None


class BufferedConversationStore:
    """带写入缓冲的 ConversationStore 包装器。

    使用方式（与 ConversationStore 相同接口）:
        store = BufferedConversationStore(db_path, session_id, run_id)
        store.append_message(msg)       # 进入缓冲区，不立即 COMMIT
        store.flush()                   # 显式刷盘（关键边界点）
        store.append_message(msg)       # 继续缓冲

    自动刷盘触发条件：
    - 缓冲区达到 batch_size 条消息
    - 距上次刷盘超过 flush_interval_ms 毫秒
    - 显式调用 flush()

    线程安全：所有写入操作持有锁。
    """

    def __init__(
        self,
        db_path: str,
        session_id: str,
        run_id: str = "",
        *,
        batch_size: int = 5,
        flush_interval_ms: int = 100,
    ):
        self._inner = ConversationStore(db_path, session_id, run_id)
        self._batch_size = batch_size
        self._flush_interval_ms = flush_interval_ms

        self._buffer: list[_BufferedEntry] = []
        self._lock = threading.Lock()
        self._last_flush_time = time.monotonic()
        self._flushed_count = 0
        self._buffered_count = 0

        # 后台定时刷盘线程（daemon — 进程退出时自动终止）
        self._closed = False
        self._timer: threading.Timer | None = None
        self._start_timer()

    # ── Public API (same as ConversationStore) ──────────────────────────

    def append_message(
        self,
        message: NativeMessage,
        turn_index: int = 0,
        parent_event_id: int | None = None,
    ) -> int:
        """写入缓冲区。返回预估 event_id（flushed_count + buffer 位置）。"""
        with self._lock:
            entry = _BufferedEntry(
                message=message,
                turn_index=turn_index,
                parent_event_id=parent_event_id,
            )
            self._buffer.append(entry)
            self._buffered_count += 1
            estimated_id = self._flushed_count + len(self._buffer)

            # 达到批量阈值 → 立即刷盘
            if len(self._buffer) >= self._batch_size:
                self._flush_locked()

            return estimated_id

    def append_tool_result(
        self,
        tool_use_id: str,
        content: str,
        is_error: bool = False,
        turn_index: int = 0,
        parent_event_id: int | None = None,
    ) -> int:
        """写入 tool_result 到缓冲区。"""
        msg = NativeMessage.tool_result(tool_use_id, content, is_error=is_error)
        return self.append_message(
            msg, turn_index=turn_index, parent_event_id=parent_event_id,
        )

    def flush(self) -> int:
        """显式刷盘 — 将所有缓冲消息写入 DB。

        应在以下关键边界调用：
        - tool_use → tool_result 配对完成后（保证协议完整性可恢复）
        - Run 结束前
        - 上下文压缩后

        Returns:
            写入的消息数量。
        """
        with self._lock:
            return self._flush_locked()

    # ── Read path (delegated to inner store — reads from committed DB) ──

    def rebuild_conversation(self):
        """从 DB 重建 — 包含已刷盘的消息，不包含缓冲区中的消息。"""
        return self._inner.rebuild_conversation()

    def messages_since(self, last_event_id: int) -> list[NativeMessage]:
        return self._inner.messages_since(last_event_id)

    @property
    def last_event_id(self) -> int:
        return self._inner.last_event_id

    @property
    def buffered_count(self) -> int:
        """缓冲区中尚未刷盘的消息数。"""
        with self._lock:
            return len(self._buffer)

    @property
    def flushed_count(self) -> int:
        """已刷盘的消息总数。"""
        return self._flushed_count

    # ── Lifecycle ───────────────────────────────────────────────────────

    def close(self) -> None:
        """关闭 — 刷盘所有缓冲消息并停止定时器。"""
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
        self.flush()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ── Internal ────────────────────────────────────────────────────────

    def _flush_locked(self) -> int:
        """在持有锁的情况下刷盘。返回写入数量。"""
        if not self._buffer:
            return 0

        count = 0
        for entry in self._buffer:
            self._inner.append_message(
                entry.message,
                turn_index=entry.turn_index,
                parent_event_id=entry.parent_event_id,
            )
            count += 1

        self._flushed_count += count
        self._buffer.clear()
        self._last_flush_time = time.monotonic()
        return count

    def _start_timer(self) -> None:
        """启动后台定时刷盘。"""
        interval_s = self._flush_interval_ms / 1000.0

        def _timer_callback():
            if self._closed:
                return
            with self._lock:
                if self._buffer:
                    elapsed = time.monotonic() - self._last_flush_time
                    if elapsed >= interval_s:
                        self._flush_locked()
            # 重新调度
            if not self._closed:
                self._timer = threading.Timer(interval_s, _timer_callback)
                self._timer.daemon = True
                self._timer.start()

        self._timer = threading.Timer(interval_s, _timer_callback)
        self._timer.daemon = True
        self._timer.start()
