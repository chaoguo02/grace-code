"""Agent registry — loads agent definitions from .md files."""

from __future__ import annotations

from pathlib import Path

from agent.v2.agent_definition import load_agent_definitions
from agent.v2.models import AgentDefinition

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
    "TaskStop": "task_stop",
}

# ── Legacy name sets (kept for SessionRuntime policy filtering) ──

_BUILD_ALLOWED = frozenset({
    "shell", "file_read", "file_view", "file_write", "file_edit",
    "search_text", "find_files", "find_symbol",
    "pytest", "git_status", "git_diff", "git_add", "git_commit",
    "web_search", "web_fetch",
    "artifact_list", "artifact_read", "artifact_search",
    "evidence_list", "evidence_get",
    "memory_read", "memory_list", "memory_write", "memory_delete",
    "memory_search", "submit_read_plan",
})

# Fallback for agents without explicit tool lists (explore subagent default)
_EXPLORE_INTERNAL_ALLOWED = frozenset({
    "file_read", "file_view", "find_files", "find_symbol", "search_text",
    "git_status", "git_diff", "web_search", "web_fetch",
    "artifact_list", "artifact_read", "artifact_search",
    "evidence_list", "evidence_get", "memory_read", "memory_list", "memory_search",
})
_GENERAL_INTERNAL_ALLOWED = _BUILD_ALLOWED


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
    _mtime_cache: dict[str, float] = {}

    def __init__(self, project_dir: str | Path | None = None) -> None:
        self._project_dir = str(project_dir) if project_dir else ""
        self._agents: dict[str, AgentDefinition] = {}

        cache_key = self._project_dir
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

    @staticmethod
    def _project_agents_mtime() -> float:
        """Get latest mtime of the project agents directory. Returns 0 if N/A."""
        import os as _os
        try:
            cwd = _os.getcwd()
            agents_dir = _os.path.join(cwd, ".forge-agent", "agents")
            if _os.path.isdir(agents_dir):
                return _os.path.getmtime(agents_dir)
        except OSError:
            pass
        return 0.0

    def _reload(self) -> None:
        self._agents = load_agent_definitions(project_dir=self._project_dir)

    @classmethod
    def invalidate_cache(cls, project_dir: str = "") -> None:
        """Force reload on next construction (call after modifying .md files)."""
        if project_dir:
            cls._instances.pop(str(project_dir), None)
            cls._mtime_cache.pop(str(project_dir), None)
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
        return [
            spec for spec in self._agents.values()
            if not spec.hidden and spec.isolation != "none"
        ]

    def list_primary_agents(self) -> list[AgentDefinition]:
        return [
            spec for spec in self._agents.values()
            if spec.isolation == "none" and not spec.hidden
        ]

    def tool_names_for(self, name: str) -> frozenset[str]:
        """Return the resolved forge-agent tool names for an agent definition."""
        definition = self.get(name)
        if definition.tools:
            return resolve_tool_set(definition.tools)
        # Fallback for agents without explicit tool lists
        return self._default_tools_for(name)

    def disallowed_tool_names_for(self, name: str) -> frozenset[str]:
        definition = self.get(name)
        if definition.disallowed_tools:
            return resolve_tool_set(definition.disallowed_tools)
        return frozenset()

    @staticmethod
    def _default_tools_for(name: str) -> frozenset[str]:
        if name == "explore":
            return _EXPLORE_INTERNAL_ALLOWED
        if name == "general":
            return _GENERAL_INTERNAL_ALLOWED
        return _BUILD_ALLOWED
