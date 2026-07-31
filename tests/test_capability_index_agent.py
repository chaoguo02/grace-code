from __future__ import annotations

from capabilities import CapabilityIndex, CapabilityKind, CapabilityQuery
from capabilities.providers.agent_provider import AgentCapabilityProvider
from capabilities.render import CapabilityPromptRenderer
from agent.session.models import (
    AgentDefinition,
    AgentKind,
    AgentVisibility,
    DelegationPolicy,
    DelegationScope,
    WorkspaceMode,
)
from agent.session.runtime_prompt_builder import build_runtime_messages
from agent.task import TaskIntent


class _AgentRegistry:
    def __init__(self, children):
        self.children = children

    def delegatable_by(self, parent):
        return [child for child in self.children if parent.permits_subagent(child)]


def _parent(*, delegation_scope=None, policy=None) -> AgentDefinition:
    return AgentDefinition(
        name="primary",
        description="Primary",
        intent=TaskIntent.EDIT,
        tools=frozenset({"Agent"}),
        delegation_policy=policy or DelegationPolicy.allowlist(frozenset({"explore", "review", "isolated"})),
        delegation_scope=delegation_scope,
        agent_kind=AgentKind.PRIMARY,
    )


def _child(
    name: str,
    *,
    intent=TaskIntent.ANALYSIS,
    workspace_mode=WorkspaceMode.CURRENT,
    visibility=AgentVisibility.PUBLIC,
) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        description=f"{name} agent",
        intent=intent,
        tools=frozenset({"Read"}),
        workspace_mode=workspace_mode,
        visibility=visibility,
        model="inherit",
        effort="low",
        skills=("review",),
        mcp_servers=("docs",),
    )


def _render(parent, registry) -> str:
    query = CapabilityQuery(kinds=frozenset({CapabilityKind.AGENT}))
    snapshot = CapabilityIndex([
        AgentCapabilityProvider(registry, parent),
    ]).snapshot(query)
    sections = CapabilityPromptRenderer().render(snapshot, query)
    return "\n\n".join(section.content for section in sections)


def test_agent_provider_renders_delegatable_subagents() -> None:
    parent = _parent()
    registry = _AgentRegistry([_child("explore"), _child("review")])

    content = _render(parent, registry)

    assert "[Available Subagents]" in content
    assert "- **explore** (workspace=current): explore agent" in content
    assert "- **review** (workspace=current): review agent" in content
    assert "Delegation rules" in content


def test_delegation_disabled_produces_no_subagent_section() -> None:
    parent = _parent(policy=DelegationPolicy.disabled())
    registry = _AgentRegistry([_child("explore")])

    content = _render(parent, registry)

    assert content == ""


def test_worktree_child_adds_worktree_result_protocol() -> None:
    parent = _parent()
    registry = _AgentRegistry([
        _child("isolated", workspace_mode=WorkspaceMode.WORKTREE),
    ])

    content = _render(parent, registry)

    assert "Worktree Result Protocol (MANDATORY):" in content
    assert "subagent_worktree_inspect" in content


def test_read_only_delegation_scope_emits_boundary_and_filters_edit_children() -> None:
    parent = _parent(delegation_scope=DelegationScope.READ_ONLY)
    registry = _AgentRegistry([
        _child("explore", intent=TaskIntent.ANALYSIS),
        _child("review", intent=TaskIntent.EDIT),
    ])

    content = _render(parent, registry)

    assert "read-only delegation scope" in content
    assert "**explore**" in content
    assert "**review**" not in content


def test_runtime_prompt_builder_uses_agent_capability_renderer() -> None:
    parent = _parent()
    registry = _AgentRegistry([_child("explore")])

    messages = build_runtime_messages(
        parent,
        "delegate this",
        agent_registry=registry,
    )

    assert any("[Available Subagents]" in message.content for message in messages)
    assert any("**explore**" in message.content for message in messages)
