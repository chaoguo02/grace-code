"""
llm/openai_backend.py

OpenAI-compatible backend。覆盖：
- OpenAI (api.openai.com)
- DeepSeek (api.deepseek.com) — deepseek-chat 支持 function calling，R1 不支持
- Groq (api.groq.com)
- Ollama (localhost:11434/v1)

全部用 openai SDK，切换只改 base_url + api_key。

function calling 不支持时（如 DeepSeek R1）走文本解析 fallback：
从 LLM 输出的文本里提取 JSON 格式的 tool call。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.task import Action, ActionType, ToolCall
from llm.base import LLMBackend, LLMMessage, LLMResponse, LLMToolSchema

logger = logging.getLogger(__name__)

# 不支持 function calling 的模型（前缀匹配）
_NO_FUNCTION_CALLING: tuple[str, ...] = (
    "deepseek-reasoner",    # DeepSeek R1
    "deepseek-r1",
)


class OpenAIBackend(LLMBackend):
    """
    OpenAI-compatible API backend。

    Args:
        model:    模型名，如 "gpt-4o", "deepseek-chat", "llama3-70b-8192"
        api_key:  API key
        base_url: API base URL，None 时用 OpenAI 官方地址
        max_tokens: 最大输出 token 数
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=api_key, base_url=base_url)
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

        self._model = model
        self._max_tokens = max_tokens
        self._use_function_calling = not any(
            model.lower().startswith(prefix) for prefix in _NO_FUNCTION_CALLING
        )

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_function_calling(self) -> bool:
        return self._use_function_calling

    @property
    def max_context_window(self) -> int:
        # 常见模型的上下文窗口：gpt-4o=128K, deepseek=128K, groq=128K, ollama=varies
        # 保守默认 128K，子类可按需覆盖
        return 128_000

    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        api_messages = _to_openai_messages(messages)

        logger.debug(
            "OpenAI-compat request: model=%s messages=%d tools=%d fc=%s",
            self._model, len(api_messages), len(tools), self._use_function_calling,
        )

        if self._use_function_calling:
            response = self._complete_with_tools(api_messages, tools)
        else:
            response = self._complete_text_only(api_messages, tools)

        return response

    # ------------------------------------------------------------------
    # function calling 路径
    # ------------------------------------------------------------------

    def _complete_with_tools(
        self,
        api_messages: list[dict],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        api_tools = [_to_openai_tool(t) for t in tools]

        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=api_messages,
            tools=api_tools,
            tool_choice="auto",
        )

        choice = response.choices[0]
        message = choice.message
        thought = message.content or "(no thought)"

        logger.debug(
            "OpenAI-compat response: finish_reason=%s input=%d output=%d",
            choice.finish_reason,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )

        action = _parse_openai_response(choice, thought)

        return LLMResponse(
            action=action,
            raw_content=thought,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

    # ------------------------------------------------------------------
    # 文本解析 fallback（R1 等不支持 function calling 的模型）
    # ------------------------------------------------------------------

    def _complete_text_only(
        self,
        api_messages: list[dict],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        # 在 system prompt 里注入工具描述，要求模型输出 JSON
        tool_desc = _build_tool_description_for_text(tools)
        # 在第一条 system 消息后插入工具说明
        augmented = list(api_messages)
        if augmented and augmented[0]["role"] == "system":
            augmented[0] = {
                "role": "system",
                "content": augmented[0]["content"] + "\n\n" + tool_desc,
            }

        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=augmented,
        )

        choice = response.choices[0]
        raw_text = choice.message.content or ""

        action = _parse_text_response(raw_text)

        return LLMResponse(
            action=action,
            raw_content=raw_text,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )


# ---------------------------------------------------------------------------
# 格式转换
# ---------------------------------------------------------------------------

def _to_openai_messages(messages: list[LLMMessage]) -> list[dict]:
    """
    把 LLMMessage 列表转为 OpenAI messages 格式。

    Native tool calling 模式：
    - assistant + tool_calls → {"role": "assistant", "tool_calls": [...]}
    - role="tool" + tool_call_id → {"role": "tool", "tool_call_id": ..., "content": ...}

    Text fallback 模式：直接传递 role + content。
    """
    result = []
    for msg in messages:
        if msg.tool_calls:
            # Native: assistant message with tool_calls
            d: dict = {
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.params, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            result.append(d)
        elif msg.tool_call_id:
            # Native: tool result
            result.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content if isinstance(msg.content, str) else str(msg.content),
            })
        else:
            result.append({"role": msg.role, "content": msg.content})
    return result


def _to_openai_tool(schema: LLMToolSchema) -> dict:
    """转换为 OpenAI tool schema 格式。"""
    return {
        "type": "function",
        "function": {
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
        },
    }


