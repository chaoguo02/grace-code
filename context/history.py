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

from agent.task import ToolCall
from llm.base import LLMMessage


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

    def clear_except_first(self) -> None:
        """保留首条任务描述，清除其余（紧急重置用）。"""
        if self._messages:
            self._messages = [self._messages[0]]

    def _trim(self) -> None:
        """超出 max_messages 时，从索引 1 开始丢弃最旧的消息。"""
        while len(self._messages) > self._max:
            # 保留 index 0（任务描述），从 index 1 开始丢
            if len(self._messages) > 1:
                self._messages.pop(1)
            else:
                break

    def __len__(self) -> int:
        return len(self._messages)

    def __repr__(self) -> str:
        return f"ConversationHistory(messages={len(self._messages)}, max={self._max})"