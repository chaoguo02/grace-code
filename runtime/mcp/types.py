"""Types for the runtime MCP bridge."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ── Per-transport idle timeout defaults (Claude Code alignment) ──
# stdio servers are long-lived processes; HTTP/SSE connections should be
# monitored more aggressively to detect hung remote servers.
DEFAULT_IDLE_TIMEOUT_STDIO = 1800.0   # 30 min
DEFAULT_IDLE_TIMEOUT_HTTP = 300.0     #  5 min


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for one MCP server (stdio, HTTP, SSE, or WebSocket).

    Transport-agnostic fields are always present. Transport-specific fields
    are validated by _parse_server_config() based on the ``type`` value.
    """

    name: str
    type: str = "stdio"            # "stdio" | "http" | "sse" | "ws"
    # ── stdio fields ──
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    # ── remote-transport fields ──
    url: str = ""
    headers: dict[str, str] | None = None
    # ── execution ──
    timeout_seconds: float = 60.0
    idle_timeout_seconds: float | None = None  # None = use transport default


@dataclass(frozen=True)
class MCPToolInfo:
    """Discovered MCP tool metadata."""

    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def runtime_name(self) -> str:
        return f"mcp__{slugify_mcp_name(self.server_name)}__{slugify_mcp_name(self.name)}"


@dataclass
class MCPServerConnection:
    """Connected MCP server state."""

    config: MCPServerConfig
    bridge: Any
    tools: list[MCPToolInfo] = field(default_factory=list)


def slugify_mcp_name(value: str) -> str:
    """Return a stable tool-name-safe slug."""
    slug = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "unnamed"


@dataclass(frozen=True)
class MCPToolProps:
    """Declarative MCP attachment for a runtime ConcreteTool.

    Replaces the previous pattern of dynamically injecting ``is_mcp``,
    ``always_load``, ``should_defer``, ``server_name`` etc. as bare
    attributes on the tool object.  The tool owns this payload as a
    single structured field so consumers don't need hasattr() duck-typing.
    """

    server_name: str = ""
    original_tool_name: str = ""
    is_deferred: bool = False
    always_load: bool = False
