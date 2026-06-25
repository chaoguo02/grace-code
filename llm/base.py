"""
llm/base.py

LLMBackend 抽象基类，定义所有 backend 必须实现的统一接口。
Agent Core 只依赖这个抽象，永不 import 具体 SDK。

还包含：
- LLMMessage / LLMResponse / LLMToolSchema 数据类（跨 backend 统一格式）
- MockBackend（测试专用，按脚本返回预设 action，不消耗 API）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent.task import Action, ActionType, ToolCall


# ---------------------------------------------------------------------------
# 跨 backend 统一数据格式
# ---------------------------------------------------------------------------

@dataclass
class LLMMessage:
    """
    发送给 LLM 的单条消息。
    role: "system" | "user" | "assistant" | "tool"
    content: 纯文本 str，或 content blocks 列表（Anthropic cache_control 格式）。
    tool_call_id 仅在 role=="tool" 时使用（工具执行结果关联到对应的 tool_use）。
    tool_calls 仅在 role=="assistant" 时使用（native function calling 模式）。
    """
    role: str
    content: "str | list[dict[str, Any]]"
    tool_call_id: str | None = None     # role=="tool" 时关联对应的 tool_use id
    tool_calls: "list[ToolCall] | None" = None  # role=="assistant" 时的 native tool calls


@dataclass
class LLMToolSchema:
    """
    向 LLM 描述一个可用工具的 schema。
    由 ToolRegistry.get_schemas() 生成，注入 LLM 调用时的 tools 参数。
    """
    name: str                           # 工具名
    description: str                    # 工具描述
    parameters: dict[str, Any]          # JSON Schema 格式


@dataclass
class CacheStats:
    """Prompt caching 统计信息。"""
    cache_read_tokens: int = 0          # 从缓存读取的 token 数
    cache_creation_tokens: int = 0      # 写入缓存的 token 数（首次缓存成本稍高）
    non_cached_input_tokens: int = 0    # 未命中缓存的输入 token 数

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率 = cached / total input tokens。"""
        total = self.cache_read_tokens + self.cache_creation_tokens + self.non_cached_input_tokens
        if total == 0:
            return 0.0
        return self.cache_read_tokens / total

    @property
    def has_cache_activity(self) -> bool:
        return self.cache_read_tokens > 0 or self.cache_creation_tokens > 0


@dataclass
class LLMResponse:
    """
    LLM 返回的统一响应格式。
    backend 负责把各家 API 的原始响应解析成这个结构。
    """
    action: Action                      # 解析好的 Action，含 tool_call 或 finish/give_up
    raw_content: str                    # LLM 原始文本输出
    input_tokens: int = 0
    output_tokens: int = 0
    cache_stats: CacheStats = field(default_factory=CacheStats)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class LLMBackend(ABC):
    """
    所有 LLM backend 必须实现的接口。

    子类实现 complete()，负责：
    1. 把 LLMMessage 列表转换为各家 API 的请求格式
    2. 发送请求
    3. 把响应解析为 LLMResponse（含 Action）
    """

    @abstractmethod
    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        """
        调用 LLM，返回解析好的响应。

        Args:
            messages: 完整对话历史，含 system prompt
            tools:    可用工具的 schema 列表

        Returns:
            LLMResponse，其中 action 已解析完毕，core.py 直接使用
        """
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """返回当前使用的模型名称，用于日志和统计。"""
        ...

    @property
    def supports_function_calling(self) -> bool:
        """
        是否支持 function calling。
        默认 True；不支持的模型（如 DeepSeek R1）覆盖返回 False。
        """
        return True

    @property
    def max_context_window(self) -> int:
        """
        模型的最大上下文窗口大小（token 数）。
        默认 128K，子类覆盖。TokenBudget 据此动态分配预算。
        """
        return 128_000

    def stream(
        self,
        messages: "list[LLMMessage]",
        tools: "list[LLMToolSchema]",
        on_text: "StreamCallback | None" = None,
        on_thought: "StreamCallback | None" = None,
    ) -> "LLMResponse":
        """
        流式调用 LLM。默认 fallback 到 complete()。
        on_text:    模型最终回答（message）的流式回调
        on_thought: 模型推理过程（thought）的流式回调，仅推理模型有内容
        """
        response = self.complete(messages, tools)
        if on_text and response.raw_content:
            on_text(response.raw_content)
        return response


