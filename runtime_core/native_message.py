"""runtime_core/native_message.py

Native 消息原语 — Anthropic Native Format 的强类型载体。

五条架构原则第 1 条：Native 路径必须拥有独立的消息原语，LLMMessage
(str|list[dict]) 被彻底隔离在 Legacy/OpenAI 适配层中。

设计决策：
- 项目目前零使用 anthropic.types.* SDK 类型。NativeMessage 使用项目自有
  frozen dataclass，语义对齐 Anthropic ContentBlock 但保持纯 Python 类型，
  不与 SDK 版本耦合。
- content 始终是 tuple[ContentBlock, ...] — 零 str|list 联合类型。
- 提供 to_api_dict() 直接输出 Anthropic API wire format，绕过 LLMMessage。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ── ContentBlock 类型 ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TextBlock:
    """CC-aligned text content block."""
    type: str = "text"
    text: str = ""
    cache_control: dict | None = None

    def to_api_dict(self) -> dict:
        d: dict = {"type": "text", "text": self.text}
        if self.cache_control is not None:
            d["cache_control"] = self.cache_control
        return d


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """CC-aligned tool_use content block — model requests a tool call."""
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)

    def to_api_dict(self) -> dict:
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """CC-aligned tool_result content block — tool execution result.

    content 可以是纯文本字符串或子 block 列表（对齐 Anthropic 协议：
    tool_result.content 可以是 str 或 ContentBlock 列表）。
    """
    type: str = "tool_result"
    tool_use_id: str = ""
    content: str | tuple[TextBlock, ...] = ""
    is_error: bool = False

    def to_api_dict(self) -> dict:
        if isinstance(self.content, tuple):
            _content = [b.to_api_dict() for b in self.content]
        else:
            _content = self.content
        d: dict = {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": _content,
        }
        if self.is_error:
            d["is_error"] = True
        return d

    @classmethod
    def from_success(cls, tool_use_id: str, output: str) -> "ToolResultBlock":
        """Factory: 成功工具结果。"""
        return cls(tool_use_id=tool_use_id, content=output, is_error=False)

    @classmethod
    def from_error(cls, tool_use_id: str, error: str) -> "ToolResultBlock":
        """Factory: 失败工具结果 — is_error=True。"""
        return cls(tool_use_id=tool_use_id, content=error, is_error=True)

    @classmethod
    def from_denied(cls, tool_use_id: str, reason: str) -> "ToolResultBlock":
        """Factory: 权限拒绝工具结果 — is_error=True。"""
        return cls(
            tool_use_id=tool_use_id,
            content=f"Tool denied: {reason}",
            is_error=True,
        )


# ── ContentBlock 联合类型 ────────────────────────────────────────────────────

ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock
"""Anthropic ContentBlock 语义的联合类型 — 纯 Python，不依赖 SDK 类型。"""


# ── NativeMessage ────────────────────────────────────────────────────────────

NativeRole = Literal["user", "assistant", "system"]


@dataclass(frozen=True, slots=True)
class NativeMessage:
    """Anthropic Native Format 消息 — 全程无 str|list 歧义。

    content 始终是 tuple[ContentBlock, ...]，不再接受裸字符串。
    入口适配层负责将 "hello" 转为 (TextBlock("hello"),)。

    与 LLMMessage 的本质区别：
    - LLMMessage.content: str | list[dict]  → OpenAI 兼容，类型不确定
    - NativeMessage.content: tuple[ContentBlock, ...] → Anthropic Native，类型确定
    """

    role: NativeRole
    content: tuple[ContentBlock, ...] = ()

    # ── Factory methods ──────────────────────────────────────────────────

    @classmethod
    def user(cls, text: str) -> "NativeMessage":
        """构造 user 消息（单 text block）。"""
        return cls(role="user", content=(TextBlock(text=text),))

    @classmethod
    def system(cls, text: str) -> "NativeMessage":
        """构造 system 消息（单 text block）。"""
        return cls(role="system", content=(TextBlock(text=text),))

    @classmethod
    def assistant_text(cls, text: str) -> "NativeMessage":
        """构造 assistant 纯文本消息。"""
        return cls(role="assistant", content=(TextBlock(text=text),))

    @classmethod
    def assistant_with_tools(
        cls,
        text: str,
        tool_uses: tuple[ToolUseBlock, ...],
    ) -> "NativeMessage":
        """构造 assistant 消息 — 含 tool_use blocks。

        CC-aligned: assistant 消息的 content 是 [text, tool_use, tool_use, ...]
        """
        blocks: list[ContentBlock] = []
        if text:
            blocks.append(TextBlock(text=text))
        blocks.extend(tool_uses)
        return cls(role="assistant", content=tuple(blocks))

    @classmethod
    def tool_result(
        cls,
        tool_use_id: str,
        content: str = "",
        is_error: bool = False,
    ) -> "NativeMessage":
        """构造 tool_result 消息 — role="user", content=[ToolResultBlock]。

        Anthropic API 要求 tool_result 以 user role 发送。
        """
        return cls(
            role="user",
            content=(ToolResultBlock(
                tool_use_id=tool_use_id,
                content=content,
                is_error=is_error,
            ),),
        )

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def text(self) -> str:
        """提取所有 text block 的拼接文本。"""
        parts: list[str] = []
        for b in self.content:
            if isinstance(b, TextBlock):
                parts.append(b.text)
        return "\n".join(parts)

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        """提取所有 tool_use blocks。"""
        return tuple(b for b in self.content if isinstance(b, ToolUseBlock))

    @property
    def tool_results(self) -> tuple[ToolResultBlock, ...]:
        """提取所有 tool_result blocks。"""
        return tuple(b for b in self.content if isinstance(b, ToolResultBlock))

    @property
    def has_tool_uses(self) -> bool:
        return any(isinstance(b, ToolUseBlock) for b in self.content)

    @property
    def has_tool_results(self) -> bool:
        return any(isinstance(b, ToolResultBlock) for b in self.content)

    # ── Serialization ────────────────────────────────────────────────────

    def to_api_dict(self) -> dict:
        """直接输出 Anthropic API 格式 dict — 绕过 LLMMessage。

        输出的 content 是 Anthropic SDK 接受的原生格式：
        - str → 纯文本消息
        - list[dict] → ContentBlock 数组
        """
        if not self.content:
            return {"role": self.role, "content": ""}

        # 单 text block → 简化为纯文本（API 优化）
        if len(self.content) == 1 and isinstance(self.content[0], TextBlock):
            return {"role": self.role, "content": self.content[0].to_api_dict()["text"]}

        # 多 block → ContentBlock 数组
        return {
            "role": self.role,
            "content": [b.to_api_dict() for b in self.content],
        }

    @classmethod
    def from_api_dict(cls, d: dict) -> "NativeMessage":
        """从 Anthropic API 响应 dict 构造 NativeMessage。

        支持两种形态：
        - {"role": "user", "content": "text"}  → 单 TextBlock
        - {"role": "assistant", "content": [{...}, ...]}  → 多个 ContentBlock
        """
        role: NativeRole = d.get("role", "user")  # type: ignore[assignment]
        content_raw = d.get("content", "")

        if isinstance(content_raw, str):
            return cls(role=role, content=(TextBlock(text=content_raw),))

        if isinstance(content_raw, list):
            blocks: list[ContentBlock] = []
            for item in content_raw:
                if not isinstance(item, dict):
                    continue
                block_type = item.get("type", "")
                if block_type == "text":
                    blocks.append(TextBlock(
                        text=item.get("text", ""),
                        cache_control=item.get("cache_control"),
                    ))
                elif block_type == "tool_use":
                    blocks.append(ToolUseBlock(
                        id=item.get("id", ""),
                        name=item.get("name", ""),
                        input=item.get("input", {}),
                    ))
                elif block_type == "tool_result":
                    sub_content = item.get("content", "")
                    if isinstance(sub_content, list):
                        sub_blocks = tuple(
                            TextBlock(text=b.get("text", ""))
                            for b in sub_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        sub_content = sub_blocks if sub_blocks else ""
                    blocks.append(ToolResultBlock(
                        tool_use_id=item.get("tool_use_id", ""),
                        content=sub_content,
                        is_error=bool(item.get("is_error", False)),
                    ))
            return cls(role=role, content=tuple(blocks))

        return cls(role=role, content=())


# ── NativeConversation ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NativeConversation:
    """有序 NativeMessage 序列 — 不可变。

    用于在 StepLoop、ConversationState、NativeBackend 之间传递完整对话历史。
    """

    messages: tuple[NativeMessage, ...] = ()

    @classmethod
    def empty(cls) -> "NativeConversation":
        """空对话。"""
        return cls(messages=())

    @classmethod
    def from_messages(cls, messages: list[NativeMessage]) -> "NativeConversation":
        """从可变列表构造。"""
        return cls(messages=tuple(messages))

    def with_message(self, msg: NativeMessage) -> "NativeConversation":
        """追加一条消息，返回新实例（不可变）。"""
        return NativeConversation(messages=(*self.messages, msg))

    def with_messages(self, msgs: tuple[NativeMessage, ...]) -> "NativeConversation":
        """批量追加消息。"""
        return NativeConversation(messages=(*self.messages, *msgs))

    def to_api_format(self) -> list[dict]:
        """转换为 Anthropic API 接受的 dict 列表 — 零 Mapper 翻译。"""
        # 分离 system 消息（Anthropic API 单独传 system prompt）
        result: list[dict] = []
        for msg in self.messages:
            if msg.role == "system":
                continue  # system 单独处理
            result.append(msg.to_api_dict())
        return result

    @property
    def system_messages(self) -> tuple[NativeMessage, ...]:
        """提取所有 system 消息。"""
        return tuple(m for m in self.messages if m.role == "system")

    @property
    def non_system_messages(self) -> tuple[NativeMessage, ...]:
        """提取所有非 system 消息。"""
        return tuple(m for m in self.messages if m.role != "system")

    @property
    def last_message(self) -> NativeMessage | None:
        """最后一条消息，若无则 None。"""
        return self.messages[-1] if self.messages else None

    @property
    def is_empty(self) -> bool:
        return len(self.messages) == 0

    def __len__(self) -> int:
        return len(self.messages)
