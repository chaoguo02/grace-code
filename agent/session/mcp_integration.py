"""MCP integration helpers for the session runtime."""

from __future__ import annotations

import logging
import json
from enum import Enum
from fnmatch import fnmatch
from typing import Any, Iterable

from agent.mcp import MCPServerConfig, SyncMCPToolManager, assemble_tool_pool
from core.base import BaseTool, ToolRegistry

logger = logging.getLogger(__name__)


class ToolLoadingMode(str, Enum):
    TST = "tst"
    TST_AUTO = "tst-auto"
    STANDARD = "standard"


class MCPToolIntegration:
    """Connect configured MCP servers and expose their tools to agents."""

    def __init__(
        self,
        raw_config: dict[str, Any] | None = None,
        *,
        server_configs: list[MCPServerConfig] | None = None,
        allow_tools: list[str] | None = None,
        deny_tools: list[str] | None = None,
        loading_mode: ToolLoadingMode | str | None = None,
        context_window: int = 128_000,
    ) -> None:
        raw_config = raw_config or {}
        parsed_servers, parsed_allow, parsed_deny = _parse_raw_config(raw_config)
        self._server_configs = list(server_configs) if server_configs is not None else parsed_servers
        self._allow_tools = list(allow_tools) if allow_tools is not None else parsed_allow
        self._deny_tools = list(deny_tools) if deny_tools is not None else parsed_deny
        raw_mode = loading_mode or raw_config.get(
            "tool_loading_mode",
            raw_config.get("mcp_tool_loading_mode", ToolLoadingMode.TST.value),
        )
        if isinstance(raw_mode, ToolLoadingMode):
            self._loading_mode = raw_mode
        else:
            try:
                self._loading_mode = ToolLoadingMode(str(raw_mode))
            except ValueError:
                self._loading_mode = ToolLoadingMode.TST
        self._context_window = max(1, int(context_window))
        self._manager: SyncMCPToolManager | None = None
        self._tools: list[BaseTool] = []
        self._registry: ToolRegistry | None = None
        self._initialized = False

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def server_tools(self) -> dict[str, list[str]]:
        """Map server name → tool names for resolving agent-scoped mcpServers."""
        if self._manager is not None:
            return self._manager.server_tools
        return {}

    @property
    def failed_servers(self) -> dict[str, str]:
        if self._manager is not None:
            return self._manager.failed_servers
        return {}

    @property
    def manager(self) -> SyncMCPToolManager | None:
        return self._manager

    @property
    def tools(self) -> list[BaseTool]:
        return list(self._tools)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self._tools)

    def deferred_tool_descriptors(self) -> list[dict[str, str]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "server": str(
                    getattr(
                        getattr(tool, "mcp_props", None),
                        "server_name",
                        "",
                    ),
                ),
            }
            for tool in self._tools
            if bool(getattr(tool, "should_defer", False))
        ]

    def activate_tools(self, names: set[str]) -> list[str]:
        activated: list[str] = []
        for tool in self._tools:
            if tool.name not in names:
                continue
            tool.always_load = True
            tool.should_defer = False
            activated.append(tool.name)
        return activated

    def activate_servers(self, server_names: set[str]) -> list[str]:
        """Activate all deferred tools owned by the named MCP servers."""
        tool_names = {
            tool_name
            for server_name in server_names
            for tool_name in self.server_tools.get(server_name, ())
        }
        return self.activate_tools(tool_names)

    def connection_errors(self) -> dict[str, str]:
        if self._manager is None:
            return {}
        errors = dict(self._manager.failed_servers)
        for name, bridge in self._manager.bridges.items():
            if not bridge.is_connected:
                errors.setdefault(name, "not connected")
        return errors

    def initialize(self) -> None:
        if self._initialized:
            return
        if not self._server_configs:
            self._initialized = True
            return

        self._manager = SyncMCPToolManager()
        self._manager.set_tools_changed_callback(
            self._replace_server_tools,
        )
        self._tools = self._manager.load_and_discover(self._server_configs)
        self._apply_loading_mode()
        self._initialized = True
        logger.info("MCP integration initialized with %d tool(s)", len(self._tools))

    def get_tool_pool(self, builtin_tools: Iterable[Any]) -> list[Any]:
        if not self._initialized:
            raise RuntimeError("MCPToolIntegration not initialized. Call initialize() first.")
        mcp_tools = [tool for tool in self._tools if self._is_allowed(tool.name)]
        return assemble_tool_pool(builtin_tools, mcp_tools, deny_rules=self._deny_tools)

    def register_into(self, registry: ToolRegistry) -> None:
        self._registry = registry
        pool = self.get_tool_pool(registry.tools)
        for name in tuple(registry.tool_names):
            registry.unregister(name)
        registry.register_many(pool)

    def connect_agent_servers(self, spec) -> list[str]:
        """Connect MCP servers declared in an agent's mcpServers frontmatter.
        Returns list of newly registered tool names.
        CC-aligned: inline definitions connect when agent starts.
        """
        if not spec.mcp_servers:
            return []
        if not self._initialized:
            self.initialize()
        new_tools: list[str] = []
        for entry in spec.mcp_servers:
            if isinstance(entry, dict):
                for name, config in entry.items():
                    if not isinstance(config, dict):
                        continue
                    server_config = _parse_server_config(name, config)
                    if server_config is None:
                        continue
                    # Add to manager and discover tools
                    if self._manager is not None:
                        try:
                            runtime_tools = self._manager.load_and_discover([server_config])
                            for tool in runtime_tools:
                                self._tools.append(tool)
                                new_tools.append(tool.name)
                                logger.info("Connected agent-scoped MCP server '%s' (tool: %s)", name, tool.name)
                            self._apply_loading_mode()
                        except Exception as exc:
                            logger.warning("Failed to connect agent-scoped MCP server '%s': %s", name, exc)
        return new_tools

    def disconnect_agent_servers(self, spec) -> None:
        """Disconnect agent-scoped MCP servers when agent finishes."""
        if not spec.mcp_servers or self._manager is None:
            return
        server_names: set[str] = set()
        for entry in spec.mcp_servers:
            if isinstance(entry, dict):
                server_names.update(entry.keys())
        if not server_names:
            return
        for server_name in server_names:
            self._manager.close_server(server_name)
        self._tools = [
            tool for tool in self._tools
            if getattr(
                getattr(tool, "mcp_props", None),
                "server_name",
                "",
            ) not in server_names
        ]
        logger.info("Disconnected MCP servers: %s", sorted(server_names))

    def refresh_tools(self) -> list[str]:
        """Re-discover tools from all configured MCP servers (CC: tools/list_changed)."""
        if self._manager is None:
            return []
        old_names = {t.name for t in self._tools}
        self._manager.close_all()
        self._manager = SyncMCPToolManager()
        self._manager.set_tools_changed_callback(
            self._replace_server_tools,
        )
        self._tools = self._manager.load_and_discover(self._server_configs)
        self._apply_loading_mode()
        new_names = {t.name for t in self._tools}
        added = new_names - old_names
        removed = old_names - new_names
        if added or removed:
            logger.info("MCP tools refreshed: +%d -%d", len(added), len(removed))
        return list(added)

    def shutdown(self) -> None:
        self.close()

    def close(self, timeout: float = 5.0) -> None:
        if self._manager is not None:
            self._manager.close_all(
                drain_timeout=timeout,
                close_timeout=timeout,
            )
        self._manager = None
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

    def _apply_loading_mode(self) -> None:
        deferred = self._loading_mode is ToolLoadingMode.TST
        if self._loading_mode is ToolLoadingMode.TST_AUTO:
            schema_chars = sum(
                len(json.dumps(tool.parameters_schema, sort_keys=True))
                + len(tool.description)
                for tool in self._tools
            )
            estimated_tokens = max(1, schema_chars // 4)
            deferred = estimated_tokens > int(self._context_window * 0.10)
        for tool in self._tools:
            props = getattr(tool, "mcp_props", None)
            if props is None:
                continue
            props.always_load = not deferred
            props.is_deferred = deferred

    def _replace_server_tools(
        self,
        server_name: str,
        tools: list[BaseTool],
    ) -> None:
        stale = [
            tool
            for tool in self._tools
            if getattr(
                getattr(tool, "mcp_props", None),
                "server_name",
                "",
            ) == server_name
        ]
        self._tools = [
            tool for tool in self._tools if tool not in stale
        ]
        self._tools.extend(tools)
        self._apply_loading_mode()
        if self._registry is not None:
            for tool in stale:
                self._registry.unregister(tool.name)
            for tool in tools:
                if tool.name not in self._registry:
                    self._registry.register(tool)


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
    if transport not in ("stdio", "http", "sse", "ws"):
        logger.warning("Skipping MCP server %s: unsupported transport %s", name, transport)
        return None
    if transport == "stdio":
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            logger.warning("Skipping MCP server %s: missing command for stdio", name)
            return None
    else:
        command = raw.get("command") or ""
    url = raw.get("url", "")
    if transport in ("http", "sse", "ws") and not url:
        logger.warning("Skipping MCP server %s: missing url for %s transport", name, transport)
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
    headers_raw = raw.get("headers", {})
    if isinstance(headers_raw, dict):
        headers = {str(k): str(v) for k, v in headers_raw.items()}
    else:
        headers = None
    return MCPServerConfig(
        name=name,
        type=transport,
        command=command,
        args=[str(a) for a in args],
        url=url,
        headers=headers,
        env={str(key): str(value) for key, value in env.items()} if env else None,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]
