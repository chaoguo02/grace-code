from __future__ import annotations

from pathlib import Path

from app.storage.sqlite import SqliteStorageBackend
from context.stats import ContextStats
from context.history import ConversationHistory
from context.manager import ContextManager
from llm.base import LLMMessage
from server.services.stats_recorder import StatsRecorder
from server.services.stats_service import StatsService


def test_context_snapshot_round_trip(tmp_path: Path) -> None:
    storage = SqliteStorageBackend(str(tmp_path / "context.db"))
    service = StatsService(storage)
    recorder = StatsRecorder(service)

    row_id = recorder.record_context_snapshot(
        "session-1",
        run_id="run-1",
        turn_id="turn-1",
        step=3,
        request_kind="primary",
        context_stats=ContextStats(
            request_budget_tokens=20_000,
            estimated_total_tokens=8_000,
            system_tokens=2_000,
            memory_tokens=500,
            task_tokens=5_500,
            omitted_tokens=250,
            compact_triggered=True,
            compact_reason="history exceeded planner threshold",
        ),
        capabilities={
            "tool_names": ["Read", "mcp__github__search"],
            "tool_count": 2,
            "mcp_tools": ["mcp__github__search"],
            "mcp_servers": ["github"],
            "active_skills": ["review"],
        },
    )

    assert row_id > 0
    snapshots = service.get_context_snapshots("session-1")
    assert len(snapshots) == 1
    assert snapshots[0]["run_id"] == "run-1"
    assert snapshots[0]["turn_id"] == "turn-1"
    assert snapshots[0]["step_number"] == 3
    assert snapshots[0]["stats"]["estimated_total_tokens"] == 8_000
    assert snapshots[0]["stats"]["compact_triggered"] is True
    assert snapshots[0]["capabilities"]["mcp_servers"] == ["github"]


def test_context_snapshot_limit_returns_latest_in_request_order(
    tmp_path: Path,
) -> None:
    storage = SqliteStorageBackend(str(tmp_path / "context-limit.db"))
    service = StatsService(storage)

    for step in range(1, 5):
        service.record_context_snapshot(
            "session-2",
            step_number=step,
            request_kind="subagent",
            stats={"estimated_total_tokens": step * 100},
            capabilities={},
        )

    snapshots = service.get_context_snapshots("session-2", limit=2)
    assert [item["step_number"] for item in snapshots] == [3, 4]


def test_subagent_context_separates_system_and_task_tokens() -> None:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="inspect this change"))

    request = ContextManager().build_sub_agent_messages(
        history,
        "read-only reviewer instructions",
    )

    assert request.stats.system_tokens > 0
    assert request.stats.task_tokens > 0
    assert request.stats.estimated_total_tokens == (
        request.stats.system_tokens + request.stats.task_tokens
    )
