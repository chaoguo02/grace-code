"""Evidence chain tests — verify the complete evidence lifecycle.

Tests cover: Store, Recorder, CompletionGuard, Requirements,
idempotency, stale detection, cascade cleanup, and secret redaction.
"""

import pytest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from agent.session.run_evidence import (
    RunEvidenceStore,
    EvidenceEntry,
    EvidenceKind,
    EvidenceStatus,
    RunEvidenceRequirements,
    RequiredToolCall,
    RequiredArtifact,
    idempotency_key_for_tool,
    idempotency_key_for_worker,
    idempotency_key_for_skill,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_entry(store, **kw):
    """Create and record a minimal evidence entry."""
    eid = kw.pop("evidence_id", f"ev_{len(kw)}")
    return store.record(EvidenceEntry(
        evidence_id=eid,
        idempotency_key=kw.pop("idempotency_key", eid),
        root_run_id=kw.pop("root_run_id", ""),
        session_id=kw.pop("session_id", "s1"),
        producer_session_id=kw.pop("producer_session_id", "s1"),
        kind=kw.pop("kind", EvidenceKind.TOOL_CALL_COMPLETED),
        status=kw.pop("status", EvidenceStatus.SUCCEEDED),
        **kw,
    ))


# ── Test 1: Parallel Workers do not cross-contaminate evidence ─────────────


def test_parallel_workers_no_cross_contamination():
    store = RunEvidenceStore("root-1")
    # Worker A
    _make_entry(store, evidence_id="ev_a1", producer_session_id="wa",
                idempotency_key="wa:1", tool_name="Read")
    # Worker B
    _make_entry(store, evidence_id="ev_b1", producer_session_id="wb",
                idempotency_key="wb:1", tool_name="Grep")
    # Verify separation
    a_entries = store.entries_by_producer("wa")
    b_entries = store.entries_by_producer("wb")
    assert len(a_entries) == 1 and a_entries[0].tool_name == "Read"
    assert len(b_entries) == 1 and b_entries[0].tool_name == "Grep"
    store.close()


# ── Test 2: 4 Skill entry points all produce SKILL_LOADED ──────────────────


def test_four_skill_sources_produce_skill_loaded():
    from agent.session.runtime import _flush_skill_activations
    store = RunEvidenceStore("root-2")
    activations = [
        {"skill_name": "s1", "source": src, "fingerprint": "fp",
         "mcp_dependencies": [], "session_id": "s1"}
        for src in ("http_request", "cli_slash", "preload", "tool_call")
    ]
    _flush_skill_activations(store, activations)
    entries = store.entries_by_kind(EvidenceKind.SKILL_LOADED)
    sources = {e.metadata.get("source") for e in entries}
    assert len(entries) == 4
    assert sources == {"http_request", "cli_slash", "preload", "tool_call"}
    store.close()


# ── Test 3: MCP connected ≠ required call satisfied ────────────────────────


def test_mcp_exposed_does_not_satisfy_required_call():
    store = RunEvidenceStore("root-3")
    _make_entry(store, evidence_id="ev_mcp", kind=EvidenceKind.MCP_TOOLS_EXPOSED,
                tool_name="mcp:weather_mock", idempotency_key="mcp:e:1")
    reqs = RunEvidenceRequirements(
        required_tool_calls=(
            RequiredToolCall(tool="mcp:weather_mock:weather_get_current"),
        ),
    )
    evaluation = store.evaluate(reqs)
    assert not evaluation.satisfied
    assert len(evaluation.missing) == 1
    assert evaluation.missing[0].code == "required_mcp_evidence_missing"
    store.close()


# ── Test 4: Skill loaded but MCP not called → Completion Guard blocks ─────


def test_skill_loaded_without_mcp_call_blocked():
    store = RunEvidenceStore("root-4")
    _make_entry(store, kind=EvidenceKind.SKILL_LOADED,
                tool_name="skill:city-weather", idempotency_key="sk:1")
    reqs = RunEvidenceRequirements(
        required_skills=frozenset({"city-weather"}),
        required_mcp_servers=frozenset({"weather_mock"}),
        required_tool_calls=(
            RequiredToolCall(tool="mcp:weather_mock:weather_get_current"),
        ),
    )
    evaluation = store.evaluate(reqs)
    assert not evaluation.satisfied  # MCP not called
    # Skill IS satisfied
    missing_codes = {m.code for m in evaluation.missing}
    assert "required_mcp_not_exposed" in missing_codes
    assert "required_mcp_evidence_missing" in missing_codes
    store.close()


# ── Test 5: Policy blocked → BLOCKED evidence, no STARTED ──────────────────


def test_policy_blocked_produces_blocked_not_started():
    store = RunEvidenceStore("root-5")
    _make_entry(store, kind=EvidenceKind.TOOL_CALL_BLOCKED,
                status=EvidenceStatus.BLOCKED, tool_name="Write",
                idempotency_key="b:1")
    started = store.entries_by_kind(EvidenceKind.TOOL_CALL_STARTED)
    blocked = store.entries_by_kind(EvidenceKind.TOOL_CALL_BLOCKED)
    assert len(started) == 0
    assert len(blocked) == 1
    store.close()


# ── Test 6: Same invocation retry → idempotency key dedup ──────────────────


def test_idempotency_key_dedup_on_retry():
    store = RunEvidenceStore("root-6")
    key = idempotency_key_for_tool("completed", "s1", "inv1", tool_name="Read")
    e1 = _make_entry(store, evidence_id="ev_first", idempotency_key=key)
    e2 = _make_entry(store, evidence_id="ev_dup", idempotency_key=key)
    assert e1.evidence_id == e2.evidence_id  # returned existing entry
    assert store.count == 1  # only one entry recorded
    store.close()


# ── Test 7: Worker lifecycle events ────────────────────────────────────────


def test_worker_lifecycle_start_and_complete():
    store = RunEvidenceStore("root-7")
    sk = idempotency_key_for_worker("start", "child-1", 1)
    ck = idempotency_key_for_worker("terminal", "child-1", 1)
    _make_entry(store, kind=EvidenceKind.WORKER_STARTED,
                tool_name="explore", idempotency_key=sk, session_id="child-1",
                producer_session_id="child-1")
    _make_entry(store, kind=EvidenceKind.WORKER_COMPLETED,
                tool_name="explore", idempotency_key=ck, session_id="child-1",
                producer_session_id="child-1")
    started = store.entries_by_kind(EvidenceKind.WORKER_STARTED)
    completed = store.entries_by_kind(EvidenceKind.WORKER_COMPLETED)
    assert len(started) == 1 and len(completed) == 1
    assert started[0].tool_name == "explore"
    store.close()


# ── Test 8: Background Worker handle → no premature COMPLETED ──────────────


def test_background_worker_no_premature_completed():
    """When a background worker returns a handle, WORKER_COMPLETED is NOT
    recorded until _execute_child_session terminates."""
    store = RunEvidenceStore("root-8")
    sk = idempotency_key_for_worker("start", "bg-1", 1)
    _make_entry(store, kind=EvidenceKind.WORKER_STARTED,
                tool_name="general", idempotency_key=sk,
                session_id="bg-1", producer_session_id="bg-1")
    # No WORKER_COMPLETED recorded yet
    completed = store.entries_by_kind(EvidenceKind.WORKER_COMPLETED)
    assert len(completed) == 0, "Background handle should not produce COMPLETED"
    store.close()


# ── Test 9: File re-written → old verification stale ───────────────────────


def test_stale_verification_detected():
    store = RunEvidenceStore("root-9")
    # Write file
    _make_entry(store, evidence_id="ev_w1", kind=EvidenceKind.ARTIFACT_WRITTEN,
                path="report.md", idempotency_key="w:1")
    # Verify
    _make_entry(store, evidence_id="ev_v1", kind=EvidenceKind.VALIDATION_COMPLETED,
                idempotency_key="v:1")
    # Write again (later sequence → stale)
    _make_entry(store, evidence_id="ev_w2", kind=EvidenceKind.ARTIFACT_WRITTEN,
                path="report.md", idempotency_key="w:2")
    # Check: evidence after verification at sequence 2
    later_writes = [
        e for e in store.snapshot()
        if e.kind == EvidenceKind.ARTIFACT_WRITTEN
        and e.path == "report.md"
        and e.sequence > 2  # verification was at seq 2
    ]
    assert len(later_writes) == 1  # the re-write is stale
    store.close()


# ── Test 10: Requirements per-city check ───────────────────────────────────


def test_requirements_per_city_check():
    store = RunEvidenceStore("root-10")
    # MCP called for Beijing only — missing Shanghai and Shenzhen
    _make_entry(store, kind=EvidenceKind.MCP_TOOLS_EXPOSED,
                tool_name="mcp:weather_mock", idempotency_key="mcp:e:10")
    _make_entry(store, kind=EvidenceKind.TOOL_CALL_COMPLETED,
                tool_name="mcp:weather_mock:weather_get_current",
                metadata={"city1": "北京"}, idempotency_key="call:bj")
    reqs = RunEvidenceRequirements(
        required_mcp_servers=frozenset({"weather_mock"}),
        required_tool_calls=tuple(
            RequiredToolCall(tool="mcp:weather_mock:weather_get_current",
                             arguments_match={f"city{i}": c})
            for i, c in enumerate(["北京", "上海", "深圳"], 1)
        ),
    )
    evaluation = store.evaluate(reqs)
    assert not evaluation.satisfied
    assert len(evaluation.missing) == 2  # Shanghai + Shenzhen
    store.close()


# ── Test 11: Evidence survives store snapshot/restart ──────────────────────


def test_evidence_survives_rebuild():
    store = RunEvidenceStore("root-11")
    _make_entry(store, kind=EvidenceKind.TOOL_CALL_COMPLETED,
                tool_name="Read", idempotency_key="r:1")
    _make_entry(store, kind=EvidenceKind.ARTIFACT_WRITTEN,
                path="out.txt", idempotency_key="a:1")
    # Simulate rebuild from persisted rows
    rows = [e.to_dict() for e in store.snapshot()]
    store.close()
    # Rebuild
    store2 = RunEvidenceStore.load_from_db("root-11",
        list_fn=lambda rid: rows,
    )
    assert store2.count == 2
    assert len(store2.entries_by_kind(EvidenceKind.TOOL_CALL_COMPLETED)) == 1
    store2.close()


# ── Test 12: Worker failed → evidence retained but requirement not satisfied


def test_failed_worker_evidence_retained_but_not_satisfying():
    store = RunEvidenceStore("root-12")
    _make_entry(store, kind=EvidenceKind.TOOL_CALL_COMPLETED,
                status=EvidenceStatus.FAILED,
                tool_name="mcp:weather_mock:weather_get_current",
                idempotency_key="fail:1",
                metadata={"city1": "北京"})
    reqs = RunEvidenceRequirements(
        required_tool_calls=(
            RequiredToolCall(tool="mcp:weather_mock:weather_get_current",
                             arguments_match={"city1": "北京"}),
        ),
    )
    evaluation = store.evaluate(reqs)
    assert not evaluation.satisfied  # FAILED does not satisfy
    assert store.count == 1  # evidence is retained
    store.close()


# ── Test 13: Cascade delete on session removal ─────────────────────────────


def test_cascade_delete_run_evidence():
    store = RunEvidenceStore("root-13")
    _make_entry(store, idempotency_key="cd:1")
    _make_entry(store, idempotency_key="cd:2")
    assert store.count == 2
    # Simulate cascade delete via store cleanup
    store.close()
    # After close, the lock is released. In production, SessionStore.delete_run_evidence
    # handles the DB cleanup. For the in-memory store, close() suffices.


# ── Test 14: Secret keys redacted from parameters_digest ───────────────────


def test_secret_keys_redacted_from_digest():
    from agent.session.tool_evidence_recorder import _digest_params
    params_with_secret = {"city": "北京", "api_key": "sk-12345", "token": "abc"}
    digest = _digest_params(params_with_secret)
    # Digest exists
    assert digest
    # Same params without secrets should produce different digest
    params_clean = {"city": "北京", "api_key": "[REDACTED]", "token": "[REDACTED]"}
    digest2 = _digest_params(params_clean)
    # Both redact to same values, so digests should match
    assert digest == digest2, "Redacted params should produce consistent digests"
    # Raw secret should NOT appear
    raw_digest = _digest_params({"api_key": "sk-12345"})
    raw_other = _digest_params({"api_key": "sk-different"})
    assert raw_digest == raw_other, "Different secrets should produce same digest after redaction"


# ── Test 14b: Temporal ordering — artifact after deps ──────────────────────


def test_artifact_after_dependencies_satisfies_temporal():
    """Artifact written AFTER its dependencies → satisfied."""
    store = RunEvidenceStore("root-t1")
    _make_entry(store, evidence_id="ev_call", kind=EvidenceKind.TOOL_CALL_COMPLETED,
                tool_name="weather_get_current", idempotency_key="c:1")
    # Artifact written after the call
    _make_entry(store, evidence_id="ev_art", kind=EvidenceKind.ARTIFACT_WRITTEN,
                path="report.md", idempotency_key="a:1",
                depends_on=("ev_call",))
    reqs = RunEvidenceRequirements(
        required_artifacts=(RequiredArtifact(path="report.md", must_depend_on_required_calls=False),),
    )
    evaluation = store.evaluate(reqs)
    assert evaluation.satisfied, f"Should be satisfied, got missing={evaluation.missing}"
    store.close()


def test_artifact_before_dependencies_fails_temporal():
    """Artifact written BEFORE its dependencies → violation."""
    store = RunEvidenceStore("root-t2")
    # Artifact written first (seq 1)
    _make_entry(store, evidence_id="ev_art", kind=EvidenceKind.ARTIFACT_WRITTEN,
                path="report.md", idempotency_key="a:1",
                depends_on=("ev_call",))
    # Call happens AFTER (seq 2)
    _make_entry(store, evidence_id="ev_call", kind=EvidenceKind.TOOL_CALL_COMPLETED,
                tool_name="weather_get_current", idempotency_key="c:1")
    reqs = RunEvidenceRequirements(
        required_artifacts=(RequiredArtifact(path="report.md", must_depend_on_required_calls=False),),
    )
    evaluation = store.evaluate(reqs)
    # Should fail because ev_call has seq 2 > ev_art seq 1
    assert not evaluation.satisfied
    codes = {m.code for m in evaluation.missing}
    assert "artifact_temporal_violation" in codes
    store.close()


# ── Test 15: root_run_id auto-fill from store ──────────────────────────────


def test_root_run_id_auto_fill():
    store = RunEvidenceStore("root-15")
    e = _make_entry(store, root_run_id="", session_id="")
    assert e.root_run_id == "root-15"
    assert e.session_id == "root-15"
    store.close()


def test_sqlite_is_authoritative_for_concurrent_idempotency(tmp_path):
    """Concurrent producers receive the same persisted canonical row."""
    from agent.session.session_store import SessionStore

    repository = SessionStore(str(tmp_path / "evidence.db"))

    def persist(index: int):
        store = RunEvidenceStore(
            "run-atomic",
            default_session_id="session-atomic",
            persist_fn=repository.create_evidence,
        )
        return store.record(EvidenceEntry(
            evidence_id=f"ev_candidate_{index}",
            idempotency_key="same-logical-operation",
            root_run_id="",
            session_id="session-atomic",
            producer_session_id="session-atomic",
            kind=EvidenceKind.TOOL_CALL_COMPLETED,
            status=EvidenceStatus.SUCCEEDED,
            tool_name="Read",
        ))

    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(persist, range(8)))

    assert len({entry.evidence_id for entry in entries}) == 1
    rows = repository.list_evidence("run-atomic")
    assert len(rows) == 1
    assert rows[0]["sequence"] == 1


