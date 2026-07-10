"""Allow/deny policy for runtime MCP servers."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from runtime.mcp.types import MCPServerConfig


@dataclass(frozen=True)
class MCPServerPolicy:
    """Name and command based MCP server policy."""

    allowed_mcp_servers: list[str] = field(default_factory=list)
    denied_mcp_servers: list[str] = field(default_factory=list)


def is_mcp_server_allowed(config: MCPServerConfig, policy: MCPServerPolicy | None = None) -> bool:
    """Return whether a server is allowed. Deny rules win."""
    if policy is None:
        return True

    if _matches_any(config, policy.denied_mcp_servers):
        return False

    if not policy.allowed_mcp_servers:
        return True

    return _matches_any(config, policy.allowed_mcp_servers)


def _matches_any(config: MCPServerConfig, patterns: list[str]) -> bool:
    return any(_matches(config, pattern) for pattern in patterns)


def _matches(config: MCPServerConfig, pattern: str) -> bool:
    candidate = pattern.strip()
    if not candidate:
        return False

    command_line = " ".join([config.command, *config.args]).strip()
    return (
        fnmatchcase(config.name, candidate)
        or fnmatchcase(command_line, candidate)
        or config.name == candidate
        or command_line == candidate
    )
