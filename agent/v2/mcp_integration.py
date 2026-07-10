"""MCP integration helpers for the v2 session runtime."""

from __future__ import annotations

import asyncio
import logging
from fnmatch import fnmatch
from typing import Any, Iterable

from runtime.mcp import MCPServerConfig, SyncMCPToolManager, assemble_tool_pool
from runtime.tool import ToolResult as RuntimeToolResult, ToolUseContext
from tools.base import BaseTool, RiskLevel, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


class MCPRuntimeToolProxy(BaseTool):
    """Adapt a runtime ConcreteTool returned by runtime.mcp for legacy v2 tools."""

    def __init__(self, runtime_tool: Any) -> None:
        self._runtime_tool = runtime_tool
        self.is_mcp = bool(getattr(runtime_tool, "is_mcp", True))
        self.always_load = bool(getattr(runtime_tool, "always_load", False))
        self.should_defer = bool(getattr(runtime_tool, "should_defer", False))
        self.metadata = dict(getattr(runtime_tool, "metadata", {}) or {})

    @property
    def name(self) -> str:
        return self._runtime_tool.name

    @property
    def description(self) -> str:
        if hasattr(self._runtime_tool, "to_api_definition"):
            definition = self._runtime_tool.to_api_definition()
            return str(definition.get("description") or f"MCP tool {self.name}")
        return f"MCP tool {self.name}"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return dict(getattr(self._runtime_tool, "input_schema", {"type": "object", "properties": {}}))

    @property
    def risk_level(self) -> str:
        return RiskLevel.MEDIUM

    def execute(self, params: dict[str, Any]) -> ToolResult:
        try:
            result = asyncio.run(self._runtime_tool.call(params, ToolUseContext()))
        except RuntimeError as exc:
            if "asyncio.run() cannot be called from a running event loop" not in str(exc):
                return ToolResult(success=False, output="", error=f"MCP tool '{self.name}' failed: {exc}")
            return ToolResult(success=False, output="", error=f"MCP tool '{self.name}' cannot run inside an active event loop")
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"MCP tool '{self.name}' failed: {exc}")

        return _runtime_result_to_legacy(self.name, result)


def _runtime_result_to_legacy(tool_name: str, result: RuntimeToolResult) -> ToolResult:
    output = str(result.output or "")
    metadata = result.metadata or {}
    if metadata.get("mcp_is_error") or metadata.get("mcp_error"):
        return ToolResult(
            success=False,
            output=output,
            error=str(metadata.get("mcp_error") or output or f"MCP tool '{tool_name}' returned an error"),
        )
    return ToolResult(success=True, output=output)


class MCPToolIntegration:
    """Connect configured MCP servers and expose their tools to agent/v2."""

    def __init__(
        self,
        raw_config: dict[str, Any] | None = None,
        *,
        server_configs: list[MCPServerConfig] | None = None,
        allow_tools: list[str] | None = None,
        deny_tools: list[str] | None = None,
    ) -> None:
        raw_config = raw_config or {}
        parsed_servers, parsed_allow, parsed_deny = _parse_raw_config(raw_config)
        self._server_configs = list(server_configs) if server_configs is not None else parsed_servers
        self._allow_tools = list(allow_tools) if allow_tools is not None else parsed_allow
        self._deny_tools = list(deny_tools) if deny_tools is not None else parsed_deny
        self._manager: SyncMCPToolManager | None = None
        self._runtime_tools: list[Any] = []
        self._tools: list[MCPRuntimeToolProxy] = []
        self._initialized = False

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def manager(self) -> SyncMCPToolManager | None:
        return self._manager

    @property
    def tools(self) -> list[MCPRuntimeToolProxy]:
        return list(self._tools)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self._tools)

    def initialize(self) -> None:
        if self._initialized:
            return
        if not self._server_configs:
            self._initialized = True
            return

        self._manager = SyncMCPToolManager()
        self._runtime_tools = self._manager.load_and_discover(self._server_configs)
        self._tools = [MCPRuntimeToolProxy(tool) for tool in self._runtime_tools]
        self._initialized = True
        logger.info("MCP integration initialized with %d tool(s)", len(self._tools))

    def get_tool_pool(self, builtin_tools: Iterable[Any]) -> list[Any]:
        if not self._initialized:
            raise RuntimeError("MCPToolIntegration not initialized. Call initialize() first.")
        mcp_tools = [tool for tool in self._tools if self._is_allowed(tool.name)]
        return assemble_tool_pool(builtin_tools, mcp_tools, deny_rules=self._deny_tools)

    def register_into(self, registry: ToolRegistry) -> None:
        for tool in self.get_tool_pool([]):
            if tool.name in registry:
                logger.warning("Skipping MCP tool %s because a built-in tool with the same name is registered", tool.name)
                continue
            registry.register(tool)

    def shutdown(self) -> None:
        if self._manager is not None:
            self._manager.close_all()
        self._manager = None
        self._runtime_tools.clear()
        self._tools.clear()
        self._initialized = False

    def __enter__(self) -> "MCPToolIntegration":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.shutdown()
        return False

    def _is_allowed(self, tool_name: str) -> bool:
        if self._allow_tools and not any(fnmatch(tool_name, pattern) for pattern in self._allow_tools):
            return False
        return not any(fnmatch(tool_name, pattern) for pattern in self._deny_tools)


def _parse_raw_config(raw_config: dict[str, Any]) -> tuple[list[MCPServerConfig], list[str], list[str]]:
    mcp_section = raw_config.get("mcp", raw_config)
    raw_servers = mcp_section.get("servers", mcp_section.get("mcpServers", mcp_section.get("mcp_servers", {})))
    allow_tools = _string_list(mcp_section.get("allow_tools", mcp_section.get("allowedTools", [])))
    deny_tools = _string_list(mcp_section.get("deny_tools", mcp_section.get("deniedTools", [])))

    servers: list[MCPServerConfig] = []
    if not isinstance(raw_servers, dict):
        return servers, allow_tools, deny_tools

    for name, raw in raw_servers.items():
        config = _parse_server_config(str(name), raw)
        if config is not None:
            servers.append(config)
    return servers, allow_tools, deny_tools


def _parse_server_config(name: str, raw: Any) -> MCPServerConfig | None:
    if not isinstance(raw, dict):
        return None
    transport = raw.get("transport", raw.get("type", "stdio"))
    if transport != "stdio":
        logger.warning("Skipping MCP server %s: unsupported transport %s", name, transport)
        return None
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        logger.warning("Skipping MCP server %s: missing command", name)
        return None
    args = raw.get("args", [])
    if not isinstance(args, list):
        logger.warning("Skipping MCP server %s: args must be a list", name)
        return None
    env = raw.get("env")
    if env is not None and not isinstance(env, dict):
        logger.warning("Skipping MCP server %s: env must be a dict", name)
        return None
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        logger.warning("Skipping MCP server %s: cwd must be a string", name)
        return None
    try:
        timeout_seconds = float(raw.get("timeout_seconds", raw.get("timeout", 60.0)))
    except (TypeError, ValueError):
        timeout_seconds = 60.0
    return MCPServerConfig(
        name=name,
        command=command,
        args=[str(arg) for arg in args],
        env={str(key): str(value) for key, value in env.items()} if env else None,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]
