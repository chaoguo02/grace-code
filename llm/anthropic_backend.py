"""
llm/anthropic_backend.py

Anthropic Claude 原生 backend。

消息格式差异（和 OpenAI 对比）：
- system prompt 单独传，不混在 messages 里
- tool 调用结果用 role="user" + content type "tool_result"
- 响应里 tool_use block 和 text block 可以并存
- stop_reason: "tool_use" | "end_turn" | "max_tokens"
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.task import Action, ActionType, ToolCall
from llm.base import LLMBackend, LLMMessage, LLMResponse, LLMToolSchema

logger = logging.getLogger(__name__)


class AnthropicBackend(LLMBackend):
    """
    使用 anthropic SDK 调用 Claude 系列模型。

    支持：
    - tool_use（function calling）
    - 流式（stream=True，当前实现非流式，v2 可扩展）
    - extended thinking（claude-3-7-sonnet 等支持）
    """

    def __init__(self, model: str, api_key: str, max_tokens: int = 4096) -> None:
        try:
            import anthropic as _anthropic
            self._client = _anthropic.Anthropic(api_key=api_key)
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")

        self._model = model
        self._max_tokens = max_tokens

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def max_context_window(self) -> int:
        return 200_000

    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        # 提取 system prompt（Anthropic 单独传）
        system_content = ""
        non_system: list[LLMMessage] = []
        for msg in messages:
            if msg.role == "system":
                system_content = msg.content
            else:
                non_system.append(msg)

        # 转换 messages 格式
        api_messages = _to_anthropic_messages(non_system)

        # 转换 tools 格式
        api_tools = [_to_anthropic_tool(t) for t in tools]

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
        }
        if system_content:
            kwargs["system"] = system_content
        if api_tools:
            kwargs["tools"] = api_tools

        logger.debug(
            "Anthropic request: model=%s messages=%d tools=%d",
            self._model, len(api_messages), len(api_tools),
        )

        response = self._client.messages.create(**kwargs)

        logger.debug(
            "Anthropic response: stop_reason=%s input_tokens=%d output_tokens=%d",
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        action = _parse_anthropic_response(response)

        return LLMResponse(
            action=action,
            raw_content=_extract_text(response),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# 格式转换
# ---------------------------------------------------------------------------

def _to_anthropic_messages(messages: list[LLMMessage]) -> list[dict]:
    """
    把 LLMMessage 列表转为 Anthropic API 的 messages 格式。

    tool_result 消息（工具执行结果）需要特殊处理：
    Anthropic 要求 role=user，content 是 tool_result block 列表。
    我们约定：tool_call_id 非空时，这条消息是 tool_result。
    """
    result = []
    for msg in messages:
        if msg.tool_call_id:
            # 工具执行结果
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }],
            })
        else:
            result.append({"role": msg.role, "content": msg.content})
    return result


def _to_anthropic_tool(schema: LLMToolSchema) -> dict:
    """转换为 Anthropic tool schema 格式。"""
    return {
        "name": schema.name,
        "description": schema.description,
        "input_schema": schema.parameters,
    }


def _extract_text(response: Any) -> str:
    """从响应的 content blocks 中提取所有 text。"""
    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str):
            parts.append(block.text)
    return "\n".join(parts)


def _parse_anthropic_response(response: Any) -> Action:
    """
    把 Anthropic API 响应解析为 Action。

    优先级：
    1. stop_reason == "tool_use" → 找 tool_use block → TOOL_CALL
    2. stop_reason == "end_turn" → FINISH
    3. 其他（max_tokens 等） → GIVE_UP
    """
    # 提取 thought（text block 内容）
    thought = _extract_text(response).strip() or "(no thought)"

    if response.stop_reason == "tool_use":
        # 找第一个 tool_use block
        for block in response.content:
            if block.type == "tool_use":
                return Action(
                    action_type=ActionType.TOOL_CALL,
                    thought=thought,
                    tool_call=ToolCall(
                        name=block.name,
                        params=dict(block.input),
                    ),
                )
        # stop_reason 是 tool_use 但没找到 block（理论上不会发生）
        return Action(
            action_type=ActionType.GIVE_UP,
            thought=thought,
            message="stop_reason=tool_use but no tool_use block found",
        )

    if response.stop_reason == "end_turn":
        # 检查 text 内容是否包含任务完成的意图
        # 简单判断：有文字内容就认为是 FINISH
        if thought and thought != "(no thought)":
            return Action(
                action_type=ActionType.FINISH,
                thought=thought,
                message=thought,
            )
        return Action(
            action_type=ActionType.GIVE_UP,
            thought=thought,
            message="Model ended turn with no content",
        )

    # max_tokens 或其他 stop_reason
    return Action(
        action_type=ActionType.GIVE_UP,
        thought=thought,
        message=f"Unexpected stop_reason: {response.stop_reason}",
    )


# ---------------------------------------------------------------------------
# 流式支持（覆盖 StreamingMixin.stream()）
# ---------------------------------------------------------------------------

from llm.base import StreamingMixin, StreamCallback

# 让 AnthropicBackend 同时继承 StreamingMixin
# Python 不允许事后修改继承，用 monkey-patch 把 stream() 方法加进去

def _anthropic_stream(
    self: "AnthropicBackend",
    messages: list,
    tools: list,
    on_text: StreamCallback | None = None,
) -> LLMResponse:
    """
    Anthropic 流式调用实现。
    用 anthropic SDK 的 stream() context manager，
    边收 text_delta 边调用 on_text 回调实时打印。
    """
    # 提取 system prompt
    system_content = ""
    non_system = []
    for msg in messages:
        if msg.role == "system":
            system_content = msg.content
        else:
            non_system.append(msg)

    api_messages = _to_anthropic_messages(non_system)
    api_tools = [_to_anthropic_tool(t) for t in tools]

    kwargs: dict = {
        "model": self._model,
        "max_tokens": self._max_tokens,
        "messages": api_messages,
    }
    if system_content:
        kwargs["system"] = system_content
    if api_tools:
        kwargs["tools"] = api_tools

    # 使用 stream() context manager
    with self._client.messages.stream(**kwargs) as stream:
        for text_chunk in stream.text_stream:
            if on_text and text_chunk:
                on_text(text_chunk)

        # 流结束后拿最终完整响应
        final = stream.get_final_message()

    action = _parse_anthropic_response(final)
    return LLMResponse(
        action=action,
        raw_content=_extract_text(final),
        input_tokens=final.usage.input_tokens,
        output_tokens=final.usage.output_tokens,
    )

# 把 stream() 方法绑定到 AnthropicBackend
AnthropicBackend.stream = _anthropic_stream