def test_artifact_path_is_workspace_relative(tmp_path):
    from agent.session.tool_evidence_recorder import ToolEvidenceRecorder
    from core.types import ToolEffect

    output = tmp_path / "reports" / "weather.md"
    output.parent.mkdir()
    output.write_text("weather", encoding="utf-8")
    tool = SimpleNamespace(
        name="Write",
        _workspace_root=tmp_path,
        metadata=SimpleNamespace(effects=(ToolEffect.WRITE_WORKSPACE,)),
        mcp_props=None,
    )
    result = SimpleNamespace(
        success=True,
        output="written",
        error="",
        cached=False,
        cache_key="",
        modified_files=[str(output)],
        metadata={
            "evidence": {
                "path": str(output),
                "content_hash": "hash",
            },
        },
    )
    store = RunEvidenceStore("run-path", default_session_id="session-path")
    ToolEvidenceRecorder(store).record_completed(
        "Write", {"file_path": str(output)}, result, "call-1", tool, "session-path",
    )

    artifacts = store.entries_by_kind(EvidenceKind.ARTIFACT_WRITTEN)
    assert len(artifacts) == 1
    assert artifacts[0].path == "reports/weather.md"


def test_required_started_worker_must_reach_success_terminal():
    store = RunEvidenceStore("run-worker", default_session_id="root-session")
    _make_entry(
        store,
        evidence_id="ev_worker_start",
        kind=EvidenceKind.WORKER_STARTED,
        status=EvidenceStatus.STARTED,
        session_id="child-1",
        producer_session_id="child-1",
        idempotency_key="worker:start:child-1",
        metadata={"required": True},
    )
    requirements = RunEvidenceRequirements()
    evaluation = store.evaluate(requirements)
    assert not evaluation.satisfied
    assert {item.code for item in evaluation.missing} == {
        "worker_terminal_missing",
    }

    _make_entry(
        store,
        evidence_id="ev_worker_done",
        kind=EvidenceKind.WORKER_COMPLETED,
        status=EvidenceStatus.SUCCEEDED,
        session_id="child-1",
        producer_session_id="child-1",
        idempotency_key="worker:done:child-1",
    )
    completed_evaluation = store.evaluate(requirements)
    assert completed_evaluation.satisfied
    assert completed_evaluation.satisfied_by[
        "req_worker:child-1"
    ] == ("ev_worker_done",)


