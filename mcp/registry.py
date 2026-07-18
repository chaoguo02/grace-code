"""
mcp/registry.py

MCP server configuration registry — discovery, connection, tool cache.

CC reference: 7 config scopes (local/user/project/enterprise/managed/...).
We implement the core 3: user (~/.forge-agent/mcp.json), project (.mcp.json),
and local (.mcp.local.json).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.protocol import McpClient
from mcp.transport import HttpTransport, StdioTransport, McpTransport

logger = logging.getLogger(__name__)

# ── Config types ─────────────────────────────────────────────────────────


@dataclass
class McpServerConfig:
    """One MCP server definition loaded from config."""
    name: str
    transport: str = "stdio"  # "stdio" | "http"
    command: str = ""          # for stdio
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""              # for http
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    source: str = "project"    # "user" | "project" | "local"

    @classmethod
    def from_config(cls, name: str, raw: dict, source: str = "project") -> "McpServerConfig":
        """Parse a server entry from a JSON config dict."""
        if "command" in raw:
            return cls(
                name=name, transport="stdio",
                command=raw["command"],
                args=raw.get("args", []),
                env=raw.get("env", {}),
                source=source,
                enabled=not raw.get("disabled", False),
            )
        elif "url" in raw:
            return cls(
                name=name, transport="http",
                url=raw["url"],
                headers=raw.get("headers", {}),
                source=source,
                enabled=not raw.get("disabled", False),
            )
        else:
            raise ValueError(f"MCP server '{name}': must have 'command' (stdio) or 'url' (http)")


# ── Registry ─────────────────────────────────────────────────────────────


class McpRegistry:
    """Manage MCP server configs, connections, and tool discovery.

    Usage::

        registry = McpRegistry("/path/to/project")
        await registry.connect_all()
        tools = registry.get_all_tools()  # {server_name: [tool_def, ...]}
    """

    def __init__(self, project_root: str) -> None:
        self._project_root = Path(project_root)
        self._servers: dict[str, McpServerConfig] = {}
        self._clients: dict[str, McpClient] = {}
        self._tools_cache: dict[str, list[dict[str, Any]]] = {}
        self._connected = False
        self._load_configs()

    # ── Config loading ────────────────────────────────────────────────

    def _load_configs(self) -> None:
        """Load MCP server configs from user/project/local scopes.

        CC-aligned loading order (ascending priority):
          1. ~/.forge-agent/mcp.json       (user, lowest priority)
          2. <project>/.mcp.json           (project)
          3. <project>/.mcp.local.json     (local, highest priority)
        """
        config_paths = [
            (Path.home() / ".forge-agent" / "mcp.json", "user"),
            (self._project_root / ".mcp.json", "project"),
            (self._project_root / ".mcp.local.json", "local"),
        ]

        for path, source in config_paths:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                servers = data.get("mcpServers", {})
                for name, raw in servers.items():
                    try:
                        cfg = McpServerConfig.from_config(name, raw, source)
                        # Higher-priority source replaces lower
                        if name in self._servers:
                            existing_source = self._servers[name].source
                            from hitl.permission_rule import RULE_SOURCE_PRIORITY
                            if RULE_SOURCE_PRIORITY.get(source, 0) <= RULE_SOURCE_PRIORITY.get(existing_source, 0):
                                continue  # lower priority, skip
                        self._servers[name] = cfg
                    except ValueError as e:
                        logger.warning("Skipping MCP server '%s': %s", name, e)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load MCP config %s: %s", path, e)

        logger.info("Loaded %d MCP server configs", len(self._servers))

    # ── Connection management ─────────────────────────────────────────

    async def connect_all(self) -> None:
        """Connect to all enabled servers."""
        for name, cfg in self._servers.items():
            if not cfg.enabled:
                continue
            transport = self._build_transport(cfg)
            client = McpClient(transport, server_name=name)
            try:
                await client.connect()
                caps = await client.initialize()
                logger.info("MCP server connected: %s (tools=%s)", name,
                            caps.get("tools", {}).get("listChanged", "?"))
                self._clients[name] = client
            except Exception as e:
                logger.warning("MCP server '%s' connection failed: %s", name, e)

        self._connected = True

    async def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for name, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        self._connected = False

    def _build_transport(self, cfg: McpServerConfig) -> McpTransport:
        """Create the appropriate transport for a server config."""
        if cfg.transport == "stdio":
            return StdioTransport(cfg.command, cfg.args, cfg.env or None)
        elif cfg.transport == "http":
            return HttpTransport(cfg.url, cfg.headers or None)
        else:
            raise ValueError(f"Unknown transport: {cfg.transport}")

    # ── Tool discovery ────────────────────────────────────────────────

    async def fetch_tools(self, server_name: str) -> list[dict[str, Any]]:
        """Fetch and cache tools from a specific server."""
        client = self._clients.get(server_name)
        if client is None:
            logger.warning("MCP: no client for server '%s'", server_name)
            return []

        try:
            tools = await client.list_tools()
            self._tools_cache[server_name] = tools
            logger.info("MCP server '%s': discovered %d tools", server_name, len(tools))
            return tools
        except Exception as e:
            logger.warning("MCP server '%s': tools/list failed: %s", server_name, e)
            return []

    async def fetch_all_tools(self) -> None:
        """Fetch tools from all connected servers."""
        for name in list(self._clients.keys()):
            await self.fetch_tools(name)

    def get_all_tools(self) -> dict[str, list[dict[str, Any]]]:
        """Return all cached tools, keyed by server name."""
        return dict(self._tools_cache)

    def get_client(self, server_name: str) -> McpClient | None:
        """Get the connected client for a server."""
        return self._clients.get(server_name)

    # ── Introspection ─────────────────────────────────────────────────

    @property
    def server_names(self) -> list[str]:
        return list(self._servers.keys())

    @property
    def connected_servers(self) -> list[str]:
        return list(self._clients.keys())

    @property
    def total_tools(self) -> int:
        return sum(len(t) for t in self._tools_cache.values())
