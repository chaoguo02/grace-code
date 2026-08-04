"""runtime_core/native_backend.py

NativeBackend -- Tools bound at init, NativeMessage-native input.

Principle 3: Tool definitions should be bound to the Backend instance,
not passed per-invoke. NativeBackend caches validated schemas at init.
Invoke has zero translation chain (NativeMessage -> Anthropic API direct,
no LLMMessage intermediary).

Relationship with Legacy AnthropicBackend:
- NativeBackend is the clean entry point for the Native path
- AnthropicBackend (llm/anthropic_backend.py) continues serving Legacy path
- Both share the Anthropic SDK client but use different message types
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.json_values import freeze_json
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
# NativeToolSchema -- Validated native tool schema (Backend cache format)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NativeToolSchema:
    """Validated native tool schema -- Backend internal cache format."""
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    def to_api_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# ---------------------------------------------------------------------------
# NativeResponse -- Structured response with ContentBlock tuple
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NativeResponse:
    """Structured response from NativeBackend -- contains ContentBlock tuple."""
    content: tuple  # tuple[TextBlock | ToolUseBlock, ...]
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def text(self) -> str:
        parts: list[str] = []
        for b in self.content:
            if isinstance(b, TextBlock):
                parts.append(b.text)
        return "\n".join(parts)

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.content if isinstance(b, ToolUseBlock))

    @property
    def has_tool_uses(self) -> bool:
        return any(isinstance(b, ToolUseBlock) for b in self.content)

    def to_model_action(self) -> ModelAction:
        """Convert to ModelAction (same type contract StepLoop uses)."""
        usage = TokenUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )

        if self.has_tool_uses:
            calls = tuple(
                MACToolCall(
                    id=tu.id,
                    name=tu.name,
                    params=freeze_json(tu.input),
                    usage=usage if i == 0 else None,
                )
                for i, tu in enumerate(self.tool_uses)
            )
            if len(calls) == 1:
                return calls[0]
            return ToolCallBatch(calls=calls, usage=usage)

        if self.stop_reason == "end_turn":
            return AssistantText(
                text=self.text, stop_reason=self.stop_reason, usage=usage,
            )

        if self.stop_reason == "max_tokens":
            return ModelStop(
                text=self.text, stop_reason=self.stop_reason, usage=usage,
            )

        if self.stop_reason == "refusal":
            return ModelRefusal(reason=self.text, usage=usage)

        return AssistantText(
            text=self.text,
            stop_reason=self.stop_reason or "end_turn",
            usage=usage,
        )


# ---------------------------------------------------------------------------
# NativeBackend -- Anthropic Native Backend, tools bound at init
# ---------------------------------------------------------------------------


class NativeBackend:
    """Anthropic Native Backend -- tools bound at initialization.

    Replaces the chain:
      _invoke_via_backend -> messages_to_llm -> backend.complete
      -> _to_anthropic_messages

    NativeBackend converts NativeConversation directly to Anthropic API calls,
    eliminating the LLMMessage translation layer.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        tool_schemas: tuple[NativeToolSchema, ...] = (),
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ):
        try:
            import anthropic as _anthropic
            self._client = _anthropic.Anthropic(
                api_key=api_key,
                timeout=timeout_seconds,
            )
        except ImportError:
            raise ImportError(
                "anthropic package not installed. Run: pip install anthropic"
            )

        self._model = model
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds

        # Tool schema cache -- extracted once at init
        self._tool_schemas: tuple[NativeToolSchema, ...] = tool_schemas
        self._cached_api_tools: list[dict] = [
            t.to_api_dict() for t in self._tool_schemas
        ]

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def tool_count(self) -> int:
        """Number of bound tools."""
        return len(self._tool_schemas)

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Names of bound tools."""
        return tuple(t.name for t in self._tool_schemas)

    # ── invoke -- core entry point ──────────────────────────────────────

    def invoke(
        self,
        conversation: NativeConversation,
        *,
        tool_choice: dict | None = None,
        cancellation: object | None = None,
    ) -> ModelAction:
        """Call LLM -- no tools parameter (already bound at init).

        P0: Protocol pre-validation at entry, catches API 400s locally.
        P2: Cooperative cancellation -- pass CancellationHandle to detect interrupt.

        Args:
            conversation: Complete conversation history (NativeMessage sequence)
            tool_choice: CC-aligned tool_choice (None="auto")
            cancellation: CancellationHandle | None

        Returns:
            ModelAction -- same type contract as StepLoop

        Raises:
            ValueError: Protocol pre-validation failed (messages violate API constraints)
        """
        # ── P0: Protocol pre-validation ──────────────────────────────────
        from runtime_core.message_validator import validate_messages
        validation = validate_messages(conversation)
        validation.raise_if_invalid()

        # ── P2: Cancellation check ──────────────────────────────────────
        if cancellation is not None and hasattr(cancellation, 'cancelled'):
            if cancellation.cancelled:
                return ModelFailure(
                    error="Cancelled before LLM call", retryable=False,
                )

        # Step 1: Extract system messages (Anthropic API passes them separately)
        system_content = _extract_system(conversation)
        non_system = conversation.non_system_messages

        # Step 2: NativeMessage -> Anthropic API dict (zero isinstance checks)
        api_messages = [_native_msg_to_api_dict(m) for m in non_system]

        # Step 3: Build request
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
        }
        if system_content:
            kwargs["system"] = system_content
        if self._cached_api_tools:
            kwargs["tools"] = self._cached_api_tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        logger.debug(
            "NativeBackend request: model=%s messages=%d tools=%d",
            self._model, len(api_messages), len(self._cached_api_tools),
        )

        # Step 4: Call Anthropic SDK
        response = self._client.messages.create(**kwargs)

        logger.debug(
            "NativeBackend response: stop_reason=%s input=%d output=%d",
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        # Step 5: Parse response -> NativeResponse -> ModelAction
        native_response = _parse_sdk_response(response)
        return native_response.to_model_action()

    # ── Streaming ────────────────────────────────────────────────────────

    def stream_iter(
        self,
        conversation: NativeConversation,
        *,
        tool_choice: dict | None = None,
    ):
        """Streaming call -- yield StreamEvent (CC-aligned streaming dispatch).

        Same event semantics as AnthropicBackend.stream_iter().
        """
        from llm.base import StreamEvent, StreamEventKind

        system_content = _extract_system(conversation)
        non_system = conversation.non_system_messages
        api_messages = [_native_msg_to_api_dict(m) for m in non_system]

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
        }
        if system_content:
            kwargs["system"] = system_content
        if self._cached_api_tools:
            kwargs["tools"] = self._cached_api_tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        try:
            with self._client.messages.stream(**kwargs) as stream:
                for text_chunk in stream.text_stream:
                    if text_chunk:
                        yield StreamEvent(
                            kind=StreamEventKind.TEXT_DELTA,
                            text=text_chunk,
                        )

                final = stream.get_final_message()
                native_response = _parse_sdk_response(final)

                for tu in native_response.tool_uses:
                    yield StreamEvent(
                        kind=StreamEventKind.TOOL_USE,
                        tool_call=MACToolCall(
                            id=tu.id, name=tu.name,
                            params=tu.input,  # type: ignore[arg-type]
                        ),
                    )

                yield StreamEvent(
                    kind=StreamEventKind.FINISH,
                    text=native_response.text,
                    finish_message=native_response.text,
                )

        except Exception as exc:
            yield StreamEvent(kind=StreamEventKind.ERROR, text=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_system(conversation: NativeConversation) -> str | list[dict]:
    """Extract system messages, merge into Anthropic API format.

    Anthropic API system param accepts str or ContentBlock list.
    """
    system_msgs = conversation.system_messages
    if not system_msgs:
        return ""

    texts: list[str] = []
    blocks: list[dict] = []
    has_structured = False

    for msg in system_msgs:
        for block in msg.content:
            if isinstance(block, TextBlock):
                if block.cache_control is not None:
                    has_structured = True
                    blocks.append(block.to_api_dict())
                else:
                    texts.append(block.text)

    if has_structured or blocks:
        result: list[dict] = []
        for t in texts:
            result.append({"type": "text", "text": t})
        result.extend(blocks)
        return result

    return "\n\n".join(t for t in texts if t)


def _to_anthropic_tool_input(params) -> dict:
    """Convert FrozenJsonObject / dict to Anthropic SDK input dict."""
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
        return {k: v for k, v in params.items()}
    return {}


# ---------------------------------------------------------------------------
# NativeMessage -> API dict (zero LLMMessage translation)
# ---------------------------------------------------------------------------


def _native_msg_to_api_dict(msg: NativeMessage) -> dict:
    """Convert NativeMessage directly to Anthropic API dict.

    Key difference from AnthropicBackend._to_anthropic_messages():
    - Input is NativeMessage (typed), not LLMMessage (str|list)
    - Zero isinstance(content, str) checks -- content is always tuple[ContentBlock, ...]
    - Zero message_mapper translation
    """
    role = msg.role

    # Single text block -> simplify to plain text
    if len(msg.content) == 1 and isinstance(msg.content[0], TextBlock):
        return {"role": role, "content": msg.content[0].text}

    # Multiple blocks -> ContentBlock array
    api_blocks: list[dict] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            api_blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            api_blocks.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": _to_anthropic_tool_input(block.input),
            })
        elif isinstance(block, ToolResultBlock):
            api_blocks.append(block.to_api_dict())

    # Anthropic API: tool_result must be sent as user role
    has_tool_results = any(
        isinstance(b, ToolResultBlock) for b in msg.content
    )
    api_role = "user" if has_tool_results else role

    return {"role": api_role, "content": api_blocks}


# ---------------------------------------------------------------------------
# SDK Response -> NativeResponse
# ---------------------------------------------------------------------------


def _parse_sdk_response(response: Any) -> NativeResponse:
    """Parse Anthropic SDK response into NativeResponse.

    Key difference from AnthropicBackend._parse_anthropic_response():
    - Output is NativeResponse (ContentBlock tuple), not Action (LLMMessage-era)
    - Preserves original ContentBlock structure without semantic compression
    """
    blocks: list[TextBlock | ToolUseBlock] = []

    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            blocks.append(TextBlock(text=getattr(block, "text", "") or ""))
        elif block_type == "tool_use":
            blocks.append(ToolUseBlock(
                id=getattr(block, "id", "") or "",
                name=getattr(block, "name", "") or "",
                input=dict(getattr(block, "input", {}) or {}),
            ))

    usage = response.usage
    return NativeResponse(
        content=tuple(blocks),
        stop_reason=getattr(response, "stop_reason", "") or "",
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )
