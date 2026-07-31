from __future__ import annotations

from types import SimpleNamespace

from server.services.architecture_service import ArchitectureService


def test_tool_categories_are_derived_from_runtime_metadata() -> None:
    assert ArchitectureService._tool_category(
        "mcp__github__search", [], []
    ) == "mcp"
    assert ArchitectureService._tool_category(
        "Agent", ["delegate"], []
    ) == "delegation"
    assert ArchitectureService._tool_category(
        "Edit", [], ["write_workspace"]
    ) == "mutation"
    assert ArchitectureService._tool_category(
        "Read", [], ["read_workspace"]
    ) == "read"


def test_flatten_tree_preserves_parent_first_order() -> None:
    tree = {
        "id": "root",
        "children": [
            {"id": "review", "children": [{"id": "child", "children": []}]},
            {"id": "tests", "children": []},
        ],
    }

    assert ArchitectureService._flatten_tree_ids(tree) == [
        "root",
        "review",
        "child",
        "tests",
    ]


class _AgentRegistry:
    def list_all(self):
        return []


class _ToolRegistry:
    tool_names = ()
    _tools = {}
    skill_registry = None

    def get_schemas(self):
        return []


class _SessionService:
    session = SimpleNamespace(
        id="session-1",
        root_id="session-1",
        agent_name="build",
        status="completed",
        mode="primary",
        agent_kind="primary",
        context_origin="fresh",
        execution_placement="foreground",
        workspace_mode="current",
    )

    def get_session(self, session_id):
        return self.session if session_id == self.session.id else None

    def get_session_tree(self, session_id):
        return {
            "id": session_id,
            "agent_name": "build",
            "status": "completed",
            "children": [],
        }


class _StatsService:
    def get_session_steps(self, session_id):
        return []

    def get_context_snapshots(self, session_id, *, limit):
        return []


class _Storage:
    def list_trace_events(self, session_id, *, limit):
        return []


def _fake_service(tmp_path):
    return SimpleNamespace(
        repo_path=str(tmp_path),
        _agent_registry=_AgentRegistry(),
        _registry=_ToolRegistry(),
        _mcp_integration=None,
        _memory_store=None,
        _memory_context=SimpleNamespace(enabled=False),
        _memory_retriever=None,
        _memory_recall_service=None,
        _hook_dispatcher=None,
        _loaded_rules=[],
        _config=SimpleNamespace(
            llm=SimpleNamespace(provider="test", model="fixture"),
            agent=SimpleNamespace(max_steps=12, budget_tokens=10_000),
            context=SimpleNamespace(
                request_budget_tokens=8_000,
                history_window=20,
            ),
            prompts=SimpleNamespace(
                source="local",
                label="test",
                version=None,
            ),
        ),
        session_service=_SessionService(),
        _stats_service=_StatsService(),
        _storage=_Storage(),
    )


def test_live_snapshot_and_empty_session_overlay(tmp_path) -> None:
    architecture = ArchitectureService(_fake_service(tmp_path))

    configured = architecture.get_snapshot()
    observed = architecture.get_snapshot("session-1")

    assert configured["components"]
    assert configured["disclosure"]["prompt_contents_included"] is False
    assert configured["session_overlay"] is None
    assert observed["session_overlay"]["selected_session_id"] == "session-1"
    assert observed["session_overlay"]["session_count"] == 1
    assert observed["session_overlay"]["tool_usage"] == []


# ── Phase 6: provider-driven metadata extraction ────────────────────────────