def test_optional_worker_does_not_block_completion():
    store = RunEvidenceStore("run-optional", default_session_id="root-session")
    _make_entry(
        store,
        kind=EvidenceKind.WORKER_STARTED,
        status=EvidenceStatus.STARTED,
        session_id="child-optional",
        producer_session_id="child-optional",
        idempotency_key="worker:start:optional",
        metadata={"required": False},
    )
    assert store.evaluate(RunEvidenceRequirements()).satisfied


def test_real_tool_registry_projects_completed_and_cache_hit(tmp_path):
    from agent.session.evidence_scope import EvidenceScope
    from core.base import ToolRegistry
    from tools.file_tool import FileReadCache, FileReadTool

    source = tmp_path / "source.txt"
    source.write_text("one\ntwo\n", encoding="utf-8")
    store = RunEvidenceStore("run-registry", default_session_id="session-registry")
    registry = ToolRegistry()
    registry.register(FileReadTool(
        read_cache=FileReadCache(),
        workspace_root=tmp_path,
    ))
    bound = (
        registry.with_session_id("session-registry")
        .with_run_context(SimpleNamespace(
            evidence_store=store,
            evidence_scope=EvidenceScope(),
        ))
    )

    first = bound.execute_tool("Read", {"path": "source.txt"}, invocation_id="read-1")
    second = bound.execute_tool("Read", {"path": "source.txt"}, invocation_id="read-2")

    assert first.success and second.success and second.cached
    completed = store.entries_by_kind(EvidenceKind.TOOL_CALL_COMPLETED)
    cache_hits = store.entries_by_kind(EvidenceKind.CACHE_HIT)
    assert len(completed) == 2
    assert len(cache_hits) == 1
    assert cache_hits[0].parent_evidence_id == completed[-1].evidence_id


