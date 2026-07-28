from __future__ import annotations

from app.storage.sqlite import SqliteStorageBackend
from agent.session.models import SessionMode
from agent.session.session_store import SessionStore
from llm.base import LLMMessage
from memory.models import Memory, MemoryMetadata
from memory.store import MemoryStore
from server.services.evaluation_service import EvaluationService
from server.services.stats_recorder import _redacted_param_summary
from server.services.stats_service import StatsService


def test_memory_writes_append_revisions_and_exact_content_is_noop(tmp_path):
    store = MemoryStore(repo_path=str(tmp_path), db_path=str(tmp_path / "memory.db"))
    first = Memory(
        name="decision",
        description="Stable decision",
        content="Use SQLite.",
        metadata=MemoryMetadata(confidence=0.8, importance=0.7),
    )
    assert store.write_memory(first, source="test", source_session_id="s1")
    assert store.last_write_result["action"] == "NEW"
    assert store.write_memory(first, source="test", source_session_id="s1")
    assert store.last_write_result["action"] == "NOOP"

    first.content = "Use SQLite with immutable revisions."
    assert store.write_memory(first, source="test", source_session_id="s2")
    assert store.last_write_result["action"] == "REVISION"
    revisions = store.list_revisions("decision")
    assert [item["revision"] for item in revisions] == [2, 1]


def test_memory_edges_require_evidence_and_are_queryable(tmp_path):
    store = MemoryStore(repo_path=str(tmp_path), db_path=str(tmp_path / "memory.db"))
    for name in ("source", "target"):
        assert store.write_memory(Memory(
            name=name, description=name, content=name,
            metadata=MemoryMetadata(),
        ))
    edge = store.upsert_edge(
        "source", "target", "depends_on", 0.9, "Recorded in run r1"
    )
    assert edge["relation_type"] == "depends_on"
    assert store.list_edges("source")[0]["target_name"] == "target"


def test_compaction_replaces_active_messages_and_archives_sources(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session = store.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Compaction",
    )
    for index in range(6):
        store.append_message(
            session.id, LLMMessage(role="user", content=f"message {index}")
        )
    result = store.replace_messages_with_compaction(
        session.id,
        [
            {"role": "user", "content": "message 0"},
            {"role": "user", "content": "[Conversation compacted] summary"},
        ],
        tokens_before=60,
        tokens_after=15,
    )
    assert result["source_count"] == 6
    assert len(store.list_messages(session.id)) == 1  # runtime boundary is hidden
    assert len(store.list_archived_messages(session.id)) == 6
    assert store.list_compaction_runs(session.id)[0]["status"] == "completed"


def test_compaction_is_idempotent_for_unchanged_active_set(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session = store.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="Idempotency",
    )
    store.append_message(session.id, LLMMessage(role="user", content="one"))
    compacted = [{"role": "user", "content": "one"}]
    first = store.replace_messages_with_compaction(session.id, compacted)
    second = store.replace_messages_with_compaction(session.id, compacted)
    assert first["action"] == "COMPACTED"
    assert second["action"] == "NOOP"
    assert len(store.list_compaction_runs(session.id)) == 1
    assert len(store.list_archived_messages(session.id)) == 1


def test_tool_telemetry_redacts_commands_prompts_and_secrets():
    summary = _redacted_param_summary({
        "command": "echo secret",
        "prompt": "private prompt",
        "api_key": "sk-example",
        "limit": 10,
    })
    assert summary["limit"] == 10
    assert all(summary[key]["redacted"] for key in ("command", "prompt", "api_key"))
    assert "echo secret" not in str(summary)


def test_raw_telemetry_retention_is_idempotent_and_preserves_rollups(tmp_path):
    storage = SqliteStorageBackend(str(tmp_path / "stats.db"))
    service = StatsService(storage)
    with storage._store._connect() as conn:
        conn.execute(
            """INSERT INTO llm_turn_metrics
               (session_id, run_id, turn_id, step_number, input_tokens,
                output_tokens, billable_tokens, cache_read_tokens,
                cache_create_tokens, non_cached_input_tokens, token_source,
                attempts, retries, backoff_ms, timed_out, created_at)
               VALUES ('s', 'r', 't', 1, 1, 1, 1, 0, 0, 1, 'provider',
                       1, 0, 0, 0, '2000-01-01T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO daily_rollup
               (date, session_count, total_tokens, total_duration_ms,
                tool_summary, status_summary)
               VALUES ('2000-01-01', 1, 2, 3, '{}', '{}')"""
        )

    first = service.prune_raw_telemetry(90)
    second = service.prune_raw_telemetry(90)

    assert first["llm_turn_metrics"] == 1
    assert second["llm_turn_metrics"] == 0
    with storage._store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_rollup"
        ).fetchone()[0] == 1


def test_evaluation_completion_is_derived_from_acceptance_checks(tmp_path):
    overview = EvaluationService(str(tmp_path)).get_overview()
    assert len(overview["domain_gates"]) == 8
    for gate in overview["domain_gates"]:
        assert gate["completion"] == gate["passed"] / gate["total"]
        assert gate["status"] != "passed"
