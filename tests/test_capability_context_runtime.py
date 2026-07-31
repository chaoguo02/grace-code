from __future__ import annotations

from types import SimpleNamespace

from agent.mcp.types import MCPToolProps
from agent.session.models import (
    AgentDefinition,
    AgentKind,
    DelegationPolicy,
    WorkspaceMode,
)
from agent.session.runtime_prompt_builder import build_runtime_messages
from agent.task import TaskIntent
from capabilities import build_capability_context
from skills.registry import SkillMetadata


class _SkillRegistry:
    def __init__(self) -> None:
        self.loaded = []
        self._skills = [
            ("review", SkillMetadata(
                name="review",
                display_name="Review",
                description="Review code",
            )),
        ]

    def list_skill_entries(self):
        return list(self._skills)

    def load_and_render(self, name, **_kwargs):
        self.loaded.append(name)
        return f"Loaded body for {name}"

    def get_skill_meta(self, name):
        for skill_name, meta in self._skills:
            if skill_name == name:
                return meta
        return None


class _AgentRegistry:
    project_dir = ""

    def __init__(self, children) -> None:
        self.children = children

    def delegatable_by(self, parent):
        return [child for child in self.children if parent.permits_subagent(child)]


class _McpTool:
    def __init__(self) -> None:
        self.name = "mcp__docs__lookup"
        self.description = "Look up documentation"
        self.should_defer = True
        self.always_load = False
        self.mcp_props = MCPToolProps(
            server_name="docs",
            original_tool_name="lookup",
            is_deferred=True,
        )


def _primary(*, skills=()) -> AgentDefinition:
    return AgentDefinition(
        name="primary",
        description="Primary",
        intent=TaskIntent.EDIT,
        tools=frozenset({"Skill", "Agent", "ToolSearch"}),
        skills=tuple(skills),
        delegation_policy=DelegationPolicy.allowlist(frozenset({"explore"})),
        agent_kind=AgentKind.PRIMARY,
    )


def _child() -> AgentDefinition:
    return AgentDefinition(
        name="explore",
        description="Explore code",
        intent=TaskIntent.ANALYSIS,
        tools=frozenset({"Read"}),
        workspace_mode=WorkspaceMode.CURRENT,
    )


def _mcp_integration():
    return SimpleNamespace(
        server_tools={"docs": ["mcp__docs__lookup"]},
        failed_servers={},
        tools=[_McpTool()],
        tool_names=frozenset({"mcp__docs__lookup"}),
        deferred_tool_descriptors=lambda: [{
            "name": "mcp__docs__lookup",
            "description": "Look up documentation",
            "server": "docs",
        }],
    )


def test_build_capability_context_combines_skill_mcp_and_agents() -> None:
    content = build_capability_context(
        spec=_primary(),
        skill_registry=_SkillRegistry(),
        mcp_integration=_mcp_integration(),
        agent_registry=_AgentRegistry([_child()]),
    )

    assert content.startswith("[CAPABILITY CONTEXT]")
    assert content.count("## Skills") == 1
    assert "Review code" in content
    assert "MCP Tool Discovery" in content
    assert "Use `ToolSearch`" in content
    assert "mcp__docs__lookup" in content
    assert "[Available Subagents]" in content
    assert "**explore**" in content


def test_runtime_prompt_builder_injects_single_capability_context() -> None:
    messages = build_runtime_messages(
        _primary(),
        "do work",
        skill_registry=_SkillRegistry(),
        mcp_integration=_mcp_integration(),
        agent_registry=_AgentRegistry([_child()]),
    )
    contexts = [
        message.content for message in messages
        if message.content.startswith("[CAPABILITY CONTEXT]")
    ]

    assert len(contexts) == 1
    assert contexts[0].count("## Skills") == 1
    assert "MCP Tool Discovery" in contexts[0]
    assert "[Available Subagents]" in contexts[0]


def test_preloaded_skill_body_remains_separate_from_capability_context() -> None:
    registry = _SkillRegistry()

    messages = build_runtime_messages(
        _primary(skills=("review",)),
        "do work",
        skill_registry=registry,
        mcp_integration=None,
        agent_registry=_AgentRegistry([_child()]),
    )

    assert any(message.content.startswith("[PRELOADED SKILLS]") for message in messages)
    assert any(message.content.startswith("[CAPABILITY CONTEXT]") for message in messages)
    assert registry.loaded == ["review"]


def test_agent_memory_remains_independent_from_capability_context(tmp_path) -> None:
    memory_file = tmp_path / ".grace" / "agent-memory" / "primary" / "MEMORY.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("remembered fact", encoding="utf-8")
    spec = AgentDefinition(
        name="primary",
        description="Primary",
        intent=TaskIntent.EDIT,
        tools=frozenset({"Skill"}),
        memory="project",
        delegation_policy=DelegationPolicy.disabled(),
        agent_kind=AgentKind.PRIMARY,
    )

    messages = build_runtime_messages(
        spec,
        "do work",
        project_dir=str(tmp_path),
        skill_registry=_SkillRegistry(),
    )

    assert any(message.content.startswith("[AGENT MEMORY]") for message in messages)
    assert any(message.content.startswith("[CAPABILITY CONTEXT]") for message in messages)


def test_capability_context_trims_sections_by_priority() -> None:
    content = build_capability_context(
        spec=_primary(),
        skill_registry=_SkillRegistry(),
        mcp_integration=_mcp_integration(),
        agent_registry=_AgentRegistry([_child()]),
        max_tokens=1,
    )

    # max_tokens=1 means everything except the highest-priority section is trimmed.
    # The highest-priority section is always included (never silently dropped).
    assert "## Skills" in content
    assert "MCP Tool Discovery" not in content
    assert "[Available Subagents]" not in content
