from __future__ import annotations

from agent.policy_registry import PolicyAwareToolRegistry
from tools.base import ToolResult


_DELEGATION_BLOCK_PREFIX = "BLOCKED_BY_DELEGATION_POLICY:"


class DynamicPolicyAwareToolRegistry(PolicyAwareToolRegistry):
    """Policy-aware registry with a mutable deny set for runtime tool visibility."""

    def __init__(self, *args, dynamic_denied_tools: set[str] | frozenset[str] | None = None, **kwargs) -> None:
        self._dynamic_denied_tools = frozenset(dynamic_denied_tools or ())
        super().__init__(*args, **kwargs)

    @property
    def dynamic_denied_tools(self) -> frozenset[str]:
        return self._dynamic_denied_tools

    def set_dynamic_denied_tools(self, denied_tools: set[str] | frozenset[str]) -> None:
        self._dynamic_denied_tools = frozenset(denied_tools)
        self._refresh_visible_tools()

    def _refresh_visible_tools(self) -> None:
        self._tools.clear()
        for name, tool in self._base._tools.items():
            if self._is_tool_visible(name):
                self._tools[name] = tool

    def _is_tool_visible(self, name: str) -> bool:
        if name in self._dynamic_denied_tools:
            return False
        return super()._is_tool_visible(name)

    def execute_tool(self, name: str, params: dict[str, object], thought: str = "") -> ToolResult:
        if name in self._dynamic_denied_tools:
            available = ", ".join(self.tool_names) or "none"
            guidance = (
                "BLOCKED: You are in child-delegation mode and cannot perform broad exploration yourself.\n"
                "DO NOT retry this tool.\n"
                "Instead, you MUST do one of the following:\n"
                "1. Dispatch a new, more specific child task based on the previous partial or failed child result.\n"
                "2. If the previous child summaries already give enough context, synthesize the final answer now.\n"
                f"Available tools now: {available}"
            )
            return ToolResult(
                success=False,
                output=guidance,
                error=f"{_DELEGATION_BLOCK_PREFIX} {name}",
            )
        return super().execute_tool(name, params, thought=thought)