def _parse_openai_response(choice: Any, thought: str) -> Action:
    """
    解析 OpenAI API 的 choice，返回 Action。
    """
    finish_reason = choice.finish_reason
    message = choice.message

    if finish_reason == "tool_calls" and message.tool_calls:
        # 取第一个 tool call（agent 每轮只调一个工具）
        tc = message.tool_calls[0]
        try:
            params = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            params = {"raw": tc.function.arguments}

        return Action(
            action_type=ActionType.TOOL_CALL,
            thought=thought,
            tool_call=ToolCall(name=tc.function.name, params=params, id=tc.id),
        )

    if finish_reason == "stop":
        if thought and thought != "(no thought)":
            return Action(
                action_type=ActionType.FINISH,
                thought="",      # 普通 chat 模型没有独立推理链，thought 置空
                message=thought,  # 模型输出的内容就是最终回答
            )
        # 空 content + stop → 模型认为任务已完成且无需额外说明
        return Action(
            action_type=ActionType.FINISH,
            thought="",
            message="Task completed.",
        )

    # length（token 超限）或其他
    return Action(
        action_type=ActionType.GIVE_UP,
        thought=thought,
        message=f"Unexpected finish_reason: {finish_reason}",
    )


# ---------------------------------------------------------------------------
# 文本解析 fallback
# ---------------------------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_INLINE_JSON_RE = re.compile(r"\{[^{}]+\}", re.DOTALL)

_FINISH_KEYWORDS = ("task complete", "task is complete", "i have finished", "all done")
_GIVE_UP_KEYWORDS = ("cannot solve", "give up", "unable to", "i cannot")


def _build_tool_description_for_text(tools: list[LLMToolSchema]) -> str:
    """
    给不支持 function calling 的模型注入工具描述。
    要求模型输出特定 JSON 格式：
    {"tool": "tool_name", "params": {...}}
    或者输出 FINISH / GIVE_UP 关键词。
    """
    if not tools:
        return ""

    lines = [
        "## Available tools",
        "To call a tool, output ONLY a JSON block in this exact format:",
        '```json\n{"tool": "<tool_name>", "params": {<params>}}\n```',
        "",
        "To finish the task, output: TASK_COMPLETE: <summary>",
        "To give up, output: GIVE_UP: <reason>",
        "",
        "Tools:",
    ]
    for t in tools:
        lines.append(f"- {t.name}: {t.description}")
    return "\n".join(lines)


def _parse_text_response(text: str) -> Action:
    """
    从纯文本中解析 Action。
    优先匹配 JSON block，其次匹配关键词。
    """
    text_stripped = text.strip()

    # 检查 TASK_COMPLETE
    if text_stripped.upper().startswith("TASK_COMPLETE:"):
        summary = text_stripped[len("TASK_COMPLETE:"):].strip()
        return Action(
            action_type=ActionType.FINISH,
            thought=text_stripped,
            message=summary or "Task complete",
        )

    # 检查 GIVE_UP
    if text_stripped.upper().startswith("GIVE_UP:"):
        reason = text_stripped[len("GIVE_UP:"):].strip()
        return Action(
            action_type=ActionType.GIVE_UP,
            thought=text_stripped,
            message=reason or "Agent gave up",
        )

    # 尝试提取 JSON block（```json ... ```）
    block_match = _JSON_BLOCK_RE.search(text)
    if block_match:
        return _try_parse_tool_json(block_match.group(1), thought=text_stripped)

    # 尝试提取内联 JSON
    for m in _INLINE_JSON_RE.finditer(text):
        action = _try_parse_tool_json(m.group(0), thought=text_stripped)
        if action is not None:
            return action

    # 关键词匹配兜底
    text_lower = text.lower()
    if any(kw in text_lower for kw in _FINISH_KEYWORDS):
        return Action(
            action_type=ActionType.FINISH,
            thought=text_stripped,
            message=text_stripped,
        )
    if any(kw in text_lower for kw in _GIVE_UP_KEYWORDS):
        return Action(
            action_type=ActionType.GIVE_UP,
            thought=text_stripped,
            message=text_stripped,
        )

    # 无法解析，GIVE_UP
    logger.warning("Could not parse action from text: %s", text_stripped[:100])
    return Action(
        action_type=ActionType.GIVE_UP,
        thought=text_stripped,
        message="Could not parse a valid action from model output",
    )


def _try_parse_tool_json(json_str: str, thought: str) -> Action | None:
    """尝试把 JSON 字符串解析为 TOOL_CALL Action，失败返回 None。"""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    tool_name = data.get("tool") or data.get("name") or data.get("function")
    params = data.get("params") or data.get("arguments") or data.get("input") or {}

    if not tool_name or not isinstance(tool_name, str):
        return None

    return Action(
        action_type=ActionType.TOOL_CALL,
        thought=thought,
        tool_call=ToolCall(name=tool_name, params=params if isinstance(params, dict) else {}),
    )


# ---------------------------------------------------------------------------
# 流式支持
# ---------------------------------------------------------------------------

from llm.base import StreamCallback


