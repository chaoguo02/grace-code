"""Canonical fail-closed tool factory.

Every dynamically assembled tool is a normal ``BaseTool``.  There is no
second runtime Tool protocol and no proxy layer between MCP and the agent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.base import (
    BaseTool,
    RiskLevel,
    ToolConcurrency,
    ToolMetadata,
    ToolResult,
)


class BuiltTool(BaseTool):
    """Concrete ``BaseTool`` produced by :func:`build_tool`."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters_schema: dict[str, Any],
        execute: Callable[[dict[str, Any]], ToolResult],
        metadata: ToolMetadata | None = None,
        aliases: tuple[str, ...] = (),
        is_enabled: Callable[[], bool] | None = None,
        is_read_only: Callable[[dict[str, Any]], bool] | None = None,
        concurrency: Callable[[dict[str, Any]], ToolConcurrency] | None = None,
        risk: Callable[[dict[str, Any]], str] | None = None,
        close: Callable[[float], None] | None = None,
        mcp_props: Any = None,
    ) -> None:
        self._name = name
        self._description = description
        self._parameters_schema = dict(parameters_schema)
        self._execute = execute
        self.metadata = metadata or ToolMetadata()
        self.aliases = aliases
        self._is_enabled = is_enabled or (lambda: True)
        self._is_read_only = is_read_only or (lambda _params: False)
        self._concurrency = concurrency or (
            lambda _params: ToolConcurrency.SERIAL
        )
        self._risk = risk or (lambda _params: RiskLevel.MEDIUM)
        self._close = close
        self._supports_cancellation = False
        self.mcp_props = mcp_props
        self.is_mcp = mcp_props is not None
        self._always_load = False
        self._should_defer = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return dict(self._parameters_schema)

    @property
    def risk_level(self) -> str:
        return self._risk({})

    def classify_risk(self, params: dict[str, Any]) -> str:
        return self._risk(params)

    def concurrency_mode(
        self,
        params: dict[str, Any],
    ) -> ToolConcurrency:
        return self._concurrency(params)

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return self._is_read_only(params or {})

    @property
    def supports_cancellation(self) -> bool:
        return self._supports_cancellation

    def is_enabled(self) -> bool:
        return bool(self._is_enabled())

    @property
    def always_load(self) -> bool:
        if self.mcp_props is not None:
            return bool(self.mcp_props.always_load)
        return self._always_load

    @always_load.setter
    def always_load(self, value: bool) -> None:
        if self.mcp_props is not None:
            self.mcp_props.always_load = bool(value)
        else:
            self._always_load = bool(value)

    @property
    def should_defer(self) -> bool:
        if self.mcp_props is not None:
            return bool(self.mcp_props.is_deferred)
        return self._should_defer

    @should_defer.setter
    def should_defer(self, value: bool) -> None:
        if self.mcp_props is not None:
            self.mcp_props.is_deferred = bool(value)
        else:
            self._should_defer = bool(value)

    def execute(self, params: dict[str, Any]) -> ToolResult:
        return self._execute(params)

    def close(self, timeout: float = 5.0) -> None:
        if self._close is not None:
            self._close(timeout)


def build_tool(
    *,
    tool: BaseTool | None = None,
    name: str = "",
    description: str = "",
    parameters_schema: dict[str, Any] | None = None,
    execute: Callable[[dict[str, Any]], ToolResult] | None = None,
    metadata: ToolMetadata | None = None,
    aliases: tuple[str, ...] = (),
    is_enabled: Callable[[], bool] | None = None,
    is_read_only: Callable[[dict[str, Any]], bool] | None = None,
    concurrency: Callable[[dict[str, Any]], ToolConcurrency] | None = None,
    risk: Callable[[dict[str, Any]], str] | None = None,
    close: Callable[[float], None] | None = None,
    mcp_props: Any = None,
    supports_cancellation: bool = False,
) -> BaseTool:
    """Build or validate one canonical tool with fail-closed defaults."""
    if tool is not None:
        if not isinstance(tool, BaseTool):
            raise TypeError("tool must implement BaseTool")
        if not tool.name:
            raise ValueError("tool name must not be empty")
        return tool
    if not name or parameters_schema is None or execute is None:
        raise ValueError(
            "dynamic tools require name, parameters_schema, and execute",
        )
    tool_obj = BuiltTool(
        name=name,
        description=description,
        parameters_schema=parameters_schema,
        execute=execute,
        metadata=metadata,
        aliases=aliases,
        is_enabled=is_enabled,
        is_read_only=is_read_only,
        concurrency=concurrency,
        risk=risk,
        close=close,
        mcp_props=mcp_props,
    )
    tool_obj._supports_cancellation = supports_cancellation
    return tool_obj


# Architecture-document spelling for external integrations.
buildTool = build_tool
