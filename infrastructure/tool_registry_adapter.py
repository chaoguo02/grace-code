"""T18: ToolRegistryPort adapter wrapping existing ToolRegistry.

Implements register/unregister/resolve/list_names/metadata_for
using the core.base.ToolRegistry API.  Thread-safe.
"""

from __future__ import annotations

import threading


class ToolRegistryAdapter:
    """Adapts core.base.ToolRegistry → runtime_core.ports.ToolRegistryPort.

    Thread-safe wrapper that delegates to the existing BaseTool registry.
    Handles alias resolution via ToolRegistry.resolve_name().
    """

    def __init__(self, registry=None):
        self._registry = registry  # core.base.ToolRegistry instance
        self._lock = threading.Lock()

    def register(self, tool) -> None:
        with self._lock:
            if self._registry is not None:
                self._registry.register(tool)

    def unregister(self, name: str) -> None:
        with self._lock:
            if self._registry is not None and hasattr(self._registry, '_tools'):
                self._registry._tools.pop(name, None)

    def resolve(self, name: str) -> object | None:
        if self._registry is None:
            return None
        canonical = name
        if hasattr(self._registry, 'resolve_name'):
            canonical = self._registry.resolve_name(name) or name
        tools = getattr(self._registry, '_tools', {})
        return tools.get(canonical)

    def list_names(self) -> list[str]:
        if self._registry is None:
            return []
        if hasattr(self._registry, 'tool_names'):
            return list(self._registry.tool_names)
        return list(getattr(self._registry, '_tools', {}).keys())

    def metadata_for(self, name: str):
        tool = self.resolve(name)
        if tool is None:
            return None
        from runtime_core.tool_scheduler import ToolMetadata
        return ToolMetadata.from_base_tool(tool)
