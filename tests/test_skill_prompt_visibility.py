from __future__ import annotations

from agent.session.models import (
    AgentDefinition,
    AgentKind,
    DelegationPolicy,
)
from agent.session.runtime_prompt_builder import build_runtime_messages
from agent.task import TaskIntent
from skills.registry import SkillMetadata, SkillRegistry
from skills.tool import SkillTool


class _PromptSkillRegistry:
    def list_skill_entries(self):
        from skills.registry import SkillMetadata
        return [
            ("review", SkillMetadata(
                name="review",
                display_name="Review",
                description="Review code for correctness",
            )),
        ]

    def format_for_prompt(self, *, llm_invocable_only: bool = True) -> str:
        assert llm_invocable_only is True
        return "## Skills\n- **review**: Review code"


def _primary_spec(*, tools: frozenset[str]) -> AgentDefinition:
    return AgentDefinition(
        name="test",
        description="test",
        intent=TaskIntent.EDIT,
        tools=tools,
        delegation_policy=DelegationPolicy.disabled(),
        agent_kind=AgentKind.PRIMARY,
    )


def test_skill_listing_does_not_depend_on_delegation() -> None:
    messages = build_runtime_messages(
        _primary_spec(tools=frozenset({"Skill"})),
        "review this",
        skill_registry=_PromptSkillRegistry(),
    )

    assert any("## Skills" in message.content for message in messages)


def test_skill_listing_is_not_injected_without_skill_tool() -> None:
    messages = build_runtime_messages(
        _primary_spec(tools=frozenset({"Read"})),
        "plan this",
        skill_registry=_PromptSkillRegistry(),
    )

    assert not any("## Skills" in message.content for message in messages)


def test_nested_skill_prompt_uses_canonical_invocation_name(tmp_path) -> None:
    registry = SkillRegistry(str(tmp_path), include_builtin=False)
    registry._nested_metadata["apps/web:review"] = SkillMetadata(
        name="review",
        display_name="Review",
        description="Review web code",
    )

    prompt = registry.format_for_prompt()

    assert "/apps/web:review" in prompt
    assert "**apps/web:review**" in prompt


def test_model_cannot_invoke_user_only_skill(tmp_path) -> None:
    registry = SkillRegistry(str(tmp_path), include_builtin=False)
    registry._metadata["release"] = SkillMetadata(
        name="release",
        display_name="Release",
        description="Release workflow",
        disable_model_invocation=True,
    )

    result = SkillTool(registry).execute({"skill_name": "release"})

    assert result.success is False
    assert "user must invoke it directly" in result.error


def test_inline_skill_does_not_silently_ignore_fork_context(tmp_path) -> None:
    registry = SkillRegistry(str(tmp_path), include_builtin=False)
    registry._metadata["isolated-review"] = SkillMetadata(
        name="isolated-review",
        display_name="Isolated review",
        description="Review in an isolated context",
        context="fork",
    )

    result = SkillTool(registry).execute({"skill_name": "isolated-review"})

    assert result.success is False
    assert "requires fork context" in result.error
