"""
runtime/query_loop.py — query loop implementations for runtime ReAct flows.

This module keeps the original coroutine-style query_loop API used by the
runtime tests and also exposes a streaming-generator path aligned with Claude
Code's queryLoop architecture.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, AsyncGenerator, Awaitable, Callable, Protocol, Sequence

from runtime.context_compression import (
    DEFAULT_CONTEXT_WINDOW,
    AutoCompactTrackingState,
    ContentReplacementState,
    compress_messages,
)
from runtime.streaming_executor import StreamingToolExecutor
from runtime.tool import ToolCall, ToolExecutionResult, ToolUseContext
from runtime.tool_executor import execute_single_tool
from runtime.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3
MAX_STOP_HOOK_RETRIES = 3


@dataclass(frozen=True)
class RuntimeMessage:
    """Unified message used by the legacy runtime query loop."""

    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    @classmethod
    def user(cls, text: str) -> "RuntimeMessage":
        return cls(role="user", content=text)

    @classmethod
    def assistant(
        cls,
        text: str = "",
        tool_calls: Sequence[ToolCall] = (),
    ) -> "RuntimeMessage":
        return cls(role="assistant", content=text, tool_calls=tuple(tool_calls))

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> "RuntimeMessage":
        return cls(role="tool_result", content=content, tool_call_id=tool_call_id)


@dataclass(frozen=True)
class RuntimeModelResponse:
    """Standardized model response for one legacy query-loop turn."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class ModelFn(Protocol):
    """Async model function protocol used by tests and future adapters."""

    def __call__(
        self,
        messages: list[RuntimeMessage],
        registry: ToolRegistry,
    ) -> Awaitable[RuntimeModelResponse]:
        ...


class MaxTurnsExceededError(RuntimeError):
    """Raised when the legacy query_loop exceeds max_turns."""

    def __init__(self, max_turns: int) -> None:
        super().__init__(f"query_loop exceeded max_turns={max_turns}")
        self.max_turns = max_turns


class LoopExitReason(Enum):
    """Why the streaming loop stopped."""

    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    ABORTED = "aborted"
    BLOCKING_LIMIT = "blocking_limit"
    FALLBACK_EXHAUSTED = "fallback_exhausted"
    ERROR = "error"


class Transition(Enum):
    """Why the streaming loop continued to another iteration."""

    NEXT_TURN = "next_turn"
    REACTIVE_COMPACT = "reactive_compact"
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    STOP_HOOK = "stop_hook"
    FALLBACK_MODEL = "fallback_model"


@dataclass(frozen=True)
class LoopState:
    """Immutable-style state object replaced wholesale each iteration."""

    messages: list[Any] = field(default_factory=list)
    turn_count: int = 1
    transition: Transition | None = None
    has_attempted_reactive_compact: bool = False
    max_output_tokens_recovery_count: int = 0
    streaming_executor: StreamingToolExecutor | None = None
    consecutive_tool_errors: int = 0
    max_consecutive_tool_errors: int = 3
    autocompact_tracking: AutoCompactTrackingState | None = None
    content_replacement_state: ContentReplacementState | None = None
    stop_hook_count: int = 0


@dataclass
class StreamEvent:
    """Base event yielded by the streaming loop."""

    type: str


@dataclass
class ModelStreamEvent(StreamEvent):
    """Forwarded model stream event."""

    type: str = "model_stream"
    data: Any = None


@dataclass
class ToolResultEvent(StreamEvent):
    """A single tool execution result."""

    type: str = "tool_result"
    tool_call_id: str = ""
    output: Any = None
    is_error: bool = False


@dataclass
class LoopTerminalEvent(StreamEvent):
    """Loop terminal event."""

    type: str = "loop_terminal"
    reason: LoopExitReason = LoopExitReason.COMPLETED


CallModelFn = Callable[..., AsyncGenerator[Any, None]]
ExecuteToolFn = Callable[[Any], Awaitable[Any]]
ConcurrencyFn = Callable[[Any], bool]
StopHookFn = Callable[[list[Any]], Awaitable[list[Any] | None]]


