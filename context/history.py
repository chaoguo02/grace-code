"""
context/history.py

对话历史滑动窗口管理。

职责：
- 维护 LLMMessage 列表
- 超过窗口大小时自动从最旧（非首条）开始丢弃
- 与 TokenBudget 协作：先按条数限制，再按 token 限制
- 提供给 core.py 使用的干净接口

设计：
- 第一条消息（任务描述）永不丢弃
- Reflection prompt 和普通 observation 同等对待，都是历史的一部分
- to_dicts() 给 TokenBudget.trim_history() 使用
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agent.task import ToolCall
from llm.base import LLMMessage


class ConversationSnapshotError(ValueError):
    """The live model-input prefix cannot be frozen without changing meaning."""


class SnapshotBoundary(str, Enum):
    """Exact point in the parent loop represented by a snapshot."""

    MODEL_INPUT = "model_input"


class SnapshotMessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolCallSnapshot:
    name: str
    params_json: str
    call_id: str

    @classmethod
    def capture(cls, call: ToolCall) -> "ToolCallSnapshot":
        if not call.id:
            raise ConversationSnapshotError(
                f"Native tool call {call.name!r} has no call id"
            )
        try:
            params_json = json.dumps(
                call.params, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ConversationSnapshotError(
                f"Tool call {call.name!r} parameters are not JSON-safe"
            ) from exc
        return cls(name=call.name, params_json=params_json, call_id=call.id)

    def materialize(self) -> ToolCall:
        return ToolCall(
            name=self.name,
            params=json.loads(self.params_json),
            id=self.call_id,
        )


@dataclass(frozen=True)
class MessageSnapshot:
    role: SnapshotMessageRole
    content_json: str
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCallSnapshot, ...] = ()

    @classmethod
    def capture(cls, message: LLMMessage) -> "MessageSnapshot":
        try:
            role = SnapshotMessageRole(message.role)
        except ValueError as exc:
            raise ConversationSnapshotError(
                f"Unsupported message role {message.role!r}"
            ) from exc
        try:
            content_json = json.dumps(
                message.content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ConversationSnapshotError(
                f"Message content for role {role.value!r} is not JSON-safe"
            ) from exc
        return cls(
            role=role,
            content_json=content_json,
            tool_call_id=message.tool_call_id,
            tool_calls=tuple(
                ToolCallSnapshot.capture(call)
                for call in (message.tool_calls or ())
            ),
        )

    def materialize(self) -> LLMMessage:
        return LLMMessage(
            role=self.role.value,
            content=json.loads(self.content_json),
            tool_call_id=self.tool_call_id,
            tool_calls=(
                [call.materialize() for call in self.tool_calls]
                if self.tool_calls else None
            ),
        )


@dataclass(frozen=True)
class ConversationSnapshot:
    """Immutable, provider-valid copy of one parent model-input prefix."""

    messages: tuple[MessageSnapshot, ...]
    boundary: SnapshotBoundary = SnapshotBoundary.MODEL_INPUT

    @classmethod
    def capture(cls, messages: list[LLMMessage]) -> "ConversationSnapshot":
        snapshot = cls(messages=tuple(MessageSnapshot.capture(m) for m in messages))
        snapshot._validate_native_tool_pairs()
        return snapshot

    def materialize(self) -> list[LLMMessage]:
        """Return new mutable message objects; never expose snapshot internals."""
        return [message.materialize() for message in self.messages]

    @property
    def fingerprint(self) -> str:
        payload = {
            "boundary": self.boundary.value,
            "messages": [
                {
                    "role": message.role.value,
                    "content": message.content_json,
                    "tool_call_id": message.tool_call_id,
                    "tool_calls": [
                        {
                            "name": call.name,
                            "params": call.params_json,
                            "id": call.call_id,
                        }
                        for call in message.tool_calls
                    ],
                }
                for message in self.messages
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _validate_native_tool_pairs(self) -> None:
        pending: set[str] | None = None
        seen_call_ids: set[str] = set()
        for message in self.messages:
            if (
                message.role is not SnapshotMessageRole.ASSISTANT
                and message.tool_calls
            ):
                raise ConversationSnapshotError(
                    "Only assistant messages may contain tool calls"
                )
            if (
                message.role is not SnapshotMessageRole.TOOL
                and message.tool_call_id is not None
            ):
                raise ConversationSnapshotError(
                    "Only tool messages may contain a tool_call_id"
                )
            if message.role is SnapshotMessageRole.TOOL:
                if not message.tool_call_id:
                    raise ConversationSnapshotError(
                        "Tool result has no tool_call_id"
                    )
                if pending is None or message.tool_call_id not in pending:
                    raise ConversationSnapshotError(
                        "Tool result is not paired with the preceding assistant call"
                    )
                pending.remove(message.tool_call_id)
                continue
            if pending:
                raise ConversationSnapshotError(
                    "Assistant tool calls are missing contiguous tool results"
                )
            pending = None
            if message.role is SnapshotMessageRole.ASSISTANT and message.tool_calls:
                ids = [call.call_id for call in message.tool_calls]
                if len(ids) != len(set(ids)) or seen_call_ids.intersection(ids):
                    raise ConversationSnapshotError(
                        "Assistant tool call ids must be unique in the snapshot"
                    )
                seen_call_ids.update(ids)
                pending = set(ids)
        if pending:
            raise ConversationSnapshotError(
                "Snapshot ends before all assistant tool calls have results"
            )


class ConversationHistory:
    """
    对话历史管理器，带滑动窗口。

    用法：
        history = ConversationHistory(max_messages=20)
        history.add(LLMMessage(role="user", content="Fix the bug"))
        history.add(LLMMessage(role="assistant", content="..."))
        msgs = history.to_list()   # 给 LLMBackend 用
    """

    def __init__(self, max_messages: int = 40) -> None:
        """
        Args:
            max_messages: 最多保留的消息条数（含首条任务描述）。
                          实际发给 LLM 的 token 数还会经过 TokenBudget 二次裁剪。
        """
        self._messages: list[LLMMessage] = []
        self._max = max_messages

    def add(self, message: LLMMessage) -> None:
        """添加一条消息，超出窗口时丢弃最旧的非首条消息。"""
        self._messages.append(message)
        self._trim()

    def add_many(self, messages: list[LLMMessage]) -> None:
        """批量添加，添加完成后统一裁剪一次。"""
        self._messages.extend(messages)
        self._trim()

    def to_list(self) -> list[LLMMessage]:
        """返回完整历史列表（浅拷贝）。"""
        return list(self._messages)

    def to_dicts(self) -> list[dict]:
        """转为 dict 列表，供 TokenBudget.trim_history() 使用。"""
        result = []
        for m in self._messages:
            d: dict = {"role": m.role, "content": m.content}
            if m.tool_call_id is not None:
                d["tool_call_id"] = m.tool_call_id
            if m.tool_calls is not None:
                d["tool_calls"] = [tc.to_dict() for tc in m.tool_calls]
            result.append(d)
        return result

    @classmethod
    def from_dicts(cls, dicts: list[dict], max_messages: int = 40) -> "ConversationHistory":
        """从 dict 列表恢复（断点续跑时用）。"""
        h = cls(max_messages=max_messages)
        for d in dicts:
            tool_calls = None
            if "tool_calls" in d:
                tool_calls = [
                    ToolCall(name=tc["name"], params=tc["params"], id=tc.get("id"))
                    for tc in d["tool_calls"]
                ]
            h._messages.append(LLMMessage(
                role=d["role"],
                content=d["content"],
                tool_call_id=d.get("tool_call_id"),
                tool_calls=tool_calls,
            ))
        return h

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def last_message(self) -> LLMMessage | None:
        return self._messages[-1] if self._messages else None

    def get_last_user_message(self) -> str:
        """返回最后一条 user role 消息的 content，不存在时返回空字符串。"""
        for msg in reversed(self._messages):
            if msg.role == "user" and msg.content:
                return msg.content
        return ""

    def clear_except_first(self) -> None:
        """保留首条任务描述，清除其余（紧急重置用）。"""
        if self._messages:
            self._messages = [self._messages[0]]

    def _trim(self) -> None:
        """
        超出 max_messages 时，从索引 1 开始丢弃最旧的消息。
        保证 assistant(tool_calls) + tool(tool_call_id) 配对不被拆散。
        """
        while len(self._messages) > self._max:
            if len(self._messages) <= 1:
                break
            msg = self._messages[1]
            if msg.role == "assistant" and msg.tool_calls:
                # 删除 assistant + 紧跟的所有 tool 回复（保持配对）
                self._messages.pop(1)
                while (len(self._messages) > 1
                       and self._messages[1].role == "tool"):
                    self._messages.pop(1)
            elif msg.role == "tool":
                # 孤立 tool 消息（其 assistant 已丢失），直接删
                self._messages.pop(1)
            else:
                self._messages.pop(1)

    def __len__(self) -> int:
        return len(self._messages)

    def __repr__(self) -> str:
        return f"ConversationHistory(messages={len(self._messages)}, max={self._max})"
