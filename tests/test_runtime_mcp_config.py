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
    path = tmp_path / ".mcp.json"
    _write_json(path, {
        "mcpServers": {
            "http_server": {"type": "http", "url": "https://example.com"},
            "missing_command": {"args": ["x"]},
            "bad_args": {"command": "python", "args": "not-list"},
            "valid": {"type": "stdio", "command": "python", "args": ["-m", "server"]},
        }
    })

    result = load_mcp_config(
        project_dir=tmp_path,
        global_config_path=tmp_path / "missing-global.json",
        project_config_path=path,
    )

    assert [server.name for server in result.servers] == ["valid"]


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