def query_loop(
    messages: list[Any],
    *,
    model_fn: ModelFn | None = None,
    registry: ToolRegistry | None = None,
    call_model: CallModelFn | None = None,
    execute_tool: ExecuteToolFn | None = None,
    get_concurrency_safe: ConcurrencyFn | None = None,
    max_turns: int | None = 10,
    abort_signal: asyncio.Event | None = None,
    on_stop_hook: StopHookFn | None = None,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    call_model_for_summary: Callable | None = None,
    max_consecutive_tool_errors: int = 3,
):
    """
    Dispatch to the legacy coroutine loop or the streaming generator loop.

    Legacy API:
        await query_loop(messages, model_fn=..., registry=..., max_turns=10)

    Streaming API:
        async for event in query_loop(
            messages,
            call_model=...,
            execute_tool=...,
            get_concurrency_safe=...,
        ): ...
    """
    if call_model is not None:
        if execute_tool is None or get_concurrency_safe is None:
            raise TypeError("streaming query_loop requires execute_tool and get_concurrency_safe")
        return _streaming_query_loop(
            messages=messages,
            call_model=call_model,
            execute_tool=execute_tool,
            get_concurrency_safe=get_concurrency_safe,
            max_turns=max_turns,
            abort_signal=abort_signal,
            on_stop_hook=on_stop_hook,
            context_window=context_window,
            call_model_for_summary=call_model_for_summary,
            max_consecutive_tool_errors=max_consecutive_tool_errors,
        )

    if model_fn is None or registry is None:
        raise TypeError("legacy query_loop requires model_fn and registry")

    return _legacy_query_loop(
        messages=messages,
        model_fn=model_fn,
        registry=registry,
        max_turns=10 if max_turns is None else max_turns,
    )


async def _legacy_query_loop(
    messages: list[RuntimeMessage],
    *,
    model_fn: ModelFn,
    registry: ToolRegistry,
    max_turns: int = 10,
) -> str:
    """Run the original message-driven ReAct loop."""

    for _turn in range(max_turns):
        response = await model_fn(messages, registry)
        messages.append(RuntimeMessage.assistant(text=response.text, tool_calls=response.tool_calls))

        if not response.has_tool_calls:
            return response.text

        context = ToolUseContext()
        executor = StreamingToolExecutor()
        for tool_call in response.tool_calls:
            tool = registry.find_by_name(tool_call.name)
            executor.add_tool(
                tool_call,
                is_concurrency_safe=tool.is_concurrency_safe(tool_call.input) if tool else False,
            )

        async def execute_fn(tool_call: ToolCall) -> ToolExecutionResult:
            tool = registry.find_by_name(tool_call.name)
            if tool is None:
                return ToolExecutionResult(
                    call_id=tool_call.id,
                    tool_name=tool_call.name,
                    error=f"Unknown tool: {tool_call.name}",
                )
            return await execute_single_tool(tool, tool_call, context)

        results = await executor.execute_all(execute_fn)

        for result in results:
            if result.result is not None:
                content = str(result.result.output)
            else:
                content = result.error or ""
            messages.append(RuntimeMessage.tool_result(result.call_id, content))

    raise MaxTurnsExceededError(max_turns)