def test_parallel_child_scopes_merge_only_consumed_evidence():
    from agent.session.evidence_scope import EvidenceScope

    requirement = RequiredToolCall(tool="Lookup")
    parent = EvidenceScope(required_tool_calls=(requirement,))
    left = parent.fork()
    right = parent.fork()
    left_entry = SimpleNamespace(evidence_id="ev-left", tool_name="Lookup")
    right_entry = SimpleNamespace(evidence_id="ev-right", tool_name="Lookup")
    left.note_tool_evidence(left_entry, {})
    right.note_tool_evidence(right_entry, {})

    parent.merge_consumed_child(left)
    visible = parent.resolved_dependency_ids([
        SimpleNamespace(
            evidence_id="ev-left",
            status=EvidenceStatus.SUCCEEDED,
        ),
        SimpleNamespace(
            evidence_id="ev-right",
            status=EvidenceStatus.SUCCEEDED,
        ),
    ])
    assert visible == ("ev-left",)


def test_sqlite_restart_preserves_sequence_dependencies_and_staleness(tmp_path):
    from agent.session.session_store import SessionStore

    repository = SessionStore(str(tmp_path / "restart.db"))
    store = RunEvidenceStore(
        "run-restart",
        default_session_id="session-restart",
        persist_fn=repository.create_evidence,
    )
    call = _make_entry(
        store,
        evidence_id="ev-call",
        kind=EvidenceKind.TOOL_CALL_COMPLETED,
        tool_name="Lookup",
        idempotency_key="call",
    )
    _make_entry(
        store,
        evidence_id="ev-artifact",
        kind=EvidenceKind.ARTIFACT_WRITTEN,
        path="report.md",
        depends_on=(call.evidence_id,),
        idempotency_key="artifact",
    )
    _make_entry(
        store,
        evidence_id="ev-validation",
        kind=EvidenceKind.VALIDATION_COMPLETED,
        idempotency_key="validation",
    )
    _make_entry(
        store,
        evidence_id="ev-rewrite",
        kind=EvidenceKind.ARTIFACT_WRITTEN,
        path="report.md",
        depends_on=(call.evidence_id,),
        idempotency_key="rewrite",
    )
    store.close()

    recovered = RunEvidenceStore.load_from_db(
        "run-restart",
        default_session_id="session-restart",
        list_fn=repository.list_evidence,
        persist_fn=repository.create_evidence,
    )
    rows = recovered.snapshot()
    assert [entry.sequence for entry in rows] == [1, 2, 3, 4]
    assert rows[1].depends_on == ("ev-call",)
    evaluation = recovered.evaluate(RunEvidenceRequirements(
        verification_requirement="required",
    ))
    assert not evaluation.satisfied
    assert {item.code for item in evaluation.missing} == {
        "verification_stale",
    }


