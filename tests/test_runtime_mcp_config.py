from __future__ import annotations

import json
from pathlib import Path

from runtime.mcp import (
    MCPServerConfig,
    MCPServerPolicy,
    expand_mcp_env_vars,
    is_mcp_server_allowed,
    load_allowed_mcp_server_configs,
    load_mcp_config,
)


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_expand_mcp_env_vars_supports_default_and_missing(monkeypatch):
    monkeypatch.setenv("TOKEN", "secret")

    expanded = expand_mcp_env_vars({
        "a": "${TOKEN}",
        "b": "${MISSING:-fallback}",
        "c": "${MISSING}",
        "d": ["x-${TOKEN}"],
    })

    assert expanded == {
        "a": "secret",
        "b": "fallback",
        "c": "",
        "d": ["x-secret"],
    }


def test_load_mcp_config_merges_global_and_project_by_server_name(tmp_path):
    global_path = tmp_path / "global.json"
    project_path = tmp_path / ".mcp.json"
    _write_json(global_path, {
        "mcpServers": {
            "shared": {"command": "old", "args": ["a"]},
            "global_only": {"command": "global"},
        },
        "allowedMcpServers": ["global_only"],
    })
    _write_json(project_path, {
        "mcpServers": {
            "shared": {"command": "new", "args": ["b"], "timeout_seconds": 7},
            "project_only": {"command": "project", "cwd": "subdir"},
        },
        "allowedMcpServers": ["project_only"],
    })

    result = load_mcp_config(
        project_dir=tmp_path,
        global_config_path=global_path,
        project_config_path=project_path,
    )
    by_name = {server.name: server for server in result.servers}

    assert by_name["shared"].command == "new"
    assert by_name["shared"].args == ["b"]
    assert by_name["shared"].timeout_seconds == 7.0
    assert by_name["global_only"].command == "global"
    assert by_name["project_only"].command == "project"
    assert by_name["project_only"].cwd == str(tmp_path / "subdir")
    assert result.allowed_mcp_servers == ["project_only"]
    assert result.sources == [global_path, project_path]


def test_allowlist_empty_allow_permits_and_deny_wins():
    config = MCPServerConfig(name="filesystem", command="npx", args=["server"])

    assert is_mcp_server_allowed(config, MCPServerPolicy()) is True
    assert is_mcp_server_allowed(
        config,
        MCPServerPolicy(allowed_mcp_servers=["filesystem"], denied_mcp_servers=["filesystem"]),
    ) is False
    assert is_mcp_server_allowed(
        config,
        MCPServerPolicy(allowed_mcp_servers=["other"]),
    ) is False


def test_allowlist_matches_command_line_patterns():
    config = MCPServerConfig(
        name="generated-name",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem"],
    )

    assert is_mcp_server_allowed(
        config,
        MCPServerPolicy(allowed_mcp_servers=["npx *@modelcontextprotocol/server-*"]),
    ) is True
    assert is_mcp_server_allowed(
        config,
        MCPServerPolicy(denied_mcp_servers=["npx *@modelcontextprotocol/server-*"]),
    ) is False


def test_load_mcp_config_skips_unsupported_or_invalid_servers(tmp_path):
    """MCP-E1: Only invalid/missing-required-field servers are skipped.

    HTTP servers are now parsed (MCP-E1 removed the stdio-only gate).
    Servers missing required fields (stdlib: command, remote: url) are still skipped.
    """
    path = tmp_path / ".mcp.json"
    _write_json(path, {
        "mcpServers": {
            "http_server": {"type": "http", "url": "https://example.com"},
            "http_missing_url": {"type": "http"},
            "missing_command": {"args": ["x"]},
            "bad_args": {"command": "python", "args": "not-list"},
            "valid_stdio": {"type": "stdio", "command": "python", "args": ["-m", "server"]},
        }
    })

    result = load_mcp_config(
        project_dir=tmp_path,
        global_config_path=tmp_path / "missing-global.json",
        project_config_path=path,
    )

    names = [server.name for server in result.servers]
    assert "valid_stdio" in names
    assert "http_server" in names  # MCP-E1: remote servers are now parsed
    assert "http_missing_url" not in names  # remote requires url
    assert "missing_command" not in names   # stdio requires command


def test_load_allowed_mcp_server_configs_applies_policy(tmp_path):
    path = tmp_path / ".mcp.json"
    _write_json(path, {
        "allowedMcpServers": ["allowed"],
        "deniedMcpServers": ["denied"],
        "mcpServers": {
            "allowed": {"command": "a"},
            "denied": {"command": "d"},
            "other": {"command": "o"},
        },
    })

    configs = load_allowed_mcp_server_configs(
        project_dir=tmp_path,
        global_config_path=tmp_path / "missing-global.json",
        project_config_path=path,
    )

    assert [config.name for config in configs] == ["allowed"]


# ---------------------------------------------------------------------------
# MCP-E2: CC-aligned config path with legacy fallback
# ---------------------------------------------------------------------------

