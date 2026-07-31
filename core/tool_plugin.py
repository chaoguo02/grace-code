"""Tool plugin protocol — runtime tool registration without code changes.

Session configs can declare ``plugins`` entries.  Each entry names a
plugin class (registered via setuptools entry_points, Python dotted-path,
or direct import) and passes ``config`` dict.  The plugin validates config,
produces ``ToolMetadata``, and creates ``BaseTool`` instances on demand.

This is the contract.  Concrete plugin implementations live alongside
the tools they wrap (e.g. in ``tools/`` or external packages).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class ToolPlugin(Protocol):
    """Protocol for runtime tool registration without code changes.

    Implement this protocol in any module and register it via session
    config ``plugins``.  ``ToolRegistry.register_plugin()`` calls
    ``validate_config()`` first, then ``create_tool()`` to produce
    a ``BaseTool`` instance which is registered normally.
    """

    def metadata(self) -> "ToolPluginMetadata":
        """Return static metadata describing this plugin."""
        ...

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate *config* before tool creation. Raises on invalid input."""
        ...

    def create_tool(self, config: dict[str, Any]) -> Any:
        """Create a ``BaseTool`` instance from validated *config*."""
        ...


@dataclass(frozen=True)
class ToolPluginMetadata:
    """Static metadata for one tool plugin."""

    name: str
    version: str
    description: str = ""
    author: str = ""
    homepage: str = ""


@dataclass
class ToolPluginConfig:
    """Session-level configuration for one plugin instance."""

    plugin: str  # dotted path, entry_point name, or class reference
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


# ── Plugin resolution ──────────────────────────────────────────────

_plugin_registry: dict[str, Callable[[], ToolPlugin]] = {}
"""In-process plugin registry: ``name → factory()``."""


def register_plugin(name: str, factory: Callable[[], ToolPlugin]) -> None:
    """Register a plugin factory under *name* for session-config lookup.

    Typically called at import time by plugin packages:
        register_plugin("my_mcp_tools", lambda: MyMcpPlugin())
    """
    if name in _plugin_registry:
        raise ValueError(f"Plugin {name!r} is already registered")
    _plugin_registry[name] = factory


def resolve_plugin(name: str) -> ToolPlugin | None:
    """Resolve a plugin by name from the in-process registry."""
    factory = _plugin_registry.get(name)
    if factory is not None:
        return factory()
    return None


def list_plugins() -> dict[str, ToolPluginMetadata]:
    """Return metadata for all registered plugins."""
    return {
        name: factory().metadata()
        for name, factory in _plugin_registry.items()
    }
