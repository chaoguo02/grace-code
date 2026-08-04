"""runtime_core/conversation_store.py

事件溯源式会话持久化 — DB 是唯一事实来源。

五条架构原则第 4 条：数据库应是唯一事实来源，内存只是投影。
每个 content block 的生成是一个原子事件，先落库，再更新内存视图。
恢复会话时，从数据库重建内存状态，而非反过来。

与 Legacy SessionStore 的关系：
- ConversationStore 直接写入 session_messages 表（使用 content_json + 新增列）
- Legacy append_message(LLMMessage) 继续工作（读路径兼容）
- 两者共享同一张表，不同路径写入不同的列子集
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Serialization ────────────────────────────────────────────────────────────


def _native_message_to_content_json(msg: NativeMessage) -> str:
    """将 NativeMessage.content 序列化为 JSON（ContentBlock 数组）。

    复用现有 message_serializer.py 的 JSON 格式：
    每个 ContentBlock 转为 {"type": "...", ...} dict。
    """
    blocks = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            d: dict = {"type": "text", "text": block.text}
            if block.cache_control is not None:
                d["cache_control"] = block.cache_control
            blocks.append(d)
        elif isinstance(block, ToolUseBlock):
            blocks.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif isinstance(block, ToolResultBlock):
            blocks.append(block.to_api_dict())
    return json.dumps(blocks, ensure_ascii=False)


def _content_json_to_native_message(
    role: str,
    content_json: str,
    tool_call_id: str = "",
    is_error: bool = False,
    created_at: str = "",
) -> NativeMessage:
    """从 content_json 反序列化为 NativeMessage。"""
    try:
        blocks_raw = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return NativeMessage(role="user", content=())

    if not isinstance(blocks_raw, list):
        return NativeMessage(role="user", content=())

    from runtime_core.native_message import NativeMessage as NM
    return NM.from_api_dict({
        "role": role,
        "content": blocks_raw,
    })


# ── ConversationStore ────────────────────────────────────────────────────────


class ConversationStore:
    """事件溯源式会话持久化。

    核心原则: DB 是唯一事实来源，内存只是投影。
    每个 NativeMessage 的生成先 INSERT 并 COMMIT，再更新 ConversationState。

    使用现有的 session_messages 表。Native 路径使用 content_json 列
    存储 ContentBlock JSON 数组，与 Legacy 路径的 content_json 格式兼容。
    """

    def __init__(self, db_path: str, session_id: str, run_id: str = ""):
        self._db_path = db_path
        self._session_id = session_id
        self._run_id = run_id
        self._event_seq = 0  # 当前 session 的事件序号

        # 确保 DB 文件存在 + 迁移已应用
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """确保 session_messages 表存在并已迁移新列。"""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            # 创建基础表（如果不存在）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    content_json TEXT,
                    message_kind TEXT,
                    tool_call_id TEXT,
                    tool_name TEXT,
                    tool_calls_json TEXT,
                    turn_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT ''
                )
            """)

            # 新增 Native 路径需要的列（幂等）
            self._add_column_if_missing(conn, "session_messages", "is_error", "INTEGER DEFAULT 0")
            self._add_column_if_missing(conn, "session_messages", "event_id", "INTEGER")
            self._add_column_if_missing(conn, "session_messages", "run_id", "TEXT")
            self._add_column_if_missing(conn, "session_messages", "block_type", "TEXT")
            self._add_column_if_missing(conn, "session_messages", "parent_event_id", "INTEGER")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _add_column_if_missing(conn, table: str, column: str, col_type: str) -> None:
        """幂等添加列。"""
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    # ── Write path（事件溯源 — 即时 COMMIT） ────────────────────────────

    def append_message(
        self,
        message: NativeMessage,
        turn_index: int = 0,
        parent_event_id: int | None = None,
    ) -> int:
        """原子写入一个 NativeMessage → 返回 event_id。

        每个消息是一个原子事件。INSERT 后立即 COMMIT — 不等待 Run 结束。
        崩溃恢复时，从最后一个完整 event 重建。

        Args:
            message: 要持久化的 NativeMessage
            turn_index: 当前 turn 序号
            parent_event_id: tool_result 的父 event（tool_use 的 event_id）

        Returns:
            event_id — 自增事件序号
        """
        self._event_seq += 1
        event_id = self._event_seq

        content_json_str = _native_message_to_content_json(message)
        content_text = message.text or ""

        # 推断 block_type
        block_type = _infer_block_type(message)
        is_error = _extract_is_error(message)

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                INSERT INTO session_messages (
                    session_id, role, content, content_json,
                    tool_call_id, tool_name,
                    is_error, event_id, run_id, block_type,
                    parent_event_id, turn_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._session_id,
                    message.role,
                    content_text,
                    content_json_str,
                    _extract_tool_call_id(message),
                    _extract_tool_name(message),
                    int(is_error),
                    event_id,
                    self._run_id,
                    block_type,
                    parent_event_id,
                    str(turn_index),
                    _utc_now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        logger.debug(
            "ConversationStore: appended event_id=%d role=%s block_type=%s",
            event_id, message.role, block_type,
        )
        return event_id

    def append_tool_result(
        self,
        tool_use_id: str,
        content: str,
        is_error: bool = False,
        turn_index: int = 0,
        parent_event_id: int | None = None,
    ) -> int:
        """便捷方法：直接写 tool_result block。"""
        msg = NativeMessage.tool_result(tool_use_id, content, is_error=is_error)
        return self.append_message(msg, turn_index=turn_index,
                                   parent_event_id=parent_event_id)

    # ── Read path（从 DB 重建状态） ─────────────────────────────────────

    def rebuild_conversation(self) -> NativeConversation:
        """从 DB 重建完整 NativeConversation。

        用于：会话恢复、断点续传、跨轮上下文。
        按 event_id 排序，保证消息顺序。
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT role, content_json, tool_call_id,
                       is_error, event_id, block_type, created_at
                FROM session_messages
                WHERE session_id = ?
                ORDER BY event_id, id
                """,
                (self._session_id,),
            ).fetchall()
        finally:
            conn.close()

        messages: list[NativeMessage] = []
        for row in rows:
            msg = _row_to_native_message(row)
            if msg is not None:
                messages.append(msg)

        return NativeConversation(messages=tuple(messages))

    def messages_since(self, last_event_id: int) -> list[NativeMessage]:
        """增量查询 — 获取 last_event_id 之后的新消息。

        用于：多轮对话的增量上下文加载。
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT role, content_json, tool_call_id,
                       is_error, event_id, block_type, created_at
                FROM session_messages
                WHERE session_id = ? AND event_id > ?
                ORDER BY event_id, id
                """,
                (self._session_id, last_event_id),
            ).fetchall()
        finally:
            conn.close()

        return [
            msg for msg in (
                _row_to_native_message(row) for row in rows
            ) if msg is not None
        ]

    @property
    def last_event_id(self) -> int:
        """当前 session 的最后一个 event_id（0 表示无消息）。"""
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT MAX(event_id) FROM session_messages WHERE session_id = ?",
                (self._session_id,),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _infer_block_type(msg: NativeMessage) -> str:
    """推断消息的主要 block 类型。"""
    for block in msg.content:
        if isinstance(block, ToolUseBlock):
            return "tool_use"
        if isinstance(block, ToolResultBlock):
            return "tool_result"
    return "text"


