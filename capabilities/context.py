"""Build prompt-facing capability context from active capability providers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from capabilities.index import CapabilityIndex
from capabilities.models import CapabilityKind, CapabilityQuery, CapabilitySection
from capabilities.providers import CapabilityProvider
from capabilities.providers.agent_provider import AgentCapabilityProvider
from capabilities.providers.mcp_provider import McpCapabilityProvider
from capabilities.providers.skill_provider import SkillCapabilityProvider
from capabilities.render import CapabilityPromptRenderer
from context.token_budget import estimate_tokens


DEFAULT_CAPABILITY_CONTEXT_BUDGET = 12_000


def build_capability_sections(
    *,
    spec: Any,
    skill_registry: Any = None,
    mcp_integration: Any = None,
    agent_registry: Any = None,
) -> list[CapabilitySection]:
    """Build structured capability sections without final markdown assembly."""
    providers: list[CapabilityProvider] = []
    kinds: set[CapabilityKind] = set()

    if (
        skill_registry is not None
        and "Skill" in getattr(spec, "tools", frozenset())
        and hasattr(skill_registry, "list_skill_entries")
    ):
        providers.append(SkillCapabilityProvider(skill_registry, llm_invocable_only=True))
        kinds.add(CapabilityKind.SKILL)

    if mcp_integration is not None:
        providers.append(McpCapabilityProvider(mcp_integration))
        kinds.update({CapabilityKind.MCP_SERVER, CapabilityKind.MCP_TOOL})

    if _should_include_agents(spec, agent_registry):
        providers.append(AgentCapabilityProvider(agent_registry, spec))
        kinds.add(CapabilityKind.AGENT)

    if not providers or not kinds:
        return []

    query = CapabilityQuery(
        kinds=frozenset(kinds),
        visible_to_model=None,
    )
    snapshot = CapabilityIndex(providers).snapshot(query)
    sections = CapabilityPromptRenderer().render(snapshot, query)
    return _estimate_sections(sections)


def render_capability_sections(
    sections: list[CapabilitySection],
    *,
    max_tokens: int = DEFAULT_CAPABILITY_CONTEXT_BUDGET,
) -> str:
    return render_capability_selection(
        sections,
        max_tokens=max_tokens,
    )[0]


def render_capability_selection(
    sections: list[CapabilitySection],
    *,
    max_tokens: int = DEFAULT_CAPABILITY_CONTEXT_BUDGET,
) -> tuple[str, list[CapabilitySection], int]:
    selected, trimmed_count = select_capability_sections(
        sections,
        max_tokens=max_tokens,
    )
    if not selected:
        return "", [], trimmed_count

    body = "\n\n".join(
        f"## {section.title}\n{section.content}"
        for section in selected
        if section.content.strip()
    )
    if not body.strip():
        return "", [], trimmed_count
    return "[CAPABILITY CONTEXT]\n\n" + body, selected, trimmed_count


def select_capability_sections(
    sections: list[CapabilitySection],
    *,
    max_tokens: int,
) -> tuple[list[CapabilitySection], int]:
    selected = _trim_sections(sections, max_tokens=max_tokens)
    non_empty_count = sum(1 for section in sections if section.content.strip())
    return selected, max(0, non_empty_count - len(selected))


def capability_sections_fingerprint(sections: list[CapabilitySection]) -> str:
    """Compute a stable fingerprint for a set of capability sections.

    The fingerprint intentionally excludes ``token_estimate`` — token counts
    are computational estimates, not identity.  They can vary across runs
    without any change to the capability surface.
    """
    keys = [
        (
            section.title,
            section.kind_filter.value,
            section.priority,
            section.descriptor_count,
            section.source_fingerprint,
        )
        for section in sorted(sections, key=lambda item: (item.priority, item.title))
        if section.content.strip()
    ]
    raw = json.dumps(keys, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_capability_context(
    *,
    spec: Any,
    skill_registry: Any = None,
    mcp_integration: Any = None,
    agent_registry: Any = None,
    max_tokens: int = DEFAULT_CAPABILITY_CONTEXT_BUDGET,
) -> str:
    """Build one prompt-facing capability context block for a runtime session.

    Providers remain read-only. Token estimation and section trimming happen here,
    immediately before final markdown assembly.
    """
    return render_capability_sections(
        build_capability_sections(
            spec=spec,
            skill_registry=skill_registry,
            mcp_integration=mcp_integration,
            agent_registry=agent_registry,
        ),
        max_tokens=max_tokens,
    )


def _should_include_agents(spec: Any, agent_registry: Any) -> bool:
    if agent_registry is None:
        return False
    try:
        from agent.session.models import DelegationMode, SessionMode
    except Exception:
        return False
    if getattr(spec, "mode", None) is not SessionMode.PRIMARY:
        return False
    policy = getattr(spec, "delegation_policy", None)
    return bool(policy is not None and policy.mode is not DelegationMode.DISABLED)


def _estimate_sections(sections: list[CapabilitySection]) -> list[CapabilitySection]:
    return [
        replace(section, token_estimate=estimate_tokens(section.content))
        for section in sections
    ]


def _trim_sections(
    sections: list[CapabilitySection],
    *,
    max_tokens: int,
) -> list[CapabilitySection]:
    if max_tokens <= 0:
        return []
    selected: list[CapabilitySection] = []
    total = 0
    for section in sorted(sections, key=lambda item: (item.priority, item.title)):
        if not section.content.strip():
            continue
        # Never skip the highest-priority section — a single oversized
        # section is still better than an empty capability context.
        if total + section.token_estimate > max_tokens and selected:
            continue
        selected.append(section)
        total += section.token_estimate
    return selected
