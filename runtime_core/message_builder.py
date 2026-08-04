"""runtime_core/message_builder.py

对话消息构造（Phase 2/5）—— 从 model_action + tool_results 生成 CC 规范
dict 消息（assistant tool_use + role=tool tool_result）。

G41: DEPRECATED — Legacy path only.
Native 路径使用 ConversationState（runtime_core/conversation_state.py）自动
保证协议完整性。build_tool_messages() 不再被 NativeStepLoop 调用。

独立模块：避免 step_loop 硬编码 role dict（G16 架构约束：step_loop 不得
内嵌 `{"role": "assistant"...}` 字面量）。
"""

from __future__ import annotations


def _params_to_dict(params) -> dict:
    """Normalize tool-call params (FrozenJsonObject / dict) to plain dict.

    FrozenJsonObject.items 是 tuple 属性（非方法）；普通 dict.items 是方法。
    """
    if params is None:
        return {}
    if isinstance(params, dict):
        return dict(params)
    items = getattr(params, "items", None)
    if isinstance(items, tuple):
        return dict(items)
    if callable(items):
        return dict(items())
    if hasattr(params, "__dataclass_fields__"):
        return dict(params.items())
    return {}


def assistant_text_message(text: str) -> dict:
    """构造 assistant 纯文本消息（规范 dict）。"""
    return {"role": "assistant", "content": text}


def build_tool_messages(calls, tool_results) -> list[dict]:
    """生成 assistant(tool_calls) + 每个 tool 结果的 role=tool 消息。

    对齐 CC List[ContentBlock]：tool_use_id 与 tool_call.id 一一对应，
    Anthropic 服务端才能校验配对。
    """
    msgs: list[dict] = []
    msgs.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tc.id,
                "name": tc.name,
                "params": _params_to_dict(tc.params),
            }
            for tc in calls
        ],
    })
    for tr in tool_results:
        block = (
            tr.outcome.to_chat_block()
            if tr.outcome is not None and hasattr(tr.outcome, "to_chat_block")
            else None
        )
        msgs.append({
            "role": "tool",
            "tool_call_id": tr.tool_call.id,
            "content": block.get("content", "") if block else (tr.hook_deny_reason or ""),
            "is_error": bool(block.get("is_error", False)) if block else (not tr.hook_allowed),
        })
    return msgs
