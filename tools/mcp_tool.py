"""
tools/mcp_tool.py

McpToolWrapper — registers an MCP server tool in ToolRegistry.

CC format: mcp__{server}__{tool} (e.g. mcp__postgres__query).

Permission model:
  - Tools with destructiveHint → requires_user_interaction = True
  - Permission rules use mcp__server__tool or mcp__server__* format
  - No bare mcp__* wildcard unless explicitly configured
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, TYPE_CHECKING

from core.base import BaseTool, ToolResult
from core.types import ToolMetadata, ToolEffect, PathAccess

if TYPE_CHECKING:
    from mcp.protocol import McpClient

logger = logging.getLogger(__name__)

# CC: tool names must match ^[a-zA-Z0-9_-]{1,64}$
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _normalize_mcp_name(name: str) -> str:
    """Normalize a name for the mcp__ prefix format.

    Replaces invalid characters with underscores.
    CC reference: normalizeNameForMCP() in services/mcp/.
    """
    return _NAME_RE.sub("_", name)


class McpToolWrapper(BaseTool):
    """Wraps an MCP tool definition so it appears as a native tool.

    Registered in ToolRegistry with the name ``mcp__{server}__{tool}``.
    When executed, delegates to the MCP server via tools/call.
    """

    def __init__(self, server_name: str, tool_def: dict[str, Any],
                 client: "McpClient") -> None:
        self._server = server_name
        self._tool_def = tool_def
        self._client = client

        # CC format: mcp__{server}__{tool}
        safe_server = _normalize_mcp_name(server_name)
        safe_tool = _normalize_mcp_name(tool_def["name"])
        self._canonical_name = f"mcp__{safe_server}__{safe_tool}"

    # ── BaseTool interface ────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._canonical_name

    @property
    def description(self) -> str:
        desc = self._tool_def.get("description", "")
        # CC: truncate at 2048 chars (some servers dump huge descriptions)
        return desc[:2048]

    @property
    def parameters_schema(self) -> dict[str, Any]:
        schema = self._tool_def.get("inputSchema", {})
        if not schema:
            return {"type": "object", "properties": {}}
        return schema

    @property
    def metadata(self) -> ToolMetadata:
        annotations = self._tool_def.get("annotations", {})
        effects = frozenset({ToolEffect.UNKNOWN})

        if annotations.get("readOnlyHint"):
            effects = frozenset({ToolEffect.READ_WORKSPACE})
        elif annotations.get("destructiveHint"):
            effects = frozenset({ToolEffect.WRITE_WORKSPACE})

        return ToolMetadata(
            effects=effects,
            requires_user_interaction=annotations.get("destructiveHint", False),
        )

    def execute(self, params: dict[str, Any]) -> ToolResult:
        """Delegate to the MCP server via tools/call.

        CC pattern: MCP tool execution is always async (server round-trip).
        We detect the calling context and use the appropriate sync bridge.
        """
        try:
            try:
                loop = asyncio.get_running_loop()
                # Async context (e.g. inside an async HTTP handler) —
                # use run_coroutine_threadsafe to avoid deadlock
                future = asyncio.run_coroutine_threadsafe(
                    self._client.call_tool(self._tool_def["name"], params),
                    loop,
                )
                result = future.result(timeout=60)
            except RuntimeError:
                # No running loop (synchronous context, e.g. agent thread) —
                # create a new loop for this call
                result = asyncio.run(
                    self._client.call_tool(self._tool_def["name"], params)
                )
            return self._format_result(result)
        except Exception as e:
            logger.warning("MCP tool %s failed: %s", self._canonical_name, e)
            return ToolResult(success=False, output="", error=str(e))

    def _format_result(self, raw: dict[str, Any]) -> ToolResult:
        """Convert MCP tool result to ToolResult."""
        is_error = raw.get("isError", False)
        content = raw.get("content", [])

        # Extract text from content blocks
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    text_parts.append("[Image]")
                elif block.get("type") == "resource":
                    text_parts.append(str(block.get("resource", {})))

        output = "\n".join(text_parts) if text_parts else json.dumps(raw)
        return ToolResult(
            success=not is_error,
            output=output[:10000],  # Cap at 10K chars
            error="MCP tool returned isError" if is_error else "",
        )


# Import json at module level for _format_result
import json