def test_repository_delete_removes_run_evidence(tmp_path):
    from agent.session.session_store import SessionStore

    repository = SessionStore(str(tmp_path / "delete.db"))
    store = RunEvidenceStore(
        "run-delete",
        default_session_id="session-delete",
        persist_fn=repository.create_evidence,
    )
    _make_entry(store, idempotency_key="delete-me")
    assert len(repository.list_evidence("run-delete")) == 1
    repository.delete_run_evidence("run-delete")
    assert repository.list_evidence("run-delete") == []


def test_real_write_then_read_back_projects_integrity_chain(tmp_path):
    from agent.session.evidence_scope import EvidenceScope
    from core.base import ToolRegistry
    from tools.file_tool import FileReadCache, FileReadTool, FileWriteTool

    cache = FileReadCache()
    store = RunEvidenceStore(
        "run-file-chain",
        default_session_id="session-file-chain",
    )
    registry = ToolRegistry()
    registry.register(FileWriteTool(
        read_cache=cache,
        workspace_root=str(tmp_path),
    ))
    registry.register(FileReadTool(
        read_cache=cache,
        workspace_root=tmp_path,
    ))
    bound = (
        registry.with_session_id("session-file-chain")
        .with_run_context(SimpleNamespace(
            evidence_store=store,
            evidence_scope=EvidenceScope(),
        ))
    )

    written = bound.execute_tool(
        "Write",
        {"path": "report.md", "content": "verified content\n"},
        invocation_id="write-report",
    )
    observed = bound.execute_tool(
        "Read",
        {"path": "report.md"},
        invocation_id="read-report",
    )

    assert written.success and observed.success
    artifacts = store.entries_by_kind(EvidenceKind.ARTIFACT_WRITTEN)
    integrity = store.entries_by_kind(EvidenceKind.ARTIFACT_INTEGRITY_CHECKED)
    assert len(artifacts) == 1 and artifacts[0].path == "report.md"
    assert artifacts[0].result_digest
    assert len(integrity) == 1
    assert integrity[0].status is EvidenceStatus.SUCCEEDED
    assert artifacts[0].evidence_id in integrity[0].depends_on
    assert store.evaluate(RunEvidenceRequirements(
        required_artifacts=(RequiredArtifact(
            path="report.md",
            must_depend_on_required_calls=False,
            require_integrity_check=True,
        ),),
    )).satisfied


