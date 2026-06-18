"""
mcp_servers/demo_server.py

内置测试用 MCP Server — 用于验证 forge-agent 的 MCP 客户端全部功能路径。

暴露能力：
- Tools: echo, get_timestamp
- Resources: project://info, project://config-summary
- Resource Templates: project://files/{path}
- Prompts: summarize

启动方式：python -m mcp_servers.demo_server
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from mcp.server import FastMCP

mcp = FastMCP(
    "forge-agent-demo",
    instructions="A demo MCP server for testing forge-agent's MCP client capabilities.",
)

PROJECT_ROOT = Path(os.getcwd())


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def echo(message: str) -> str:
    """Echo back the input message. Useful for testing tool call round-trip."""
    return message


@mcp.tool()
def get_timestamp() -> str:
    """Return the current server timestamp in ISO format."""
    return datetime.now().isoformat()


# ---------------------------------------------------------------------------
# Static Resources
# ---------------------------------------------------------------------------

@mcp.resource("project://info")
def project_info() -> str:
    """Basic project information."""
    toml_path = PROJECT_ROOT / "pyproject.toml"
    if toml_path.exists():
        content = toml_path.read_text(encoding="utf-8")
        lines = []
        in_project = False
        for line in content.splitlines():
            if line.strip() == "[project]":
                in_project = True
                continue
            if in_project:
                if line.startswith("["):
                    break
                if "=" in line:
                    lines.append(line.strip())
        return "Project Info:\n" + "\n".join(lines)
    return "Project Info: pyproject.toml not found"


@mcp.resource("project://config-summary")
def config_summary() -> str:
    """Summary of current environment configuration relevant to forge-agent."""
    env_keys = [
        "FORGE_LLM_PROVIDER",
        "FORGE_LLM_MODEL",
        "FORGE_LLM_BASE_URL",
    ]
    lines = ["Config Summary:"]
    for key in env_keys:
        val = os.environ.get(key, "(not set)")
        lines.append(f"  {key} = {val}")
    lines.append(f"  CWD = {os.getcwd()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resource Templates
# ---------------------------------------------------------------------------

@mcp.resource("project://files/{path}")
def read_project_file(path: str) -> str:
    """Read a file from the project directory. Path is relative to project root."""
    target = (PROJECT_ROOT / path).resolve()
    # 安全检查：不允许读取项目根目录之外的文件
    if not str(target).startswith(str(PROJECT_ROOT.resolve())):
        return f"Error: path '{path}' is outside project root"
    if not target.is_file():
        return f"Error: file '{path}' not found"
    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > 10000:
            content = content[:10000] + "\n\n... (truncated at 10000 chars)"
        return content
    except UnicodeDecodeError:
        return f"Error: file '{path}' is not a text file"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@mcp.prompt()
def summarize(topic: str) -> list[dict]:
    """Generate a prompt asking for a summary of the given topic."""
    return [
        {
            "role": "user",
            "content": (
                f"Please provide a concise summary of the following topic:\n\n"
                f"**{topic}**\n\n"
                f"Include key points, important details, and any relevant context."
            ),
        }
    ]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