async def _streaming_query_loop(
    *,
    messages: list[Any],
    call_model: CallModelFn,
    execute_tool: ExecuteToolFn,
    get_concurrency_safe: ConcurrencyFn,
    max_turns: int | None = None,
    abort_signal: asyncio.Event | None = None,
    on_stop_hook: StopHookFn | None = None,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    call_model_for_summary: Callable | None = None,
    max_consecutive_tool_errors: int = 3,
) -> AsyncGenerator[StreamEvent, None]:
    """Claude Code-style streaming query loop."""

    state = LoopState(
        messages=list(messages),
        max_consecutive_tool_errors=max_consecutive_tool_errors,
        autocompact_tracking=AutoCompactTrackingState(),
        content_replacement_state=ContentReplacementState(),
    )

    while True:
        if abort_signal and abort_signal.is_set():
            yield LoopTerminalEvent(reason=LoopExitReason.ABORTED)
            return

        if max_turns is not None and state.turn_count > max_turns:
            logger.warning("max_turns reached: %d", state.turn_count)
            yield LoopTerminalEvent(reason=LoopExitReason.MAX_TURNS)
            return

        compression = await compress_messages(
            state.messages,
            context_window=context_window,
            call_model_for_summary=call_model_for_summary,
            autocompact_tracking=state.autocompact_tracking,
            content_replacement_state=state.content_replacement_state,
        )
        messages_for_query = compression.messages

        if "blocking_limit" in compression.layers_applied:
            yield LoopTerminalEvent(reason=LoopExitReason.BLOCKING_LIMIT)
            return

        executor = StreamingToolExecutor()
        state = replace(state, streaming_executor=executor)

        tool_use_blocks: list[Any] = []
        assistant_text_parts: list[str] = []

        try:
            async for event in call_model(messages=messages_for_query):
                yield ModelStreamEvent(data=event)

                event_type = _get_event_type(event)
                if event_type == "text_delta":
                    assistant_text_parts.append(_get_text_content(event))
                elif event_type == "tool_use":
                    tool_block = _extract_tool_use_block(event)
                    if tool_block is not None:
                        tool_use_blocks.append(tool_block)
                        executor.add_tool(
                            tool_block,
                            is_concurrency_safe=get_concurrency_safe(tool_block),
                            execute_fn=execute_tool,
                        )

        except _FallbackTriggeredError as exc:
            logger.warning("Fallback triggered: %s", exc)
            executor.discard()
            state = replace(state, transition=Transition.FALLBACK_MODEL)
            continue

        except _PromptTooLongError:
            if not state.has_attempted_reactive_compact:
                logger.info("Prompt too long, attempting reactive compact")
                state = replace(
                    state,
                    has_attempted_reactive_compact=True,
                    transition=Transition.REACTIVE_COMPACT,
                )
                continue

            logger.error("Reactive compact already attempted, giving up")
            yield LoopTerminalEvent(reason=LoopExitReason.BLOCKING_LIMIT)
            return

        except _MaxOutputTokensError:
            if state.max_output_tokens_recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
                recovery_msg = _make_output_recovery_message()
                state = replace(
                    state,
                    messages=[*state.messages, recovery_msg],
                    max_output_tokens_recovery_count=state.max_output_tokens_recovery_count + 1,
                    transition=Transition.MAX_OUTPUT_TOKENS,
                )
                continue

            logger.error("Max output tokens recovery limit reached")
            yield LoopTerminalEvent(reason=LoopExitReason.ERROR)
            return

        assistant_message = _build_assistant_message(
            text_parts=assistant_text_parts,
            tool_use_blocks=tool_use_blocks,
        )
        updated_messages = [*state.messages, assistant_message]

        if tool_use_blocks:
            tool_results = await executor.get_remaining_results()

            for result in tool_results:
                yield ToolResultEvent(
                    tool_call_id=_tool_call_id(result),
                    output=_tool_output(result),
                    is_error=_tool_is_error(result),
                )

            updated_messages = [*updated_messages, *_build_tool_result_messages(tool_results)]

            if tool_results:
                all_failed = all(_tool_is_error(result) for result in tool_results)
                if all_failed:
                    consecutive_tool_errors = state.consecutive_tool_errors + 1
                    if consecutive_tool_errors >= state.max_consecutive_tool_errors:
                        logger.warning(
                            "Circuit breaker tripped: %d consecutive tool error rounds",
                            consecutive_tool_errors,
                        )
                        yield LoopTerminalEvent(reason=LoopExitReason.ERROR)
                        return
                    state = replace(state, consecutive_tool_errors=consecutive_tool_errors)
                elif state.consecutive_tool_errors > 0:
                    state = replace(state, consecutive_tool_errors=0)

            state = replace(
                state,
                messages=updated_messages,
                turn_count=state.turn_count + 1,
                transition=Transition.NEXT_TURN,
            )
            continue

        if on_stop_hook is not None:
            hook_messages = await on_stop_hook(updated_messages)
            if hook_messages:
                next_count = state.stop_hook_count + 1
                if next_count > MAX_STOP_HOOK_RETRIES:
                    logger.warning("Stop hook retry limit reached: %d", MAX_STOP_HOOK_RETRIES)
                    yield LoopTerminalEvent(reason=LoopExitReason.ERROR)
                    return
                state = replace(
                    state,
                    messages=[*updated_messages, *hook_messages],
                    turn_count=state.turn_count + 1,
                    transition=Transition.STOP_HOOK,
                    stop_hook_count=next_count,
                )
                continue

        yield LoopTerminalEvent(reason=LoopExitReason.COMPLETED)
        return


