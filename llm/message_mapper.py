"""llm/message_mapper.py

消息契约保真映射 —— 唯一 dict→LLMMessage 转换源（对齐 CC List[ContentBlock]）。

G41: DEPRECATED — Legacy path only.
Native 路径不再需要 dict→LLMMessage 翻译。NativeMessage 是 Anthropic Native
Format 的强类型载体，与 LLMMessage 完全解耦。

Native 路径替代方案：
- ConversationState 自动构造协议完整消息（无需手动映射 5 种 dict 形态）
- NativeBackend.invoke(NativeConversation) 直通 Anthropic API（零翻译）

Phase 1: 废除 _invoke_via_backend 的扁平化循环，改为本模块统一转换。
LLMMessage（llm/base.py）已是 CC ContentBlock 的 provider-agnostic 载体：
content: str|list[dict]、tool_calls、tool_call_id、is_error。本模块保证
这些结构化字段从 dict 到 LLMMessage 全程保真，不再被降级为纯文本。

5 种 dict 形态：
  1. user/system        {"role": "user", "content": "..."}
  2. assistant+tool_calls {"role": "assistant", "content": "...", "tool_calls": [{id,name,params}]}
  3. role=tool          {"role": "tool", "tool_call_id": "...", "content": "..."}
  4. 裸 tool_result block {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": ...}
  5. content 为 list[dict] (ContentBlock) {"role": "user", "content": [{"type": "text", ...}]}
"""

from __future__ import annotations

from typing import Any

from core.base import LLMToolSchema
from core.types import ToolCall
from llm.base import LLMMessage


def _as_dict(m: Any) -> dict:
    """Normalize a FrozenJsonObject / dict / dataclass-dict to a plain dict."""
    if isinstance(m, dict):
        return m
    if hasattr(m, "items") and not isinstance(m, str):
        # FrozenJsonObject: items is tuple[(key, value), ...]
        return {k: v for k, v in m.items}
    if hasattr(m, "__dataclass_fields__"):
        return {k: v for k, v in m.items()}
    return {}


def _to_tool_call(tc: dict) -> ToolCall:
    """Convert a tool_calls dict to core.types.ToolCall (params preserved)."""
    tc = _as_dict(tc)
    params = tc.get("params", {})
    if not isinstance(params, dict):
        params = _as_dict(params)
    return ToolCall(
        name=tc.get("name", "") or "",
        params=dict(params or {}),
        id=tc.get("id") or None,
    )


def _message_from_dict(m: dict) -> LLMMessage:
    """Map one message dict to LLMMessage, preserving all structured fields."""
    if not isinstance(m, dict):
        m = _as_dict(m)
        if not m:
            return LLMMessage(role="user", content=str(m))

    # ── 4. 裸 tool_result block（CC 格式，无 role 键）──
    if m.get("type") == "tool_result":
        return LLMMessage(
            role="tool",
            tool_call_id=str(m.get("tool_use_id", "") or ""),
            content=m.get("content", ""),
            is_error=bool(m.get("is_error", False)),
        )

    role = m.get("role", "user") or "user"
    content = m.get("content", "")

    # ── 2. assistant + tool_calls ──
    tool_calls = m.get("tool_calls")
    if role == "assistant" and tool_calls:
        return LLMMessage(
            role="assistant",
            content=content,
            tool_calls=[_to_tool_call(tc) for tc in tool_calls if isinstance(tc, dict)],
        )

    # ── 3. role=tool + tool_call_id ──
    tool_call_id = m.get("tool_call_id")
    if role == "tool" and tool_call_id:
        return LLMMessage(
            role="tool",
            tool_call_id=str(tool_call_id),
            content=content,
            is_error=bool(m.get("is_error", False)),
        )

    # ── 1 / 5. 普通消息（content 可为 str 或 list[dict] ContentBlock）──
    return LLMMessage(role=role, content=content)


def messages_to_llm(messages: Any) -> list[LLMMessage]:
    """Convert a message list (dicts / FrozenJsonObject wrapper) to LLMMessage list.

    Preserves tool_calls / tool_call_id / content blocks — no flattening.
    """
    if messages is None:
        return []
    if hasattr(messages, "get") and not isinstance(messages, (list, tuple)):
        # FrozenJsonObject wrapper: {"messages": [...]} (FrozenJsonObject 非 dict 子类)
        inner = messages.get("messages")
        if isinstance(inner, (list, tuple)):
            messages = inner
    if not isinstance(messages, (list, tuple)):
        return []
    return [_message_from_dict(m) for m in messages]


def tool_dicts_to_schemas(tool_dicts: Any) -> list[LLMToolSchema]:
    """Convert tool schema dicts to LLMToolSchema list (preserving parameters)."""
    if tool_dicts is None:
        return []
    if not isinstance(tool_dicts, (list, tuple)):
        return []
    schemas: list[LLMToolSchema] = []
    for t in tool_dicts:
        if not isinstance(t, dict):
            continue
        name = t.get("name", "") or ""
        if not name:
            continue
        schemas.append(LLMToolSchema(
            name=name,
            description=t.get("description", "") or "",
            parameters=t.get("parameters", {}) or {},
        ))
    return schemas