def _extract_is_error(msg: NativeMessage) -> bool:
    """提取 is_error 标志。"""
    for block in msg.content:
        if isinstance(block, ToolResultBlock) and block.is_error:
            return True
    return False


def _extract_tool_call_id(msg: NativeMessage) -> str:
    """提取 tool_use_id（用于 tool_result 消息）。"""
    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            return block.tool_use_id
    return ""


def _extract_tool_name(msg: NativeMessage) -> str:
    """提取工具名称（用于索引）。"""
    names = []
    for block in msg.content:
        if isinstance(block, ToolUseBlock):
            names.append(block.name)
    return ",".join(names) if names else ""


def _row_to_native_message(row: sqlite3.Row) -> NativeMessage | None:
    """将 DB 行转为 NativeMessage。"""
    content_json = row["content_json"] if "content_json" in row.keys() else None
    if content_json:
        return _content_json_to_native_message(
            role=row["role"],
            content_json=content_json,
            tool_call_id=row["tool_call_id"] or "",
            is_error=bool(row["is_error"]) if "is_error" in row.keys() else False,
            created_at=row["created_at"] or "",
        )
    # 旧数据：content 列作为 text
    content_text = row["content"] or ""
    return NativeMessage(role=row["role"] or "user",
                         content=(TextBlock(text=content_text),))