# ---------------------------------------------------------------------------
# MockBackend — 测试专用
# ---------------------------------------------------------------------------

class MockBackend(LLMBackend):
    """
    测试专用 backend，按脚本顺序返回预设的 Action。

    用法：
        script = [
            Action(ActionType.TOOL_CALL, "run tests", [ToolCall("shell", {"cmd": "pytest"})]),
            Action(ActionType.FINISH, "all tests pass", message="Done"),
        ]
        backend = MockBackend(script)
        # 每次调用 complete() 返回 script 中的下一个 Action
        # 脚本用完后自动返回 GIVE_UP

    好处：
    - 完全不消耗 API，测试快且稳定
    - 可以精确控制 agent 走哪条路径
    - 可以模拟"连续相同 action"触发死循环检测
    """

    def __init__(
        self,
        script: list[Action],
        input_tokens: int = 100,
        output_tokens: int = 50,
    ) -> None:
        self._script = script
        self._index = 0
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        # 记录所有 complete() 调用，供测试断言
        self.call_count = 0
        self.received_messages: list[list[LLMMessage]] = []

    @property
    def model_name(self) -> str:
        return "mock-model"

    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        import uuid

        self.call_count += 1
        self.received_messages.append(messages)

        if self._index < len(self._script):
            action = self._script[self._index]
            self._index += 1
        else:
            # 脚本用完，返回 GIVE_UP 防止 core.py 死循环
            action = Action(
                action_type=ActionType.GIVE_UP,
                thought="MockBackend script exhausted.",
                message="No more scripted actions.",
            )

        # Assign synthetic ID for tool calls without one (native tool_use round-trip)
        for tc in action.tool_calls:
            if tc.id is None:
                tc.id = f"mock_{uuid.uuid4().hex[:8]}"

        return LLMResponse(
            action=action,
            raw_content=f"[mock] {action!r}",
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )

    def reset(self) -> None:
        """重置脚本指针，复用同一个 backend 跑多个测试。"""
        self._index = 0
        self.call_count = 0
        self.received_messages.clear()


# ---------------------------------------------------------------------------
# 流式输出支持
# ---------------------------------------------------------------------------

from typing import Generator, Callable


@dataclass
class StreamChunk:
    """
    流式输出的单个 chunk。
    text_delta: 这个 chunk 新增的文本片段（thought 部分）
    is_done:    是否是最后一个 chunk
    final_response: is_done=True 时包含完整的 LLMResponse
    """
    text_delta: str = ""
    is_done: bool = False
    final_response: "LLMResponse | None" = None


# Callable 类型：接收 text_delta，实时打印
StreamCallback = Callable[[str], None]


class StreamingMixin:
    """
    为 LLMBackend 子类提供流式调用能力的 Mixin。
    子类实现 stream()，complete() 可以复用它。
    """

    def stream(
        self,
        messages: "list[LLMMessage]",
        tools: "list[LLMToolSchema]",
        on_text: StreamCallback | None = None,
    ) -> "LLMResponse":
        """
        流式调用 LLM。

        Args:
            messages:  完整对话历史
            tools:     工具 schema 列表
            on_text:   每收到一个 text chunk 时的回调，用于实时打印 thought

        Returns:
            完整的 LLMResponse（流结束后才返回，与 complete() 格式一致）

        默认实现：直接调用 complete()（非流式 fallback）。
        支持流式的 backend 覆盖此方法。
        """
        response = self.complete(messages, tools)  # type: ignore[attr-defined]
        # 非流式 fallback：把 raw_content 当作一次性输出
        if on_text and response.raw_content:
            on_text(response.raw_content)
        return response