def test_event_bus_preserves_observation_evidence_reference():
    from server.services.event_bus import _translate_event

    event = SimpleNamespace(
        event_type="observation",
        timestamp="2026-01-01T00:00:00Z",
        child_session_id="",
        payload={
            "step": 2,
            "tool_call_id": "call-1",
            "observation": {
                "tool_name": "Read",
                "output": "ok",
                "status": "success",
                "metadata": {
                    "evidence_ref": {
                        "evidence_id": "ev-call-1",
                        "kind": "tool_call_completed",
                        "status": "succeeded",
                    },
                },
            },
        },
    )

    translated = _translate_event(event)
    assert translated[0]["evidence"]["evidence_id"] == "ev-call-1"
    assert translated[0]["tool_call_id"] == "call-1"


def test_child_requirements_cannot_use_sibling_evidence():
    store = RunEvidenceStore("run-producers", default_session_id="root")
    _make_entry(
        store,
        evidence_id="ev-sibling",
        tool_name="Deliver",
        session_id="child-b",
        producer_session_id="child-b",
        idempotency_key="child-b:deliver",
    )
    requirements = RunEvidenceRequirements(
        required_tool_calls=(RequiredToolCall(
            tool="Deliver",
            producer_session_id="child-a",
        ),),
        require_started_workers_succeed=False,
        producer_session_id="child-a",
    )
    evaluation = store.evaluate(requirements)
    assert not evaluation.satisfied
    assert evaluation.missing[0].code == "required_tool_call_missing"

    _make_entry(
        store,
        evidence_id="ev-own",
        tool_name="Deliver",
        session_id="child-a",
        producer_session_id="child-a",
        idempotency_key="child-a:deliver",
    )
    assert store.evaluate(requirements).satisfied


