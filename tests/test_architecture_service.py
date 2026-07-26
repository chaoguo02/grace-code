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
    _skill_registry = None

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
