from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_tool_pool_keeps_builtin_prefix_and_rejects_duplicates():
    from core.base import ToolResult
    from agent.mcp.types import MCPToolProps
    from tools.factory import build_tool
    from tools.pool import assemble_tool_pool

    def make(name, *, mcp=False):
        return build_tool(
            name=name,
            description=name,
            parameters_schema={"type": "object", "properties": {}},
            execute=lambda _params: ToolResult(success=True, output="ok"),
            mcp_props=(
                MCPToolProps(server_name="x", original_tool_name=name)
                if mcp else None
            ),
        )

    pool = assemble_tool_pool(
        [make("Zulu"), make("Alpha")],
        [make("mcp__z__b", mcp=True), make("mcp__a__a", mcp=True)],
    )
    assert [tool.name for tool in pool] == [
        "Alpha",
        "Zulu",
        "mcp__a__a",
        "mcp__z__b",
    ]

    with pytest.raises(ValueError, match="Duplicate tool name"):
        assemble_tool_pool([make("same")], [make("same", mcp=True)])


def test_built_tool_defaults_are_fail_closed():
    from core.base import ToolConcurrency, ToolResult
    from tools.factory import build_tool

    tool = build_tool(
        name="dynamic",
        description="dynamic",
        parameters_schema={"type": "object", "properties": {}},
        execute=lambda _params: ToolResult(success=True, output="ok"),
    )
    assert tool.isReadOnly({}) is False
    assert tool.concurrency_mode({}) is ToolConcurrency.SERIAL
    assert tool.execute({}).success is True


def test_filtered_registry_preserves_dependencies_without_owning_lifecycle():
    from core.base import ToolRegistry, ToolResult
    from tools.factory import build_tool

    closed = []
    dependency = object()
    tool = build_tool(
        name="one",
        description="one",
        parameters_schema={"type": "object", "properties": {}},
        execute=lambda _params: ToolResult(success=True, output="ok"),
        close=lambda timeout: closed.append(timeout),
    )
    root = ToolRegistry(skill_registry=dependency).register(tool)
    child = root.filtered({"one"})

    assert child.skill_registry is dependency
    child.close()
    assert closed == []
    root.close(timeout=1.5)
    assert closed == [1.5]


def _write_skill(
    root: Path,
    name: str,
    body: str,
    *,
    description: str = "test",
) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


def test_skill_sources_merge_by_priority_and_block_untrusted_commands(tmp_path):
    from skills.registry import SkillRegistry, SkillSource

    project = tmp_path / "project"
    bundled = tmp_path / "bundled"
    remote = tmp_path / "mcp"
    _write_skill(project, "same", "project body")
    _write_skill(bundled, "same", "bundled body")
    _write_skill(remote, "remote", "!`echo unsafe`")

    registry = SkillRegistry(
        "",
        include_builtin=False,
        sources=[
            SkillSource("project", (str(project),), 2),
            SkillSource("bundled", (str(bundled),), 5),
            SkillSource("mcp", (str(remote),), 6, trusted=False),
        ],
    )
    try:
        assert registry.get_skill_detail("same").strip() == "project body"
        rendered = registry.load_and_render(
            "remote",
            runtime=SimpleNamespace(
                exec=lambda *_args, **_kwargs: pytest.fail(
                    "untrusted MCP skill executed a command",
                ),
            ),
        )
        assert "blocked: untrusted skill inline command" in rendered
        assert registry.get_skill_meta("same").source == "project"
    finally:
        registry.close()


def test_skill_live_reload_detects_body_change(tmp_path):
    from skills.registry import SkillRegistry, SkillSource

    root = tmp_path / "skills"
    _write_skill(
        root,
        "reloadable",
        "version one",
        description="description one",
    )
    registry = SkillRegistry(
        "",
        include_builtin=False,
        sources=[SkillSource("project", (str(root),), 2)],
        live_reload=True,
    )
    try:
        _write_skill(
            root,
            "reloadable",
            "version two",
            description="description two",
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if (
                registry.get_skill_meta("reloadable").description
                == "description two"
            ):
                break
            time.sleep(0.1)
        assert (
            registry.get_skill_meta("reloadable").description
            == "description two"
        )
    finally:
        registry.close()


def test_mcp_loading_modes_have_one_deferred_state():
    from agent.mcp.types import MCPToolProps
    from agent.session.mcp_integration import MCPToolIntegration, ToolLoadingMode
    from core.base import ToolResult
    from tools.factory import build_tool

    def tool():
        return build_tool(
            name="mcp__docs__lookup",
            description="lookup docs",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            execute=lambda _params: ToolResult(success=True, output="ok"),
            mcp_props=MCPToolProps(
                server_name="docs",
                original_tool_name="lookup",
            ),
        )

    standard = MCPToolIntegration(
        server_configs=[],
        loading_mode=ToolLoadingMode.STANDARD,
    )
    standard._tools = [tool()]
    standard._apply_loading_mode()
    assert standard._tools[0].always_load is True
    assert standard._tools[0].mcp_props.is_deferred is False

    deferred = MCPToolIntegration(
        server_configs=[],
        loading_mode=ToolLoadingMode.TST,
    )
    deferred._tools = [tool()]
    deferred._apply_loading_mode()
    assert deferred._tools[0].should_defer is True
    deferred.activate_tools({"mcp__docs__lookup"})
    assert deferred._tools[0].mcp_props.always_load is True
    assert deferred._tools[0].mcp_props.is_deferred is False
