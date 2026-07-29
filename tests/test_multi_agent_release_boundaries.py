"""Release-boundary regression tests for Workbench Multi-Agent mode."""

from __future__ import annotations

import sqlite3

from agent.session.models import SessionMode
from agent.session.multi_agent_config import MultiAgentFeatureConfig
from agent.session.session_store import SessionStore
from server.routers.multi_agent import create_multi_agent_router
from server.services.multi_agent_service import MultiAgentService


def test_feature_config_is_independent_and_bounded() -> None:
    config = MultiAgentFeatureConfig.from_environment({
        "GRACE_MULTI_AGENT_MODE_ENABLED": "off",
        "GRACE_MAX_MULTI_AGENT_TASKS": "80",
        "GRACE_MAX_FANOUT_PER_TURN": "7",
        "GRACE_MAX_CONCURRENT_SUBAGENTS": "5",
    })

    assert config.enabled is False
    assert config.max_tasks == 80
    assert config.max_wave_fanout == 7
    assert config.max_concurrent == 5

    clamped = MultiAgentFeatureConfig.from_environment({
        "GRACE_MAX_MULTI_AGENT_TASKS": "9999",
        "GRACE_MAX_FANOUT_PER_TURN": "9999",
        "GRACE_MAX_CONCURRENT_SUBAGENTS": "0",
    })
    assert clamped.max_tasks == 128
    assert clamped.max_wave_fanout == 32
    assert clamped.max_concurrent == 1


def test_release_api_exposes_run_control_aliases() -> None:
    router = create_multi_agent_router(lambda: None)
    routes = {
        (route.path, method)
        for route in router.routes
        for method in (route.methods or set())
    }

    expected = {
        ("/api/multi-agent/{session_id}/runs/{run_id}", "GET"),
        ("/api/multi-agent/{session_id}/runs/{run_id}/cancel", "POST"),
        ("/api/multi-agent/{session_id}/runs/{run_id}/resume", "POST"),
        ("/api/multi-agent/{session_id}/runs/{run_id}/integrate", "POST"),
        ("/api/multi-agent/{session_id}/runs/{run_id}/verify", "POST"),
        ("/api/multi-agent/{session_id}/tasks/{task_id}/retry", "POST"),
    }
    assert expected <= routes


def test_legacy_session_database_gets_additive_delegation_and_trace_schema(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, parent_id TEXT, root_id TEXT NOT NULL,
                agent_name TEXT NOT NULL, mode TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, repo_path TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, tool_call_id TEXT,
                tool_name TEXT, tool_calls_json TEXT, created_at TEXT NOT NULL
            )
            """
        )

    store = SessionStore(path)
    parent = store.create_session(
        agent_name="orchestrator",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="migrated",
    )
    store.create_delegation_run(
        run_id="migrated-run",
        parent_session_id=parent.id,
        topology="chain",
    )

    with sqlite3.connect(path) as conn:
        run_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(delegation_runs)")
        }
        task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(delegation_tasks)")
        }
        trace_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(session_trace_events)")
        }
    assert {"phase", "version", "verification_json", "interrupted_at"} <= run_columns
    assert {"retry_count", "supersedes_task_id", "integration_status"} <= task_columns
    assert {"session_id", "seq", "event_type", "event_json"} <= trace_columns


def test_snapshot_exposes_feature_limits_and_observability(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRACE_MAX_MULTI_AGENT_TASKS", "24")
    store = SessionStore(tmp_path / "snapshot.db")
    root = store.create_session(
        agent_name="orchestrator",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="metrics",
    )
    store.create_delegation_run(
        run_id="metrics-run",
        parent_session_id=root.id,
        topology="fan_out_fan_in",
    )
    store.create_delegation_task(
        task_id="metrics-run:inspect",
        delegation_run_id="metrics-run",
        agent_type="explore",
        goal="Inspect",
    )
    store.update_delegation_task(
        "metrics-run:inspect",
        status="completed",
        report={"tokens_used": 12, "duration_ms": 34},
    )
    store.reconcile_delegation_run("metrics-run")

    service = MultiAgentService(type("Service", (), {"_store": store})())
    snapshot = service.get_snapshot(root.id)

    assert snapshot["feature"]["enabled"] is True
    assert snapshot["feature"]["max_tasks"] == 24
    assert snapshot["limits"]["max_multi_agent_tasks"] == 24
    assert snapshot["observability"]["run_status_counts"] == {"completed": 1}
    assert snapshot["observability"]["task_status_counts"] == {"completed": 1}
    assert snapshot["observability"]["tokens_used"] == 12
    assert snapshot["observability"]["worker_duration_ms"] == 34