def test_skills_flow_through_provider(tmp_path) -> None:
    """Skills are built from SkillCapabilityProvider + UI-only registry backfill."""
    from skills.registry import SkillMetadata

    class _SkillReg:
        def list_skill_entries(self):
            return [
                ("review", SkillMetadata(
                    name="review",
                    display_name="Code Review",
                    description="Review code changes",
                    source="project",
                    user_invocable=True,
                    context="",
                    agent="",
                    model="gpt-5",
                    effort="high",
                    allowed_tools=frozenset({"Read", "Glob"}),
                    disallowed_tools=frozenset({"Edit"}),
                    paths=("src/",),
                    mcp_servers=frozenset(),
                    when_to_use="When code needs review",
                    trusted=True,
                    file_path=".grace/skills/review.md",
                )),
            ]

    registry = _ToolRegistry()
    registry.skill_registry = _SkillReg()

    service = _fake_service(tmp_path)
    service._registry = registry

    architecture = ArchitectureService(service)
    snapshot = architecture.get_snapshot()

    skills = snapshot["skills"]
    assert len(skills) == 1
    skill = skills[0]
    assert skill["name"] == "review"
    assert skill["display_name"] == "Code Review"
    assert skill["description"] == "Review code changes"
    assert skill["model_invocable"] is True
    assert skill["user_invocable"] is True
    assert skill["context"] == "current"
    assert skill["model"] == "gpt-5"
    assert skill["effort"] == "high"
    assert skill["allowed_tools"] == ["Glob", "Read"]
    assert skill["disallowed_tools"] == ["Edit"]
    assert skill["path_scopes"] == ["src/"]


def test_mcp_flows_through_provider(tmp_path) -> None:
    """MCP server/tool status is built from McpCapabilityProvider."""
    from types import SimpleNamespace as SN

    # A mock MCP tool approximating the real MCPTool shape
    _MCPTool = SN
    tool = _MCPTool(
        name="mcp__docs__search",
        description="Search documentation",
        mcp_props=SN(server_name="docs", original_tool_name="search"),
        should_defer=False,
        always_load=True,
    )

    mcp = SN(
        is_initialized=True,
        server_tools={"docs": ["mcp__docs__search"]},
        failed_servers={},
        tools=[tool],
        tool_names=frozenset({"mcp__docs__search"}),
    )

    service = _fake_service(tmp_path)
    service._mcp_integration = mcp

    architecture = ArchitectureService(service)
    snapshot = architecture.get_snapshot()

    mcp_data = snapshot["mcp"]
    assert mcp_data["initialized"] is True
    assert mcp_data["tool_names"] == ["mcp__docs__search"]
    assert len(mcp_data["servers"]) == 1
    assert mcp_data["servers"][0]["name"] == "docs"
    assert mcp_data["servers"][0]["status"] == "connected"
    assert mcp_data["servers"][0]["tools"] == ["mcp__docs__search"]
    assert mcp_data["servers"][0]["error"] == ""
    assert mcp_data["failed_servers"] == []


def test_failed_mcp_server_from_provider(tmp_path) -> None:
    """Failed MCP servers show correct status from provider."""
    from types import SimpleNamespace as SN

    mcp = SN(
        is_initialized=True,
        server_tools={},
        failed_servers={"bad": "Connection refused"},
        tools=[],
        tool_names=frozenset(),
    )

    service = _fake_service(tmp_path)
    service._mcp_integration = mcp

    architecture = ArchitectureService(service)
    snapshot = architecture.get_snapshot()

    mcp_data = snapshot["mcp"]
    assert mcp_data["failed_servers"] == [
        {"name": "bad", "error": "Connection refused"},
    ]
    assert len(mcp_data["servers"]) == 1
    assert mcp_data["servers"][0]["name"] == "bad"
    assert mcp_data["servers"][0]["status"] == "failed"
    assert mcp_data["servers"][0]["tools"] == []
    assert mcp_data["servers"][0]["error"] == "Connection refused"


def test_fingerprint_in_snapshot() -> None:
    """Architecture snapshot includes a stable capability fingerprint."""
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        architecture = ArchitectureService(_fake_service(Path(td)))
        snapshot = architecture.get_snapshot()

        assert "fingerprint" in snapshot
        assert isinstance(snapshot["fingerprint"], str)
        # When no providers are available, fingerprint is empty
        assert snapshot["fingerprint"] == ""


def test_fingerprint_stable_across_calls() -> None:
    """Fingerprint is deterministic for the same runtime state."""
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = Path(td)
        architecture = ArchitectureService(_fake_service(path))
        fp1 = architecture.get_snapshot()["fingerprint"]
        fp2 = architecture.get_snapshot()["fingerprint"]
        assert fp1 == fp2
