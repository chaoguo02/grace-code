"""Agent registry — loads agent definitions from .md files."""

from __future__ import annotations

from pathlib import Path

from agent.v2.agent_definition import load_agent_definitions
from agent.v2.models import AgentDefinition, AgentIsolation, AgentVisibility

# ── Tool name mapping: Claude Code → forge-agent ──

_TOOL_ALIASES: dict[str, str] = {
    "Read": "file_read",
    "Write": "file_write",
    "Edit": "file_edit",
    "Glob": "find_files",
    "Grep": "search_text",
    "Bash": "shell",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
    "Task": "task",
}

def resolve_tool_name(name: str) -> str:
    """Map a Claude Code tool alias to a forge-agent tool name."""
    return _TOOL_ALIASES.get(name, name)


def resolve_tool_set(names: frozenset[str]) -> frozenset[str]:
    """Map a set of tool names (may include aliases) to forge-agent names."""
    return frozenset(resolve_tool_name(name) for name in names)


class AgentRegistryV2:
    """Registry that discovers agent definitions from .md files.

    Scope priority: project (.forge-agent/agents/) > user (~/.forge-agent/agents/) > built-in.

    Cached per project_dir with mtime-based automatic invalidation:
    - First load: scan .md files, cache in memory
    - Subsequent loads: check project agents dir mtime; if unchanged, reuse cache
    - mtime changed: auto-reload (e.g., user added/edited an agent .md file)
    """

    _instances: dict[str, "AgentRegistryV2"] = {}
    _mtime_cache: dict[str, int] = {}

    def __init__(self, project_dir: str | Path | None = None) -> None:
        self._project_dir = (
            str(Path(project_dir).expanduser().resolve())
            if project_dir is not None else None
        )
        self._agents: dict[str, AgentDefinition] = {}

        cache_key = self._project_dir or "<no-project>"
        current_mtime = self._project_agents_mtime()

        cached_instance = AgentRegistryV2._instances.get(cache_key)
        cached_mtime = AgentRegistryV2._mtime_cache.get(cache_key, 0)

        if cached_instance is not None and current_mtime == cached_mtime:
            # Cache hit — zero I/O
            self._agents = cached_instance._agents
        else:
            # Cache miss or stale — reload from disk
            self._reload()
            AgentRegistryV2._instances[cache_key] = self
            AgentRegistryV2._mtime_cache[cache_key] = current_mtime

    @property
    def project_dir(self) -> str | None:
        """Absolute project fact source, or None for built-in/user-only registries."""
        return self._project_dir

    def _project_agents_mtime(self) -> int:
        """Return a content-sensitive project-agent version without consulting CWD."""
        if self._project_dir is None:
            return 0
        agents_dir = Path(self._project_dir) / ".forge-agent" / "agents"
        try:
            if not agents_dir.is_dir():
                return 0
            versions = [agents_dir.stat().st_mtime_ns]
            versions.extend(
                path.stat().st_mtime_ns
                for path in agents_dir.glob("*.md")
                if path.is_file()
            )
            return max(versions)
        except OSError:
            return 0

    def _reload(self) -> None:
        self._agents = load_agent_definitions(project_dir=self._project_dir)

    @classmethod
    def invalidate_cache(cls, project_dir: str = "") -> None:
        """Force reload on next construction (call after modifying .md files)."""
        if project_dir:
            cache_key = str(Path(project_dir).expanduser().resolve())
            cls._instances.pop(cache_key, None)
            cls._mtime_cache.pop(cache_key, None)
        else:
            cls._instances.clear()
            cls._mtime_cache.clear()

    def get(self, name: str) -> AgentDefinition:
        try:
            return self._agents[name]
        except KeyError:
            raise KeyError(
                f"Unknown agent: {name!r}. "
                f"Available: {list(self._agents.keys())}"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._agents

    def list_all(self) -> list[AgentDefinition]:
        return sorted(self._agents.values(), key=lambda a: a.name)

    def list_subagents(self) -> list[AgentDefinition]:
        """Return public agents discoverable without an explicit parent grant."""
        return [
            spec for spec in self._agents.values()
            if (
                spec.visibility is AgentVisibility.PUBLIC
                and spec.isolation is not AgentIsolation.NONE
            )
        ]

    def delegatable_by(
        self, parent: AgentDefinition,
    ) -> list[AgentDefinition]:
        """Return children granted to a parent, including explicitly named hidden agents."""
        explicitly_allowed = parent.allowed_subagents or frozenset()
        return sorted(
            (
                child
                for child in self._agents.values()
                if child.isolation is not AgentIsolation.NONE
                and (
                    child.visibility is AgentVisibility.PUBLIC
                    or child.name in explicitly_allowed
                )
                and parent.permits_subagent(child)
            ),
            key=lambda child: child.name,
        )

    def list_primary_agents(self) -> list[AgentDefinition]:
        return [
            spec for spec in self._agents.values()
            if (
                spec.isolation is AgentIsolation.NONE
                and spec.visibility is AgentVisibility.PUBLIC
            )
        ]

    def tool_names_for(self, name: str) -> frozenset[str]:
        """Return the resolved forge-agent tool names for an agent definition."""
        definition = self.get(name)
        return resolve_tool_set(definition.tools)

    def disallowed_tool_names_for(self, name: str) -> frozenset[str]:
        definition = self.get(name)
        if definition.disallowed_tools:
            return resolve_tool_set(definition.disallowed_tools)
        return frozenset()
