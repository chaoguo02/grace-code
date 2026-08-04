"""runtime_core/openai_native_backend.py

OpenAINativeBackend -- OpenAI-compatible Native backend using NativeConversation.

Condition 2: Completes the Native pipeline for all providers.  Wraps
OpenAIBackend's SDK client but uses NativeConversation instead of LLMMessage.
This eliminates the _invoke_via_backend -> messages_to_llm -> LLMMessage chain
for OpenAI/DeepSeek/Groq/Ollama providers.

After wiring this into assemble(), message_builder.py, _invoke_via_backend,
message_mapper.py, and old StepLoop become zero-reference dead code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from core.json_values import freeze_json
from llm.base import StreamEvent, StreamEventKind
from runtime_core.model_actions import (
    AssistantText,
    ModelAction,
    ModelFailure,
    ModelRefusal,
    ModelStop,
    ToolCall as MACToolCall,
    ToolCallBatch,
    TokenUsage,
)
from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NativeMessage -> OpenAI API dicts
# ---------------------------------------------------------------------------


def _native_msg_to_openai_dict(msg: NativeMessage) -> dict:
    """Convert NativeMessage to OpenAI API dict.

    Maps:
      - TextBlock -> content string
      - ToolUseBlock -> tool_calls array entry
      - ToolResultBlock -> role="tool" with tool_call_id
    """
    # Single text block -> plain message
    if len(msg.content) == 1 and isinstance(msg.content[0], TextBlock):
        return {"role": msg.role, "content": msg.content[0].text}

    # Multi-block -> ContentBlock dispatch
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []

    for block in msg.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.input, ensure_ascii=False),
                },
            })
        elif isinstance(block, ToolResultBlock):
            tool_results.append(block)

    # Assistant with tool_calls
    if tool_calls:
        return {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else "",
            "tool_calls": tool_calls,
        }

    # Tool result
    if tool_results:
        tr = tool_results[0]
        content = tr.content if isinstance(tr.content, str) else str(tr.content)
        return {
            "role": "tool",
            "tool_call_id": tr.tool_use_id,
            "content": content,
        }

    # Plain message
    return {"role": msg.role, "content": "\n".join(text_parts)}


def _native_conv_to_openai_dicts(conv: NativeConversation) -> list[dict]:
    """Convert NativeConversation to OpenAI API dict list.

    Includes _sanitize_tool_pairs equivalent: strips orphan tool messages
    and unpaired assistant tool_calls.
    """
    result = [_native_msg_to_openai_dict(m) for m in conv.messages]
    return _sanitize_tool_pairs(result)


def _sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
    """Ensure assistant(tool_calls) and tool messages are strictly paired.

    Handles:
      1. Orphan tool messages (assistant lost) -> remove
      2. Assistant tool_calls with all tool responses lost -> strip tool_calls
    """
    result: list[dict] = []
    call_ids: set[str] = set()

    # First pass: collect all tool_call_ids
    for msg in messages:
        if msg.get("role") == "tool":
            call_ids.add(msg.get("tool_call_id", ""))

    # Second pass: keep only paired messages
    for msg in messages:
        if msg.get("role") == "tool":
            if msg.get("tool_call_id") in call_ids:
                result.append(msg)
        elif msg.get("tool_calls"):
            # Check if any tool_call has a matching result
            valid_calls = [
                tc for tc in msg["tool_calls"]
                if tc["id"] in call_ids
            ]
            if valid_calls:
                result.append({**msg, "tool_calls": valid_calls})
            elif msg.get("content"):
                result.append({"role": "assistant", "content": msg["content"]})
        else:
            result.append(msg)

    return result


# ---------------------------------------------------------------------------
# NativeToolSchema for OpenAI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenAINativeToolSchema:
    """OpenAI-compatible tool schema -- Backend cache format."""
    name: str
    description: str = ""
    parameters: dict = field(default_factory=dict)

    def to_api_dict(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# OpenAINativeBackend
# ---------------------------------------------------------------------------


class OpenAINativeBackend:
    """OpenAI-compatible Native Backend -- wraps SDK client, uses NativeConversation.

    Replaces the chain:
      _invoke_via_backend -> messages_to_llm -> LLMMessage
      -> OpenAIBackend.complete() -> _to_openai_messages -> OpenAI API

    With:
      OpenAINativeBackend.invoke(NativeConversation) -> _native_conv_to_openai_dicts -> OpenAI API
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        tool_schemas: tuple[OpenAINativeToolSchema, ...] = (),
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
        use_function_calling: bool = True,
    ):
        try:
            from openai import OpenAI, AsyncOpenAI
            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
            )
            # Phase A: async client — CC callModel() 不阻塞事件循环。
            self._async_client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
            )
        except ImportError:
            raise ImportError(
                "openai package not installed. Run: pip install openai"
            )

        self._model = model
        self._base_url = base_url
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds
        self._use_function_calling = use_function_calling

        # Tool schema cache
        self._tool_schemas: tuple[OpenAINativeToolSchema, ...] = tool_schemas
        self._cached_api_tools: list[dict] = [
            t.to_api_dict() for t in self._tool_schemas
        ]

    @classmethod
    def from_backend(cls, openai_backend, tool_schemas=()) -> "OpenAINativeBackend":
        """Wrap an existing OpenAIBackend as OpenAINativeBackend."""
        instance = object.__new__(cls)
        instance._client = getattr(openai_backend, "_client")
        instance._async_client = getattr(openai_backend, "_async_client", None)
        instance._model = getattr(openai_backend, "_model", "")
        instance._base_url = getattr(openai_backend, "_base_url", None)
        instance._max_tokens = getattr(openai_backend, "_max_tokens", 4096)
        instance._timeout_seconds = getattr(openai_backend, "_timeout_seconds", 60.0)
        instance._use_function_calling = getattr(
            openai_backend, "_use_function_calling", True,
        )
        instance._tool_schemas = tuple(tool_schemas)
        instance._cached_api_tools = [t.to_api_dict() for t in instance._tool_schemas]
        return instance

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def tool_count(self) -> int:
        return len(self._tool_schemas)

    # ── invoke ───────────────────────────────────────────────────────────

    def invoke(
        self,
        conversation: NativeConversation,
        *,
        tool_choice: dict | None = None,
        cancellation: object | None = None,
    ) -> ModelAction:
        """Call OpenAI-compatible LLM -- no tools parameter."""
        # P2: Cancellation check
        if cancellation is not None and hasattr(cancellation, 'cancelled'):
            if cancellation.cancelled:
                return ModelFailure(error="Cancelled before LLM call", retryable=False)

        # NativeConversation -> OpenAI API dicts
        api_messages = _native_conv_to_openai_dicts(conversation)

        logger.debug(
            "OpenAINativeBackend request: model=%s messages=%d tools=%d",
            self._model, len(api_messages), len(self._cached_api_tools),
        )

        if self._use_function_calling and self._cached_api_tools:
            # Function calling path
            kwargs: dict = dict(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=api_messages,
                tools=self._cached_api_tools,
                tool_choice=tool_choice or "auto",
            )
        else:
            kwargs = dict(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=api_messages,
            )

        response = self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        message = choice.message
        content = message.content or ""

        logger.debug(
            "OpenAINativeBackend response: finish=%s input=%d output=%d",
            choice.finish_reason,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )

        usage = TokenUsage(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

        # Parse tool_calls
        if message.tool_calls:
            calls = tuple(
                MACToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    params=freeze_json(
                        json.loads(tc.function.arguments)
                        if tc.function.arguments else {}
                    ),
                    usage=usage if i == 0 else None,
                )
                for i, tc in enumerate(message.tool_calls)
            )
            if len(calls) == 1:
                return calls[0]
            return ToolCallBatch(calls=calls, usage=usage)

        # Finish / stop
        if choice.finish_reason in ("stop", "end_turn"):
            return AssistantText(
                text=content,
                stop_reason=choice.finish_reason,
                usage=usage,
            )

        if choice.finish_reason == "length":
            return ModelStop(
                text=content,
                stop_reason="max_tokens",
                usage=usage,
            )

        # Fallback
        return AssistantText(
            text=content,
            stop_reason=choice.finish_reason or "stop",
            usage=usage,
        )

    # ── Async (CC callModel) ─────────────────────────────────────────────

    async def ainvoke(
        self,
        conversation: NativeConversation,
        *,
        tool_choice: dict | None = None,
        cancellation: object | None = None,
    ) -> ModelAction:
        """CC callModel() 等价 — async 调用, 不阻塞事件循环。"""
        if cancellation is not None and hasattr(cancellation, 'cancelled'):
            if cancellation.cancelled:
                return ModelFailure(error="Cancelled before LLM call", retryable=False)

        api_messages = _native_conv_to_openai_dicts(conversation)

        if self._use_function_calling and self._cached_api_tools:
            kwargs: dict = dict(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=api_messages,
                tools=self._cached_api_tools,
                tool_choice=tool_choice or "auto",
            )
        else:
            kwargs = dict(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=api_messages,
            )

        response = await self._async_client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        message = choice.message
        content = message.content or ""
        usage = TokenUsage(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

        if message.tool_calls:
            calls = tuple(
                MACToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    params=freeze_json(
                        json.loads(tc.function.arguments)
                        if tc.function.arguments else {}
                    ),
                    usage=usage if i == 0 else None,
                )
                for i, tc in enumerate(message.tool_calls)
            )
            if len(calls) == 1:
                return calls[0]
            return ToolCallBatch(calls=calls, usage=usage)

        if choice.finish_reason in ("stop", "end_turn"):
            return AssistantText(
                text=content, stop_reason=choice.finish_reason, usage=usage,
            )
        if choice.finish_reason == "length":
            return ModelStop(
                text=content, stop_reason="max_tokens", usage=usage,
            )
        return AssistantText(
            text=content, stop_reason=choice.finish_reason or "stop", usage=usage,
        )

    async def astream_iter(
        self,
        conversation: NativeConversation,
        *,
        tool_choice: dict | None = None,
    ):
        """CC callModel() async generator — OpenAI SSE 流式 await.

        Same event semantics as Anthropic astream_iter — yields
        TEXT_DELTA / TOOL_USE / FINISH.  Uses AsyncOpenAI stream.
        """
        api_messages = _native_conv_to_openai_dicts(conversation)

        kwargs: dict = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=api_messages,
            stream=True,
        )
        if self._use_function_calling and self._cached_api_tools:
            kwargs["tools"] = self._cached_api_tools
            kwargs["tool_choice"] = tool_choice or "auto"

        try:
            full_text = ""
            tool_calls_raw: list[dict] = []
            yielded_indices: set[int] = set()

            async for chunk in await self._async_client.chat.completions.create(**kwargs):
                if getattr(chunk, "usage", None):
                    pass  # usage on final chunk — handled below
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue
                delta = choice.delta

                # text delta
                if delta.content:
                    full_text += delta.content
                    yield StreamEvent(
                        kind=StreamEventKind.TEXT_DELTA, text=delta.content,
                    )

                # tool call delta (CC-aligned: yield complete blocks)
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        while len(tool_calls_raw) <= idx:
                            tool_calls_raw.append(
                                {"id": "", "name": "", "arguments": ""},
                            )
                        if tc_delta.id:
                            tool_calls_raw[idx]["id"] = tc_delta.id
                        if tc_delta.function.name:
                            tool_calls_raw[idx]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_raw[idx]["arguments"] += tc_delta.function.arguments
                        if idx not in yielded_indices:
                            tc_data = tool_calls_raw[idx]
                            if tc_data["id"] and tc_data["name"]:
                                try:
                                    params = json.loads(tc_data["arguments"])
                                    yielded_indices.add(idx)
                                    yield StreamEvent(
                                        kind=StreamEventKind.TOOL_USE,
                                        tool_call=MACToolCall(
                                            name=tc_data["name"],
                                            params=params,
                                            id=tc_data["id"],
                                        ),
                                    )
                                except json.JSONDecodeError:
                                    pass

            # Final FINISH
            for i, tc in enumerate(tool_calls_raw):
                if i not in yielded_indices and tc["name"]:
                    yield StreamEvent(
                        kind=StreamEventKind.TOOL_USE,
                        tool_call=MACToolCall(
                            name=tc["name"],
                            params={},  # incomplete args — empty params
                            id=tc["id"] or f"tc{i}",
                        ),
                    )
            yield StreamEvent(
                kind=StreamEventKind.FINISH,
                text=full_text,
                finish_message=full_text,
            )
        except Exception as exc:
            yield StreamEvent(kind=StreamEventKind.ERROR, text=str(exc))
