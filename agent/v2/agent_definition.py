"""Load agent definitions from .md YAML frontmatter files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent.task import TaskIntent
from agent.v2.models import (
    AgentDefinition,
    AgentKind,
    AgentVisibility,
    DelegationMode,
    DelegationPolicy,
    DelegationScope,
    WorkspaceMode,
)



class AgentDefinitionError(ValueError):
    """A discovered agent file exists but cannot define a trustworthy agent."""

    def __init__(self, path: str | Path, detail: str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.detail = detail
        super().__init__(f"Invalid agent definition {self.path}: {detail}")


def _invalid(path: Path, detail: str) -> AgentDefinitionError:
    return AgentDefinitionError(path, detail)


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
    names: dict[str, Path] = {}
    for path in sorted(directory.glob("*.md")):
        definition = _parse_definition(path)
        previous = names.get(definition.name)
        if previous is not None:
            raise _invalid(
                path,
                f"duplicate agent name {definition.name!r} in the same scope; "
                f"already declared by {previous.resolve()}",
            )
        names[definition.name] = path
        definitions.append(definition)
    return definitions


def _parse_definition(path: Path) -> AgentDefinition:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _invalid(path, f"unable to read UTF-8 content: {exc}") from exc

    from utils.frontmatter import split_frontmatter
    fm_text, body = split_frontmatter(text)
    if not fm_text:
        raise _invalid(path, "missing YAML frontmatter")

    try:
        frontmatter: dict[str, Any] = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise _invalid(path, f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(frontmatter, dict):
        raise _invalid(path, "YAML frontmatter must be a mapping")

    name = frontmatter.get("name", path.stem)

    tools_raw = frontmatter.get("tools", "")
    disallowed_raw = frontmatter.get("disallowedTools", frontmatter.get("disallowed_tools", ""))
    allowed_subagents_raw = frontmatter.get(
        "allowedSubagents", frontmatter.get("allowed_subagents")
    )
    intent_raw = frontmatter.get("intent")
    if intent_raw is None:
        raise _invalid(path, "missing required field 'intent'")
    try:
        intent = TaskIntent(intent_raw)
    except ValueError as exc:
        raise _invalid(path, f"field 'intent' has invalid value {intent_raw!r}") from exc
    kind_raw = frontmatter.get("kind", AgentKind.NAMED_SUBAGENT.value)
    try:
        agent_kind = AgentKind(kind_raw)
    except ValueError as exc:
        raise _invalid(path, f"field 'kind' has invalid value {kind_raw!r}") from exc
    if agent_kind is AgentKind.FORK:
        raise _invalid(
            path,
            "fork is a spawn-time context choice, not a reusable agent definition",
        )
    isolation_raw = frontmatter.get("isolation")
    if isolation_raw == "fork":
        raise _invalid(
            path,
            "field 'isolation' controls only the workspace; conversation forks "
            "must be requested through the Agent spawn context",
        )
    if isolation_raw == "shared":
        raise _invalid(
            path,
            "field 'isolation' value 'shared' is obsolete; omit 'isolation' "
            "to use the current workspace",
        )
    try:
        workspace_mode = (
            WorkspaceMode.CURRENT
            if isolation_raw is None
            else WorkspaceMode(isolation_raw)
        )
    except ValueError as exc:
        raise _invalid(
            path, f"field 'isolation' has invalid value {isolation_raw!r}"
        ) from exc
    if "hidden" in frontmatter:
        raise _invalid(path, "removed field 'hidden'; use 'visibility'")
    visibility_raw = frontmatter.get("visibility", AgentVisibility.PUBLIC.value)
    try:
        visibility = AgentVisibility(visibility_raw)
    except ValueError as exc:
        raise _invalid(
            path, f"field 'visibility' has invalid value {visibility_raw!r}"
        ) from exc
    delegation_scope_raw = frontmatter.get(
        "delegationScope", frontmatter.get("delegation_scope")
    )
    try:
        delegation_scope = (
            DelegationScope(delegation_scope_raw)
            if delegation_scope_raw is not None
            else None
        )
    except ValueError as exc:
        raise _invalid(
            path,
            f"field 'delegationScope' has invalid value {delegation_scope_raw!r}",
        ) from exc
    model_raw = frontmatter.get("model", "inherit")
    if not isinstance(model_raw, str):
        raise _invalid(path, "field 'model' must be a string")
    model = model_raw.strip().lower()
    # Validate against known aliases, but accept arbitrary model IDs too
    from agent.v2.models import AgentModel
    known = {m.value for m in AgentModel}
    if model not in known and not model.startswith("claude-"):
        import logging
        logging.getLogger(__name__).warning(
            "Unrecognized model %r in %s; accepted model aliases: %s",
            model, path, ", ".join(sorted(known)),
        )
    try:
        max_turns = int(frontmatter.get("maxTurns", frontmatter.get("max_turns", 50)))
        max_tokens_raw = frontmatter.get(
            "maxTokens", frontmatter.get("max_tokens")
        )
        max_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None
        if max_turns < 1 or (max_tokens is not None and max_tokens < 1):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise _invalid(
            path, "fields 'maxTurns' and 'maxTokens' must be positive integers"
        ) from exc

    # ── CC-aligned frontmatter fields ──
    permission_mode = str(frontmatter.get("permissionMode", "") or "")
    mcp_servers_raw = frontmatter.get("mcpServers")
    mcp_servers: tuple[str | dict, ...] = ()
    if isinstance(mcp_servers_raw, list):
        mcp_servers = tuple(mcp_servers_raw)
    elif isinstance(mcp_servers_raw, str):
        mcp_servers = (mcp_servers_raw,)
    skills_raw = frontmatter.get("skills", "")
    skills: tuple[str, ...] = ()
    if isinstance(skills_raw, list):
        skills = tuple(str(s) for s in skills_raw)
    elif isinstance(skills_raw, str):
        skills = tuple(s.strip() for s in skills_raw.replace(",", " ").split() if s.strip())
    memory = str(frontmatter.get("memory", "") or "")
    background_raw = frontmatter.get("background", False)
    if isinstance(background_raw, bool):
        background = background_raw
    elif isinstance(background_raw, str):
        background = background_raw.strip().lower() in ("true", "yes", "1")
    else:
        background = False
    effort = str(frontmatter.get("effort", "") or "")
    color = str(frontmatter.get("color", "") or "")
    initial_prompt = str(frontmatter.get("initialPrompt", "") or "")
    hooks_raw = frontmatter.get("hooks", {})
    hooks: tuple[dict, ...] = ()
    if isinstance(hooks_raw, dict):
        hooks = (hooks_raw,)
    elif isinstance(hooks_raw, list):
        hooks = tuple(h for h in hooks_raw if isinstance(h, dict))

    delegation_policy = _parse_delegation_policy(path, allowed_subagents_raw)
    if (
        agent_kind is not AgentKind.PRIMARY
        and delegation_policy.mode is not DelegationMode.DISABLED
    ):
        raise _invalid(
            path,
            "field 'allowedSubagents' applies only to a primary agent; "
            "subagents cannot spawn other subagents",
        )

    return AgentDefinition(
        name=str(name),
        description=str(frontmatter.get("description", "")),
        intent=intent,
        agent_kind=agent_kind,
        tools=_parse_tool_list(tools_raw),
        disallowed_tools=_parse_tool_list(disallowed_raw),
        delegation_policy=delegation_policy,
        delegation_scope=delegation_scope,
        model=model,
        workspace_mode=workspace_mode,
        visibility=visibility,
        max_turns=max_turns,
        max_tokens=max_tokens,
        system_prompt=body or str(frontmatter.get("instructions", "")),
        permission_mode=permission_mode,
        mcp_servers=mcp_servers,
        skills=skills,
        memory=memory,
        background=background,
        effort=effort,
        color=color,
        initial_prompt=initial_prompt,
        hooks=hooks,
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


def _parse_delegation_policy(path: Path, value: Any) -> DelegationPolicy:
    if value is None:
        return DelegationPolicy.disabled()
    if not isinstance(value, (str, list)):
        raise _invalid(
            path, "field 'allowedSubagents' must be a string or list of names"
        )
    if isinstance(value, list) and not all(isinstance(item, str) for item in value):
        raise _invalid(
            path, "field 'allowedSubagents' list items must be strings"
        )
    names = _parse_tool_list(value)
    if not names:
        return DelegationPolicy.disabled()
    return DelegationPolicy.allowlist(names)
