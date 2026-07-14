"""Load agent definitions from .md YAML frontmatter files."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from agent.task import TaskIntent
from agent.v2.models import (
    AgentDefinition,
    AgentIsolation,
    AgentVisibility,
    DelegationScope,
)

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def load_agent_definitions(
    project_dir: str | Path | None = None,
    user_dir: str | Path | None = None,
) -> dict[str, AgentDefinition]:
    """Load agent definitions from user, project, and built-in scopes.

    Priority: project > user > built-in.
    """
    merged: dict[str, AgentDefinition] = {}

    # Built-in (lowest priority)
    from agent.v2.models import _BUILTIN_AGENTS
    merged.update(_BUILTIN_AGENTS)

    # User (~/.forge-agent/agents/)
    user_agents_dir = Path(user_dir) if user_dir else Path.home() / ".forge-agent" / "agents"
    for definition in _load_from_dir(user_agents_dir):
        merged[definition.name] = definition

    # Project (.forge-agent/agents/).  Project discovery is opt-in: callers
    # must provide the Runtime-owned project root instead of inheriting the
    # host process CWD.
    if project_dir is not None:
        project_root = Path(project_dir).expanduser().resolve()
        project_agents_dir = project_root / ".forge-agent" / "agents"
        for definition in _load_from_dir(project_agents_dir):
            merged[definition.name] = definition

    return merged


def _load_from_dir(directory: Path) -> list[AgentDefinition]:
    if not directory.is_dir():
        return []
    definitions: list[AgentDefinition] = []
    for path in sorted(directory.glob("*.md")):
        definition = _parse_definition(path)
        if definition is not None:
            definitions.append(definition)
    return definitions


def _parse_definition(path: Path) -> AgentDefinition | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read agent definition %s: %s", path, exc)
        return None

    match = _FRONTMATTER_RE.match(text)
    if match is None:
        logger.warning("Agent definition %s has no YAML frontmatter", path)
        return None

    try:
        frontmatter: dict[str, Any] = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("Invalid YAML frontmatter in %s: %s", path, exc)
        return None

    if not isinstance(frontmatter, dict):
        logger.warning("Agent definition %s frontmatter is not a mapping", path)
        return None

    name = frontmatter.get("name", path.stem)
    body = text[match.end():].strip()

    tools_raw = frontmatter.get("tools", "")
    disallowed_raw = frontmatter.get("disallowedTools", frontmatter.get("disallowed_tools", ""))
    allowed_subagents_raw = frontmatter.get("allowedSubagents", frontmatter.get("allowed_subagents", None))
    intent_raw = frontmatter.get("intent")
    if intent_raw is None:
        logger.warning("Agent definition %s is missing required intent", path)
        return None
    try:
        intent = TaskIntent(intent_raw)
    except ValueError:
        logger.warning("Agent definition %s has invalid intent", path)
        return None
    isolation_raw = frontmatter.get("isolation", AgentIsolation.FORK.value)
    try:
        isolation = AgentIsolation(isolation_raw)
    except ValueError:
        logger.warning("Agent definition %s has invalid isolation", path)
        return None
    if "background" in frontmatter:
        logger.warning("Agent definition %s declares unsupported background", path)
        return None
    if "hidden" in frontmatter:
        logger.warning(
            "Agent definition %s uses removed hidden flag; use visibility", path
        )
        return None
    visibility_raw = frontmatter.get("visibility", AgentVisibility.PUBLIC.value)
    try:
        visibility = AgentVisibility(visibility_raw)
    except ValueError:
        logger.warning("Agent definition %s has invalid visibility", path)
        return None
    delegation_scope_raw = frontmatter.get(
        "delegationScope", frontmatter.get("delegation_scope")
    )
    try:
        delegation_scope = (
            DelegationScope(delegation_scope_raw)
            if delegation_scope_raw is not None
            else None
        )
    except ValueError:
        logger.warning("Agent definition %s has invalid delegation scope", path)
        return None
    try:
        max_turns = int(frontmatter.get("maxTurns", frontmatter.get("max_turns", 50)))
        max_tokens_raw = frontmatter.get(
            "maxTokens", frontmatter.get("max_tokens")
        )
        max_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None
        if max_turns < 1 or (max_tokens is not None and max_tokens < 1):
            raise ValueError
    except (TypeError, ValueError):
        logger.warning("Agent definition %s has invalid resource limits", path)
        return None

    return AgentDefinition(
        name=str(name),
        description=str(frontmatter.get("description", "")),
        intent=intent,
        tools=_parse_tool_list(tools_raw),
        disallowed_tools=_parse_tool_list(disallowed_raw),
        allowed_subagents=_parse_optional_list(allowed_subagents_raw),
        delegation_scope=delegation_scope,
        model=str(frontmatter.get("model", "inherit")),
        isolation=isolation,
        visibility=visibility,
        max_turns=max_turns,
        max_tokens=max_tokens,
        system_prompt=body or str(frontmatter.get("instructions", "")),
    )


def _parse_tool_list(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(
            name.strip()
            for name in value.replace(",", " ").split()
            if name.strip()
        )
    if isinstance(value, list):
        return frozenset(str(item).strip() for item in value if str(item).strip())
    return frozenset()


def _parse_optional_list(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    return _parse_tool_list(value)