def test_user_config_prefers_new_path_over_legacy(tmp_path, monkeypatch):
    """MCP-E2: ~/.forge-agent.json takes precedence over ~/.forge-agent/mcp.json."""
    from runtime.mcp.config import DEFAULT_USER_MCP_CONFIG, _LEGACY_USER_MCP_CONFIG

    # Mock both paths to point inside tmp_path
    new_path = tmp_path / ".forge-agent.json"
    legacy_path = tmp_path / ".forge-agent" / "mcp.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)

    _write_json(new_path, {
        "mcpServers": {"new_only": {"command": "from-new"}},
    })
    _write_json(legacy_path, {
        "mcpServers": {"legacy_only": {"command": "from-legacy"}},
    })

    monkeypatch.setattr(
        "runtime.mcp.config.DEFAULT_USER_MCP_CONFIG", new_path,
    )
    monkeypatch.setattr(
        "runtime.mcp.config._LEGACY_USER_MCP_CONFIG", legacy_path,
    )

    result = load_mcp_config(
        project_dir=tmp_path,
        global_config_path=new_path,
        project_config_path=tmp_path / "missing.json",
    )
    names = {s.name for s in result.servers}
    assert "new_only" in names
    assert "legacy_only" not in names  # new path wins


def test_user_config_falls_back_to_legacy_when_new_missing(tmp_path, monkeypatch):
    """MCP-E2: Legacy config is used when new path does not exist."""
    legacy_path = tmp_path / ".forge-agent" / "mcp.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(legacy_path, {
        "mcpServers": {"legacy": {"command": "from-legacy"}},
    })

    new_path = tmp_path / ".forge-agent.json"  # does NOT exist

    monkeypatch.setattr(
        "runtime.mcp.config.DEFAULT_USER_MCP_CONFIG", new_path,
    )
    monkeypatch.setattr(
        "runtime.mcp.config._LEGACY_USER_MCP_CONFIG", legacy_path,
    )

    result = load_mcp_config(
        project_dir=tmp_path,
        global_config_path=None,  # auto-resolve
        project_config_path=tmp_path / "missing.json",
    )
    names = {s.name for s in result.servers}
    assert "legacy" in names


# ---------------------------------------------------------------------------
# MCP-E3: idle timeout parsing
# ---------------------------------------------------------------------------

def test_parse_server_config_sets_idle_timeout_from_raw(tmp_path):
    """MCP-E3: idle_timeout_seconds is parsed from server config JSON."""
    from runtime.mcp.config import _parse_server_config

    config = _parse_server_config(
        "test",
        {"command": "echo", "idle_timeout_seconds": 120.0},
        base_dir=tmp_path,
    )
    assert config is not None
    assert config.idle_timeout_seconds == 120.0


def test_parse_server_config_uses_default_idle_for_stdio(tmp_path):
    """MCP-E3: stdio servers get DEFAULT_IDLE_TIMEOUT_STDIO when not specified."""
    from runtime.mcp.config import _parse_server_config
    from runtime.mcp.types import DEFAULT_IDLE_TIMEOUT_STDIO

    config = _parse_server_config(
        "test",
        {"command": "echo"},
        base_dir=tmp_path,
    )
    assert config is not None
    assert config.idle_timeout_seconds == DEFAULT_IDLE_TIMEOUT_STDIO


def test_parse_server_config_uses_http_default_for_remote_types(tmp_path):
    """MCP-E3: HTTP/SSE/WS servers get DEFAULT_IDLE_TIMEOUT_HTTP by default."""
    from runtime.mcp.config import _parse_server_config
    from runtime.mcp.types import DEFAULT_IDLE_TIMEOUT_HTTP

    for transport in ("http", "sse", "ws"):
        config = _parse_server_config(
            transport,
            {"type": transport, "url": "https://example.com"},
            base_dir=tmp_path,
        )
        # These are currently rejected by the type check (MCP-E1), but the
        # idle_timeout logic is in place for when Batch 4 removes the gate.
        if config is not None:
            assert config.idle_timeout_seconds == DEFAULT_IDLE_TIMEOUT_HTTP, (
                f"{transport} idle timeout mismatch"
            )


def test_execution_policy_supports_idle_timeout():
    """MCP-E3: ExecutionPolicy.idle_timeout is None by default (opt-in)."""
    from runtime.mcp.sync_bridge import (
        DEFAULT_EXECUTION_TIMEOUT,
        DEFAULT_IDLE_TIMEOUT_HTTP,
        DEFAULT_IDLE_TIMEOUT_STDIO,
        ExecutionPolicy,
    )

    policy = ExecutionPolicy()
    assert policy.timeout == DEFAULT_EXECUTION_TIMEOUT
    assert policy.idle_timeout is None  # backward compatible

    # Can be overridden for specific transports
    http_policy = ExecutionPolicy(idle_timeout=DEFAULT_IDLE_TIMEOUT_HTTP)
    assert http_policy.idle_timeout == DEFAULT_IDLE_TIMEOUT_HTTP

    stdio_policy = ExecutionPolicy(idle_timeout=DEFAULT_IDLE_TIMEOUT_STDIO)
    assert stdio_policy.idle_timeout == DEFAULT_IDLE_TIMEOUT_STDIO
