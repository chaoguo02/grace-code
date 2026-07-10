"""
Tool Registry — aligned with Claude Code src/tools.ts getAllBaseTools().

Source basis:
  D: dynamic tool registry
  A: stable alphabetical ordering for prompt cache friendliness
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.tool import ConcreteTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry for enabled runtime tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ConcreteTool] = {}

    def register(self, tool: ConcreteTool) -> None:
        """Register an enabled tool, skipping disabled tools."""
        if not tool.is_enabled():
            logger.debug("Tool %s is disabled, skipping registration", tool.name)
            return

        if tool.name in self._tools:
            logger.warning("Tool %s already registered, overwriting", tool.name)

        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> ConcreteTool | None:
        """Find a tool by name."""
        return self._tools.get(name)

    def find_by_name(self, name: str) -> ConcreteTool | None:
        """Alias aligned with Claude Code findToolByName naming."""
        return self._tools.get(name)

    def list_tools(self) -> list[ConcreteTool]:
        """Return enabled tools sorted by name for stable prompt cache input."""
        enabled = [tool for tool in self._tools.values() if tool.is_enabled()]
        enabled.sort(key=lambda tool: tool.name)
        return enabled

    def get_api_definitions(self) -> list[dict]:
        """Return sorted API tool definitions."""
        return [tool.to_api_definition() for tool in self.list_tools()]

    @property
    def count(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __repr__(self) -> str:
        names = sorted(self._tools.keys())
        return f"ToolRegistry({names})"