class _FallbackTriggeredError(Exception):
    """Raised when the model provider signals a fallback is needed."""


class _PromptTooLongError(Exception):
    """Raised on prompt_too_long from the API."""


class _MaxOutputTokensError(Exception):
    """Raised when model output hits max_output_tokens."""


def _build_assistant_message(
    text_parts: list[str],
    tool_use_blocks: list[Any],
) -> dict:
    """Build an assistant message from collected stream parts."""
    content: list[dict] = []
    if text_parts:
        content.append({"type": "text", "text": "".join(text_parts)})
    for block in tool_use_blocks:
        if isinstance(block, dict):
            content.append({"type": "tool_use", **block})
        else:
            content.append({
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", {}),
            })
    return {"role": "assistant", "content": content}


def _build_tool_result_messages(results: list[Any]) -> list[dict]:
    """Convert tool results into user-role tool_result messages for the next turn."""
    messages = []
    for result in results:
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": _tool_call_id(result),
                    "tool_name": _tool_name(result),
                    "content": _tool_output(result),
                    "is_error": _tool_is_error(result),
                }
            ],
        })
    return messages


def _make_output_recovery_message() -> dict:
    """Message injected when model output is truncated."""
    return {
        "role": "user",
        "content": "Your previous response was truncated. Please continue from where you left off.",
    }


def _get_event_type(event: Any) -> str:
    if isinstance(event, dict):
        return event.get("type", "")
    return getattr(event, "type", "")


def _get_text_content(event: Any) -> str:
    if isinstance(event, dict):
        return event.get("text", event.get("delta", ""))
    return getattr(event, "text", getattr(event, "delta", ""))


def _extract_tool_use_block(event: Any) -> dict | None:
    """Extract a complete tool_use block from a stream event."""
    if isinstance(event, dict):
        if event.get("type") == "tool_use" and event.get("complete", True):
            return {
                "id": event.get("id", ""),
                "name": event.get("name", ""),
                "input": event.get("input", {}),
            }
        return None

    if getattr(event, "type", "") == "tool_use" and getattr(event, "complete", True):
        return {
            "id": getattr(event, "id", ""),
            "name": getattr(event, "name", ""),
            "input": getattr(event, "input", {}),
        }
    return None


def _tool_call_id(result: Any) -> str:
    if isinstance(result, dict):
        return result.get("tool_call_id", result.get("call_id", result.get("id", "")))
    return getattr(result, "tool_call_id", getattr(result, "call_id", getattr(result, "id", "")))


def _tool_output(result: Any) -> Any:
    if isinstance(result, ToolExecutionResult):
        if result.result is not None:
            return result.result.output
        return result.error or ""
    if isinstance(result, dict):
        return result.get("output", result.get("content", result.get("error", "")))
    return getattr(result, "output", getattr(result, "content", getattr(result, "error", "")))


def _tool_name(result: Any) -> str:
    if isinstance(result, ToolExecutionResult):
        return result.tool_name
    if isinstance(result, dict):
        return str(result.get("tool_name", result.get("name", "")))
    return str(getattr(result, "tool_name", getattr(result, "name", "")))


def _tool_is_error(result: Any) -> bool:
    if isinstance(result, ToolExecutionResult):
        return result.error is not None
    if isinstance(result, dict):
        return result.get("is_error", result.get("error") is not None)
    return bool(getattr(result, "is_error", getattr(result, "error", None) is not None))
