"""Load agent definitions from .md YAML frontmatter files."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from agent.v2.models import AgentDefinition

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

    # Project (.forge-agent/agents/)
    project_root = Path(project_dir) if project_dir else Path.cwd()
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

    return AgentDefinition(
        name=str(name),
        description=str(frontmatter.get("description", "")),
        tools=_parse_tool_list(tools_raw),
        disallowed_tools=_parse_tool_list(disallowed_raw),
        allowed_subagents=_parse_optional_list(allowed_subagents_raw),
        model=str(frontmatter.get("model", "inherit")),
        isolation=str(frontmatter.get("isolation", "fork")),
        background=bool(frontmatter.get("background", False)),
        max_turns=int(frontmatter.get("maxTurns", frontmatter.get("max_turns", 50))),
        hidden=bool(frontmatter.get("hidden", False)),
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
