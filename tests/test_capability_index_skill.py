from __future__ import annotations

from capabilities import (
    CapabilityIndex,
    CapabilityKind,
    CapabilityQuery,
    CapabilityStatus,
)
from capabilities.providers.skill_provider import SkillCapabilityProvider
from capabilities.render import CapabilityPromptRenderer
from skills.registry import SkillMetadata, SkillRegistry


def _registry(tmp_path) -> SkillRegistry:
    return SkillRegistry(str(tmp_path), include_builtin=False)


def test_skill_capability_section_matches_golden(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry._metadata["review"] = SkillMetadata(
        name="review",
        display_name="Review",
        description="Review code for correctness",
        when_to_use="after code changes",
        paths=("src/**/*.py",),
    )
    registry._metadata["weather"] = SkillMetadata(
        name="weather",
        display_name="Weather",
        description="Use deterministic weather tools",
        mcp_servers=frozenset({"weather-mock"}),
        user_invocable=False,
    )

    query = CapabilityQuery(kinds=frozenset({CapabilityKind.SKILL}))
    snapshot = CapabilityIndex([
        SkillCapabilityProvider(registry),
    ]).snapshot(query)
    sections = CapabilityPromptRenderer().render(snapshot, query)

    assert len(sections) == 1
    assert sections[0].content == "\n".join([
        "User-invocable: /review",
        "Use the `Skill` tool to load a skill (PREFERRED — saves context by injecting instructions without duplicating):",
        "- **review**: Review code for correctness (Use when: after code changes) (Path scope: src/**/*.py)",
        "- **weather**: Use deterministic weather tools (MCP: weather-mock)",
    ])


def test_skill_provider_filters_model_disabled_skills_by_default(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry._metadata["release"] = SkillMetadata(
        name="release",
        display_name="Release",
        description="Release workflow",
        disable_model_invocation=True,
    )

    query = CapabilityQuery(kinds=frozenset({CapabilityKind.SKILL}))
    snapshot = CapabilityIndex([
        SkillCapabilityProvider(registry),
    ]).snapshot(query)

    assert snapshot.descriptors == ()
    assert registry.format_for_prompt() == ""


def test_skill_provider_can_include_model_disabled_skills_for_non_llm_catalog(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry._metadata["release"] = SkillMetadata(
        name="release",
        display_name="Release",
        description="Release workflow",
        disable_model_invocation=True,
    )

    query = CapabilityQuery(
        kinds=frozenset({CapabilityKind.SKILL}),
        visible_to_model=None,
    )
    snapshot = CapabilityIndex([
        SkillCapabilityProvider(registry, llm_invocable_only=False),
    ]).snapshot(query)

    assert [descriptor.metadata.name for descriptor in snapshot.descriptors] == ["release"]
    assert snapshot.descriptors[0].metadata.model_invocable is False


def test_capability_query_default_excludes_only_hidden_status() -> None:
    query = CapabilityQuery(kinds=frozenset({CapabilityKind.SKILL}))
    assert CapabilityStatus.HIDDEN in query.excluded_statuses
    assert CapabilityStatus.FAILED not in query.excluded_statuses
    assert CapabilityStatus.UNAVAILABLE not in query.excluded_statuses


def test_skill_snapshot_fingerprint_is_deterministic(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry._metadata["b"] = SkillMetadata(
        name="b",
        display_name="B",
        description="Second",
    )
    registry._metadata["a"] = SkillMetadata(
        name="a",
        display_name="A",
        description="First",
    )
    query = CapabilityQuery(kinds=frozenset({CapabilityKind.SKILL}))
    provider = SkillCapabilityProvider(registry)

    first = CapabilityIndex([provider]).snapshot(query)
    second = CapabilityIndex([provider]).snapshot(query)

    assert first.fingerprint == second.fingerprint
    assert [descriptor.metadata.name for descriptor in first.descriptors] == ["a", "b"]