def _openai_stream(
    self: "OpenAIBackend",
    messages: list,
    tools: list,
    on_text: StreamCallback | None = None,
    on_thought: StreamCallback | None = None,
) -> "LLMResponse":
    """
    OpenAI-compatible 流式调用实现。
    on_text:    最终回答（message）的流式回调
    on_thought: 推理过程（reasoning_content）的流式回调，仅推理模型有内容
    """
    api_messages = _to_openai_messages(messages)

    if self._use_function_calling:
        return _stream_with_tools(self, api_messages, tools, on_text, on_thought)
    else:
        return _stream_text_only(self, api_messages, tools, on_text)


def _stream_with_tools(self, api_messages, tools, on_text, on_thought=None):
    api_tools = [_to_openai_tool(t) for t in tools] if tools else None

    kwargs = dict(
        model=self._model,
        max_tokens=self._max_tokens,
        messages=api_messages,
        stream=True,
    )
    if api_tools:
        kwargs["tools"] = api_tools
        kwargs["tool_choice"] = "auto"

    # 收集流式 chunks
    full_text = ""
    full_reasoning = ""  # reasoning_content（推理模型专有）
    finish_reason = None
    tool_calls_raw = []      # 收集 tool call deltas

    stream = self._client.chat.completions.create(**kwargs)
    for chunk in stream:
        choice = chunk.choices[0] if chunk.choices else None
        if not choice:
            continue

        delta = choice.delta
        finish_reason = choice.finish_reason or finish_reason

        # reasoning_content delta（DeepSeek R1 / Claude thinking）
        reasoning_delta = getattr(delta, "reasoning_content", None)
        if reasoning_delta:
            full_reasoning += reasoning_delta
            if on_thought:
                on_thought(reasoning_delta)

        # text delta（最终回答）
        if delta.content:
            full_text += delta.content
            if on_text:
                on_text(delta.content)

        # tool call delta 拼接
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                while len(tool_calls_raw) <= idx:
                    tool_calls_raw.append({"name": "", "arguments": ""})
                if tc_delta.function.name:
                    tool_calls_raw[idx]["name"] += tc_delta.function.name
                if tc_delta.function.arguments:
                    tool_calls_raw[idx]["arguments"] += tc_delta.function.arguments

    # 构造 mock choice 供 _parse_openai_response 复用
    import json as _json
    from types import SimpleNamespace

    if tool_calls_raw and finish_reason == "tool_calls":
        tcs = []
        for tc in tool_calls_raw:
            try:
                params = _json.loads(tc["arguments"])
            except Exception:
                params = {"raw": tc["arguments"]}
            fn = SimpleNamespace(name=tc["name"], arguments=tc["arguments"])
            tcs.append(SimpleNamespace(function=fn))
        mock_message = SimpleNamespace(content=full_text or None, tool_calls=tcs)
    else:
        mock_message = SimpleNamespace(content=full_text or None, tool_calls=None)

    mock_choice = SimpleNamespace(finish_reason=finish_reason or "stop", message=mock_message)
    # 有 reasoning_content 时，thought = 推理过程，message = 最终回答
    # 没有时（普通 chat 模型），thought 置空，message = 模型输出
    thought_for_parse = full_text or "(no thought)"
    action = _parse_openai_response(mock_choice, thought_for_parse)
    # 如果有推理内容，覆盖 action.thought
    if full_reasoning and action.action_type.value == "finish":
        action = action.__class__(
            action_type=action.action_type,
            thought=full_reasoning,
            tool_call=action.tool_call,
            message=action.message,
        )

    # 流式模式拿不到精确 token 数，估算
    from context.token_budget import estimate_tokens
    input_tokens = sum(estimate_tokens(m.get("content", "")) for m in api_messages)
    output_tokens = estimate_tokens(full_text)

    return LLMResponse(
        action=action,
        raw_content=full_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _stream_text_only(self, api_messages, tools, on_text):
    """R1 等不支持 function calling 的模型的流式路径。"""
    tool_desc = _build_tool_description_for_text(tools)
    augmented = list(api_messages)
    if augmented and augmented[0]["role"] == "system":
        augmented[0] = {
            "role": "system",
            "content": augmented[0]["content"] + "\n\n" + tool_desc,
        }

    full_text = ""
    stream = self._client.chat.completions.create(
        model=self._model,
        max_tokens=self._max_tokens,
        messages=augmented,
        stream=True,
    )
    for chunk in stream:
        choice = chunk.choices[0] if chunk.choices else None
        if not choice:
            continue
        delta = choice.delta
        if delta.content:
            full_text += delta.content
            if on_text:
                on_text(delta.content)

    action = _parse_text_response(full_text)

    from context.token_budget import estimate_tokens
    return LLMResponse(
        action=action,
        raw_content=full_text,
        input_tokens=sum(estimate_tokens(m.get("content", "")) for m in augmented),
        output_tokens=estimate_tokens(full_text),
    )


# 把 stream() 方法绑定到 OpenAIBackend
OpenAIBackend.stream = _openai_stream