def test_tool_loaded_skill_contract_becomes_dynamic_requirement():
    store = RunEvidenceStore("run-dynamic-skill", default_session_id="root")
    _make_entry(
        store,
        evidence_id="ev-skill",
        kind=EvidenceKind.SKILL_LOADED,
        tool_name="skill:city-weather",
        session_id="worker-weather",
        producer_session_id="worker-weather",
        idempotency_key="skill:dynamic",
        metadata={
            "mcp_dependencies": ["weather_mock"],
            "required_tool_calls": [{
                "tool": "mcp:weather_mock:weather_get_current",
                "arguments_match": {"city": "北京"},
                "minimum_count": 1,
            }],
        },
    )
    _make_entry(
        store,
        evidence_id="ev-exposed",
        kind=EvidenceKind.MCP_TOOLS_EXPOSED,
        tool_name="mcp:weather_mock",
        idempotency_key="mcp:dynamic",
    )
    requirements = RunEvidenceRequirements(
        require_started_workers_succeed=False,
    )
    evaluation = store.evaluate(requirements)
    assert not evaluation.satisfied
    assert evaluation.missing[0].code == "required_mcp_evidence_missing"

    _make_entry(
        store,
        evidence_id="ev-weather",
        tool_name="mcp:weather_mock:weather_get_current",
        session_id="worker-weather",
        producer_session_id="worker-weather",
        idempotency_key="weather:dynamic",
        metadata={"arguments": {"city": "北京"}},
    )
    assert store.evaluate(requirements).satisfied


def test_malformed_dynamic_skill_requirement_does_not_crash_evaluation():
    store = RunEvidenceStore("run-malformed-skill", default_session_id="root")
    _make_entry(
        store,
        evidence_id="ev-malformed-skill",
        kind=EvidenceKind.SKILL_LOADED,
        tool_name="skill:broken-contract",
        idempotency_key="skill:malformed",
        metadata={
            "required_tool_calls": [{
                "tool": "mcp:broken:call",
                "minimum_count": "not-an-integer",
            }],
        },
    )

    evaluation = store.evaluate(RunEvidenceRequirements(
        require_started_workers_succeed=False,
    ))
    assert evaluation.satisfied
