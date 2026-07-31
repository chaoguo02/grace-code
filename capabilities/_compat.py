"""Compatibility bridges from legacy prompt helpers to CapabilityIndex."""

from __future__ import annotations

from capabilities.index import CapabilityIndex
from capabilities.models import CapabilityKind, CapabilityQuery
from capabilities.providers.skill_provider import SkillCapabilityProvider
from capabilities.render import CapabilityPromptRenderer


def format_skills_for_prompt(skill_registry, *, llm_invocable_only: bool = True) -> str:
    query = CapabilityQuery(kinds=frozenset({CapabilityKind.SKILL}))
    index = CapabilityIndex([
        SkillCapabilityProvider(
            skill_registry,
            llm_invocable_only=llm_invocable_only,
        ),
    ])
    snapshot = index.snapshot(query)
    sections = CapabilityPromptRenderer().render(snapshot, query)
    if not sections:
        return ""
    return "\n\n".join(section.content for section in sections if section.content.strip())
