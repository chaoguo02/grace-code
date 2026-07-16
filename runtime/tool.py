"""
Tool Runtime — aligned with Claude Code src/Tool.ts + buildTool().

Source basis:
  A: Tool type definition
  B: fail-closed safety defaults
  C: buildTool factory function
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Generic, Protocol, TypeVar, runtime_checkable


InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class PermissionDecision(Enum):
    """Permission check result aligned with Claude Code PermissionResult."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class ToolResult(Generic[OutputT]):
    """Tool call result aligned with Claude Code ToolResult<Output>."""

    output: OutputT
    size_chars: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.size_chars == 0 and isinstance(self.output, str):
            self.size_chars = len(self.output)


@dataclass
class ToolUseContext:
    """Runtime context supplied to tool calls."""

    working_dir: str = "."
    session_id: str = ""
    user_input: str = ""


@dataclass
class ToolCall:
    """One model tool_use request."""

    id: str
    name: str
    input: dict


@dataclass
class ToolExecutionResult:
    """Executor-level result including timing and errors."""

    call_id: str
    tool_name: str
    result: ToolResult | None = None
    error: str | None = None
    duration_ms: float = 0.0
    started_at: float = 0.0


@runtime_checkable
class Tool(Protocol[InputT, OutputT]):
    """Tool protocol aligned with Claude Code src/Tool.ts."""

    @property
    def name(self) -> str:
        """Unique tool name."""
        ...

    @property
    def input_schema(self) -> dict:
        """JSON Schema for tool input."""
        ...

    async def call(self, input: InputT, context: ToolUseContext) -> ToolResult[OutputT]:
        """Execute the tool."""
        ...

    async def description(self) -> str:
        """Tool description contributed to the model prompt."""
        ...

    def is_concurrency_safe(self, input: InputT | None = None) -> bool:
        """Whether this input can run concurrently. Defaults fail-closed."""
        return False

    def is_read_only(self, input: InputT | None = None) -> bool:
        """Whether this input is read-only. Defaults fail-closed."""
        return False

    def is_destructive(self, input: InputT | None = None) -> bool:
        """Whether this input is destructive."""
        return False

    def is_enabled(self) -> bool:
        """Whether the tool is enabled in this environment."""
        return True

    @property
    def max_result_size_chars(self) -> int:
        """Maximum result size before externalization."""
        return 100_000

    async def validate_input(
        self,
        input: InputT,
        context: ToolUseContext,
    ) -> tuple[bool, str | None]:
        """Validate input shape before permissions and execution."""
        return True, None

    async def check_permissions(
        self,
        input: InputT,
        context: ToolUseContext,
    ) -> PermissionDecision:
        """Tool-local permission check."""
        return PermissionDecision.ALLOW


def build_tool(
    *,
    name: str,
    input_schema: dict,
    call_fn: Callable[[Any, ToolUseContext], Awaitable[ToolResult]],
    description_fn: Callable[[], Awaitable[str]] | None = None,
    description_text: str | None = None,
    is_concurrency_safe: Callable[[Any], bool] | None = None,
    is_read_only: Callable[[Any], bool] | None = None,
    is_destructive: Callable[[Any], bool] | None = None,
    is_enabled: Callable[[], bool] | None = None,
    max_result_size_chars: int = 100_000,
    validate_input_fn: Callable[[Any, ToolUseContext], Awaitable[tuple[bool, str | None]]] | None = None,
    check_permissions_fn: Callable[[Any, ToolUseContext], Awaitable[PermissionDecision]] | None = None,
    mcp_props: Any = None,
) -> "ConcreteTool":
    """Build a concrete tool instance from functional parts."""

    return ConcreteTool(
        _name=name,
        _input_schema=input_schema,
        _call_fn=call_fn,
        _description_fn=description_fn,
        _description_text=description_text or "",
        _is_concurrency_safe=is_concurrency_safe or (lambda _: False),
        _is_read_only=is_read_only or (lambda _: False),
        _is_destructive=is_destructive or (lambda _: False),
        _is_enabled=is_enabled or (lambda: True),
        _max_result_size_chars=max_result_size_chars,
        _validate_input_fn=validate_input_fn,
        _check_permissions_fn=check_permissions_fn,
        _mcp_props=mcp_props,
    )


class ConcreteTool:
    """Concrete tool returned by build_tool()."""

    def __init__(
        self,
        *,
        _name: str,
        _input_schema: dict,
        _call_fn: Callable,
        _description_fn: Callable | None,
        _description_text: str,
        _is_concurrency_safe: Callable,
        _is_read_only: Callable,
        _is_destructive: Callable,
        _is_enabled: Callable,
        _max_result_size_chars: int,
        _validate_input_fn: Callable | None,
        _check_permissions_fn: Callable | None,
        _mcp_props: Any = None,
    ) -> None:
        self._name = _name
        self._input_schema = _input_schema
        self._call_fn = _call_fn
        self._description_fn = _description_fn
        self._description_text = _description_text
        self._is_concurrency_safe = _is_concurrency_safe
        self._is_read_only = _is_read_only
        self._is_destructive = _is_destructive
        self._is_enabled = _is_enabled
        self._max_result_size_chars = _max_result_size_chars
        self._validate_input_fn = _validate_input_fn
        self._check_permissions_fn = _check_permissions_fn
        self.mcp_props = _mcp_props  # MCPToolProps | None — declarative MCP attachment

    @property
    def name(self) -> str:
        return self._name

    @property
    def input_schema(self) -> dict:
        return self._input_schema

    @property
    def max_result_size_chars(self) -> int:
        return self._max_result_size_chars

    async def description(self) -> str:
        if self._description_fn:
            return await self._description_fn()
        return self._description_text

    def is_concurrency_safe(self, input: Any = None) -> bool:
        return self._is_concurrency_safe(input)

    def is_read_only(self, input: Any = None) -> bool:
        return self._is_read_only(input)

    def is_destructive(self, input: Any = None) -> bool:
        return self._is_destructive(input)

    def is_enabled(self) -> bool:
        return self._is_enabled()

    async def validate_input(self, input: Any, context: ToolUseContext) -> tuple[bool, str | None]:
        if self._validate_input_fn:
            return await self._validate_input_fn(input, context)
        return True, None

    async def check_permissions(self, input: Any, context: ToolUseContext) -> PermissionDecision:
        if self._check_permissions_fn:
            return await self._check_permissions_fn(input, context)
        return PermissionDecision.ALLOW

    async def call(self, input: Any, context: ToolUseContext) -> ToolResult:
        return await self._call_fn(input, context)

    def to_api_definition(self) -> dict:
        """Serialize as an Anthropic-style tool definition."""
        return {
            "name": self._name,
            "description": self._description_text,
            "input_schema": self._input_schema,
        }

    def __repr__(self) -> str:
        return f"Tool({self._name})"
