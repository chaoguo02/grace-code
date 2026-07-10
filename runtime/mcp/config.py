"""Configuration loading for runtime MCP servers."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.mcp.allowlist import MCPServerPolicy, is_mcp_server_allowed
from runtime.mcp.types import MCPServerConfig

DEFAULT_GLOBAL_MCP_CONFIG = Path.home() / ".forge-agent" / "mcp.json"
DEFAULT_PROJECT_MCP_CONFIG = Path(".mcp.json")
_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class MCPConfigLoadResult:
    """Loaded and merged MCP config."""

    servers: list[MCPServerConfig] = field(default_factory=list)
    allowed_mcp_servers: list[str] = field(default_factory=list)
    denied_mcp_servers: list[str] = field(default_factory=list)
    sources: list[Path] = field(default_factory=list)

    @property
    def policy(self) -> MCPServerPolicy:
        return MCPServerPolicy(
            allowed_mcp_servers=list(self.allowed_mcp_servers),
            denied_mcp_servers=list(self.denied_mcp_servers),
        )


def load_mcp_config(
    *,
    project_dir: str | Path | None = None,
    global_config_path: str | Path | None = None,
    project_config_path: str | Path | None = None,
) -> MCPConfigLoadResult:
    """Load global + project MCP config, with project overriding global by name."""
    project_root = Path(project_dir) if project_dir is not None else Path.cwd()
    paths = [
        Path(global_config_path) if global_config_path is not None else DEFAULT_GLOBAL_MCP_CONFIG,
        Path(project_config_path) if project_config_path is not None else project_root / DEFAULT_PROJECT_MCP_CONFIG,
    ]

    merged_servers: dict[str, MCPServerConfig] = {}
    allowed: list[str] = []
    denied: list[str] = []
    sources: list[Path] = []

    for path in paths:
        if not path.exists():
            continue
        data = _read_json(path)
        if data is None:
            continue
        sources.append(path)

        allowed = _string_list(data.get("allowedMcpServers", allowed))
        denied = _string_list(data.get("deniedMcpServers", denied))

        raw_servers = data.get("mcpServers", {})
        if not isinstance(raw_servers, dict):
            continue
        for name, raw in raw_servers.items():
            config = _parse_server_config(str(name), raw, base_dir=path.parent)
            if config is not None:
                merged_servers[config.name] = config

    return MCPConfigLoadResult(
        servers=list(merged_servers.values()),
        allowed_mcp_servers=allowed,
        denied_mcp_servers=denied,
        sources=sources,
    )


def load_allowed_mcp_server_configs(**kwargs: Any) -> list[MCPServerConfig]:
    """Load config and apply allow/deny policy."""
    result = load_mcp_config(**kwargs)
    return [
        config for config in result.servers
        if is_mcp_server_allowed(config, result.policy)
    ]


def expand_mcp_env_vars(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} recursively in config values."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace_env_var, value)
    if isinstance(value, list):
        return [expand_mcp_env_vars(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_mcp_env_vars(item) for key, item in value.items()}
    return value


def _replace_env_var(match: re.Match[str]) -> str:
    name = match.group(1)
    default = match.group(2)
    current = os.environ.get(name)
    if current is not None:
        return current
    if default is not None:
        return default
    return ""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return expand_mcp_env_vars(data)


def _parse_server_config(name: str, raw: Any, *, base_dir: Path) -> MCPServerConfig | None:
    if not isinstance(raw, dict):
        return None

    server_type = raw.get("type", "stdio")
    if server_type != "stdio":
        return None

    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        return None

    args = raw.get("args", [])
    if not isinstance(args, list):
        return None

    env = raw.get("env")
    if env is not None and not isinstance(env, dict):
        return None

    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return None

    timeout_raw = raw.get("timeout_seconds", raw.get("timeout", 60.0))
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError):
        timeout_seconds = 60.0

    resolved_cwd = cwd
    if resolved_cwd and not Path(resolved_cwd).is_absolute():
        resolved_cwd = str(base_dir / resolved_cwd)

    return MCPServerConfig(
        name=name,
        command=command,
        args=[str(arg) for arg in args],
        env={str(key): str(value) for key, value in env.items()} if env else None,
        cwd=resolved_cwd,
        timeout_seconds=timeout_seconds,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]
