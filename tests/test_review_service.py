from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from agent.session.models import (
    AgentKind,
    AgentRunResult,
    AgentRunStatus,
    SessionMode,
)
from agent.session.result_contract import (
    Finding,
    FindingCategory,
    FindingSeverity,
    SubagentReport,
    SubagentReportStatus,
)
from app.storage.sqlite import SqliteStorageBackend
from context.workspace_facts import FileFact, WorkspaceSnapshot
from server.services.review_service import ReviewService


def _snapshot(repo_path, revision: str = "revision-1") -> WorkspaceSnapshot:
    changed = repo_path / "sample.py"
    return WorkspaceSnapshot(
        project_root=str(repo_path),
        is_git_repo=True,
        head_commit="abc123",
        revision=revision,
        current_patch=(
            "diff --git a/sample.py b/sample.py\n"
            "--- a/sample.py\n"
            "+++ b/sample.py\n"
            "@@ -1 +1 @@\n"
            "-old = True\n"
            "+old = False\n"
        ),
        files=(FileFact(path=str(changed), digest="digest-1"),),
    )


class _ReviewRuntime:
    def __init__(
        self,
        *,
        status: AgentRunStatus = AgentRunStatus.COMPLETED,
        snippet: str = "old = False",
    ):
        self.status = status
        self.snippet = snippet
        self.calls = []

    def run_explicit_delegation(self, parent_session_id, **kwargs):
        self.calls.append((parent_session_id, kwargs))
        child_id = f"child-{len(self.calls)}"
        callback = kwargs.get("child_created_callback")
        if callback is not None:
            callback(SimpleNamespace(id=child_id))
        lens = kwargs["request"].description
        report = SubagentReport(
            status=(
                SubagentReportStatus.PARTIAL
                if self.status is AgentRunStatus.PARTIAL
                else SubagentReportStatus.COMPLETED
            ),
            findings=(
                Finding(
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.BUG,
                    title="Shared defect",
                    description=f"Confirmed by {lens}",
                    file_path="sample.py",
                    line_start=1,
                    line_end=1,
                    code_snippet=self.snippet,
                    verification="Static trace",
                ),
            ),
            summary="Review complete",
        )
        return AgentRunResult(
            agent_name="code-reviewer",
            session_id=child_id,
            status=self.status,
            summary="Review complete",
            tokens_used=100,
            report=report,
        )


class _BlockingReviewRuntime:
    def __init__(self):
        threading = __import__("threading")
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.cancel_calls = []

    def run_explicit_delegation(self, parent_session_id, **kwargs):
        child_id = "blocking-child"
        kwargs["child_created_callback"](SimpleNamespace(id=child_id))
        self.started.set()
        self.cancelled.wait(2)
        return AgentRunResult(
            agent_name="code-reviewer",
            session_id=child_id,
            status=AgentRunStatus.CANCELLED,
            summary="Cancelled",
            error="Cancelled",
        )

    def cancel_session(self, session_id, detail=""):
        self.cancel_calls.append((session_id, detail))
        self.cancelled.set()
        return True


class _GateReviewRuntime:
    def __init__(self):
        threading = __import__("threading")
        self.started = threading.Event()
        self.release = threading.Event()

    def run_explicit_delegation(self, parent_session_id, **kwargs):
        child_id = "gated-child"
        kwargs["child_created_callback"](SimpleNamespace(id=child_id))
        self.started.set()
        self.release.wait(2)
        return AgentRunResult(
            agent_name="code-reviewer",
            session_id=child_id,
            status=AgentRunStatus.COMPLETED,
            summary="Complete",
            report=SubagentReport(
                status=SubagentReportStatus.NO_FINDINGS,
                summary="No findings",
            ),
        )


class _FailingReviewRuntime:
    def run_explicit_delegation(self, parent_session_id, **kwargs):
        kwargs["child_created_callback"](SimpleNamespace(id="failed-child"))
        raise RuntimeError("reviewer crashed")


class _SnapshotManager:
    def __init__(self, root):
        self.root = root
        self.created = []
        self.discarded = []

    def materialize(self, snapshot_id, snapshot):
        path = self.root / snapshot_id
        path.mkdir(parents=True)
        (path / "sample.py").write_text(
            "old = False\n",
            encoding="utf-8",
        )
        self.created.append((snapshot_id, snapshot.revision, str(path)))
        return SimpleNamespace(
            path=str(path),
            workspace_revision=snapshot.revision,
            head_commit=snapshot.head_commit,
        )

    def discard(self, snapshot_path):
        self.discarded.append(snapshot_path)

    def validate(self, snapshot_path):
        path = __import__("pathlib").Path(snapshot_path)
        if not path.is_dir():
            raise ValueError("Review snapshot is unavailable")
        return str(path.resolve())


def _service(
    tmp_path,
    monkeypatch,
    *,
    runtime=None,
    snapshots=None,
    event_callback=None,
):
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    storage = SqliteStorageBackend(str(tmp_path / "sessions.db"))
    session = storage.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(repo_path),
        title="Review target",
    )
    values = list(snapshots or [_snapshot(repo_path), _snapshot(repo_path)])

    def next_snapshot(_path):
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    monkeypatch.setattr(
        "server.services.review_service.capture_workspace_snapshot",
        next_snapshot,
    )
    snapshot_manager = _SnapshotManager(tmp_path / "review-snapshots")
    service = ReviewService(
        storage=storage,
        runtime=runtime or _ReviewRuntime(),
        repo_path=str(repo_path),
        snapshot_manager=snapshot_manager,
        event_callback=event_callback,
    )
    return service, storage, session


def _wait_for_terminal(service: ReviewService, job_id: str):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = service.get_review(job_id)
        if job["status"] not in {
            "queued",
            "running",
            "aggregating",
            "cancelling",
        }:
            return job
        time.sleep(0.01)
    raise AssertionError("review did not reach a terminal state")


def test_review_reuses_runtime_shared_executor(tmp_path, monkeypatch):
    runtime = _ReviewRuntime()
    runtime._shared_executor = ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="shared-review-test",
    )
    service, _, session = _service(
        tmp_path,
        monkeypatch,
        runtime=runtime,
    )

    def fail_if_private_pool_is_created(*args, **kwargs):
        raise AssertionError("ReviewService created a private executor")

    monkeypatch.setattr(
        "agent.session.executor_pool.ThreadPoolExecutor",
        fail_if_private_pool_is_created,
    )
    try:
        started = service.start_review(session.id)
        job = _wait_for_terminal(service, started["id"])
        assert job["status"] == "completed"
        assert len(runtime.calls) == 3
    finally:
        runtime._shared_executor.shutdown(wait=True, cancel_futures=True)


def test_review_runs_three_read_only_lenses_and_deduplicates_findings(
    tmp_path,
    monkeypatch,
):
    runtime = _ReviewRuntime()
    service, storage, session = _service(
        tmp_path,
        monkeypatch,
        runtime=runtime,
    )

    started = service.start_review(session.id, focus="Check persistence")
    job = _wait_for_terminal(service, started["id"])

    assert job["status"] == "completed"
    assert len(job["tasks"]) == 3
    assert len(runtime.calls) == 3
    assert all(
        call[1]["parent_intent"].value == "analysis"
        for call in runtime.calls
    )
    assert all(
        call[1]["contract"].require_deliverables == {"ReportFindings": 1}
        for call in runtime.calls
    )
    assert all(
        call[1]["execution_repo_path"] == job["snapshot_path"]
        for call in runtime.calls
    )
    assert {
        call[1]["child_metadata"]["review_task_id"]
        for call in runtime.calls
    } == {task["id"] for task in job["tasks"]}
    assert job["result"]["finding_count"] == 1
    finding = job["result"]["findings"][0]
    assert finding["corroboration_count"] == 3
    assert set(finding["reported_by"]) == {
        "correctness",
        "concurrency_security",
        "tests_contracts",
    }
    assert job["result"]["total_tokens"] == 300

    with storage.store._connect() as conn:
        message_count = conn.execute(
            "SELECT COUNT(*) FROM review_messages WHERE job_id = ?",
            (job["id"],),
        ).fetchone()[0]
    assert message_count == 9
    assert all(len(task["attempts"]) == 1 for task in job["tasks"])
    assert {
        task["attempts"][0]["status"] for task in job["tasks"]
    } == {"completed"}


def test_review_is_stale_when_workspace_revision_changes(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    service, _, session = _service(
        tmp_path,
        monkeypatch,
        snapshots=[
            _snapshot(repo_path, "revision-1"),
            _snapshot(repo_path, "revision-2"),
        ],
    )

    started = service.start_review(session.id, max_agents=1)
    job = _wait_for_terminal(service, started["id"])

    assert job["status"] == "stale"
    assert job["result"]["snapshot_current"] is False
    assert job["result"]["current_workspace_revision"] == "revision-2"


def test_partial_reviewer_makes_aggregate_partial(tmp_path, monkeypatch):
    service, _, session = _service(
        tmp_path,
        monkeypatch,
        runtime=_ReviewRuntime(status=AgentRunStatus.PARTIAL),
    )

    started = service.start_review(session.id, max_agents=1)
    job = _wait_for_terminal(service, started["id"])

    assert job["tasks"][0]["status"] == "partial"
    assert job["status"] == "partial"


def test_failed_reviewer_attempt_keeps_child_session_and_error(
    tmp_path,
    monkeypatch,
):
    service, _, session = _service(
        tmp_path,
        monkeypatch,
        runtime=_FailingReviewRuntime(),
    )

    started = service.start_review(session.id, max_agents=1)
    job = _wait_for_terminal(service, started["id"])
    task = job["tasks"][0]

    assert job["status"] == "partial"
    assert task["status"] == "failed"
    assert task["child_session_id"] == "failed-child"
    assert task["attempts"][0]["status"] == "failed"
    assert task["attempts"][0]["child_session_id"] == "failed-child"
    assert task["attempts"][0]["error"] == "reviewer crashed"


def test_service_restart_recovers_interrupted_task_from_frozen_snapshot(
    tmp_path,
    monkeypatch,
):
    service, storage, session = _service(tmp_path, monkeypatch)
    now = "2026-01-01T00:00:00+00:00"
    with storage.store._connect() as conn:
        conn.execute(
            """
            INSERT INTO review_jobs (
                id, session_id, status, workspace_revision, head_commit,
                snapshot_path, diff_hash, changed_files_json, focus,
                result_json, error, created_at, updated_at
            ) VALUES (
                'interrupted', ?, 'running', 'revision-1', 'head',
                ?, 'hash', '["sample.py"]', '', '{}', '', ?, ?
            )
            """,
            (
                session.id,
                str(service._snapshot_manager.root / "interrupted"),
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO review_tasks (
                id, job_id, lens, title, status, created_at, updated_at
            ) VALUES (
                'interrupted-task', 'interrupted', 'correctness',
                'Correctness', 'running', ?, ?
            )
            """,
            (now, now),
        )
    interrupted_snapshot = service._snapshot_manager.root / "interrupted"
    interrupted_snapshot.mkdir(parents=True)
    (interrupted_snapshot / "sample.py").write_text(
        "old = False\n",
        encoding="utf-8",
    )

    recovered_runtime = _ReviewRuntime()
    restarted = ReviewService(
        storage=storage,
        runtime=recovered_runtime,
        repo_path=service._repo_path,
        snapshot_manager=service._snapshot_manager,
    )
    job = _wait_for_terminal(restarted, "interrupted")

    assert job["status"] == "completed"
    assert job["tasks"][0]["status"] == "completed"
    assert [
        attempt["status"] for attempt in job["tasks"][0]["attempts"]
    ] == ["interrupted", "completed"]
    assert len(recovered_runtime.calls) == 1


def test_service_restart_finishes_interrupted_cancellation_without_dispatch(
    tmp_path,
    monkeypatch,
):
    service, storage, session = _service(tmp_path, monkeypatch)
    snapshot_path = service._snapshot_manager.root / "cancelling"
    snapshot_path.mkdir(parents=True)
    now = "2026-01-01T00:00:00+00:00"
    with storage.store._connect() as conn:
        conn.execute(
            """
            INSERT INTO review_jobs (
                id, session_id, status, workspace_revision, head_commit,
                snapshot_path, diff_hash, changed_files_json, focus,
                result_json, error, created_at, updated_at
            ) VALUES (
                'cancelling', ?, 'cancelling', 'revision-1', 'head',
                ?, 'hash', '["sample.py"]', '', '{}', '', ?, ?
            )
            """,
            (session.id, str(snapshot_path), now, now),
        )
        conn.execute(
            """
            INSERT INTO review_tasks (
                id, job_id, lens, title, status, created_at, updated_at
            ) VALUES (
                'cancelling-task', 'cancelling', 'correctness',
                'Correctness', 'running', ?, ?
            )
            """,
            (now, now),
        )

    recovered_runtime = _ReviewRuntime()
    restarted = ReviewService(
        storage=storage,
        runtime=recovered_runtime,
        repo_path=service._repo_path,
        snapshot_manager=service._snapshot_manager,
    )
    job = restarted.get_review("cancelling")

    assert job["status"] == "cancelled"
    assert job["tasks"][0]["status"] == "cancelled"
    assert job["tasks"][0]["attempts"][0]["status"] == "cancelled"
    assert recovered_runtime.calls == []


def test_queued_review_can_be_cancelled_before_dispatch(tmp_path, monkeypatch):
    service, _, session = _service(tmp_path, monkeypatch)
    release = __import__("threading").Event()
    service._run_job = lambda _job_id, _diff: release.wait(2)

    started = service.start_review(session.id)
    cancelled = service.cancel_review(started["id"])
    release.set()

    assert cancelled["status"] == "cancelled"
    assert {task["status"] for task in cancelled["tasks"]} == {"cancelled"}


def test_retry_creates_a_new_job_linking_to_the_previous_review(
    tmp_path,
    monkeypatch,
):
    service, _, session = _service(tmp_path, monkeypatch)
    first = service.start_review(session.id, max_agents=1)
    first = _wait_for_terminal(service, first["id"])

    retried = service.retry_review(first["id"])
    retried = _wait_for_terminal(service, retried["id"])

    assert retried["id"] != first["id"]
    assert retried["retry_of"] == first["id"]
    assert retried["workspace_revision"] == first["workspace_revision"]


def test_terminal_review_snapshot_requires_explicit_release(
    tmp_path,
    monkeypatch,
):
    service, _, session = _service(tmp_path, monkeypatch)
    started = service.start_review(session.id, max_agents=1)
    completed = _wait_for_terminal(service, started["id"])

    released = service.release_snapshot(completed["id"])

    assert completed["snapshot_available"] is True
    assert released["snapshot_available"] is False
    assert service._snapshot_manager.discarded == [completed["snapshot_path"]]


def test_running_review_cancels_recorded_child_session(tmp_path, monkeypatch):
    runtime = _BlockingReviewRuntime()
    service, storage, session = _service(
        tmp_path,
        monkeypatch,
        runtime=runtime,
    )

    started = service.start_review(session.id, max_agents=1)
    assert runtime.started.wait(1)
    child = storage.create_session(
        agent_name="code-reviewer",
        mode=SessionMode.SUBAGENT,
        agent_kind=AgentKind.NAMED_SUBAGENT,
        repo_path=str(tmp_path / "repo"),
        title="blocking",
        parent_id=session.id,
        metadata={"review_job_id": started["id"]},
    )
    service._record_child_session(started["tasks"][0]["id"], child.id)

    cancelling = service.cancel_review(started["id"])
    terminal = _wait_for_terminal(service, started["id"])

    # Cancellation may complete concurrently before cancel_review() reloads the
    # job; both the acknowledged intermediate state and terminal state are valid.
    assert cancelling["status"] in {"cancelling", "cancelled"}
    assert terminal["status"] == "cancelled"
    assert runtime.cancel_calls


def test_review_updates_are_emitted_outside_assistant_content(
    tmp_path,
    monkeypatch,
):
    events = []
    service, _, session = _service(
        tmp_path,
        monkeypatch,
        event_callback=lambda session_id, event: events.append(
            (session_id, event.to_dict())
        ),
    )

    started = service.start_review(session.id, max_agents=1)
    completed = _wait_for_terminal(service, started["id"])
    deadline = time.monotonic() + 1
    while (
        not any(event["status"] == completed["status"] for _, event in events)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)

    assert events
    assert all(session_id == session.id for session_id, _ in events)
    assert all(event["type"] == "review_updated" for _, event in events)
    assert any(
        event["status"] == completed["status"] for _, event in events
    )


def test_single_partial_reviewer_can_retry_on_the_same_snapshot(
    tmp_path,
    monkeypatch,
):
    runtime = _ReviewRuntime(status=AgentRunStatus.PARTIAL)
    service, _, session = _service(
        tmp_path,
        monkeypatch,
        runtime=runtime,
    )
    started = service.start_review(session.id, max_agents=1)
    partial = _wait_for_terminal(service, started["id"])
    task_id = partial["tasks"][0]["id"]

    runtime.status = AgentRunStatus.COMPLETED
    retried = service.retry_task(partial["id"], task_id)
    completed = _wait_for_terminal(service, retried["id"])

    assert retried["id"] == partial["id"]
    assert completed["status"] == "completed"
    assert completed["tasks"][0]["status"] == "completed"
    assert [
        attempt["status"] for attempt in completed["tasks"][0]["attempts"]
    ] == ["partial", "completed"]
    assert [
        attempt["attempt_number"]
        for attempt in completed["tasks"][0]["attempts"]
    ] == [1, 2]
    assert len(runtime.calls) == 2


def test_invalid_finding_evidence_is_excluded_from_authoritative_results(
    tmp_path,
    monkeypatch,
):
    service, _, session = _service(
        tmp_path,
        monkeypatch,
        runtime=_ReviewRuntime(snippet="this does not exist"),
    )

    started = service.start_review(session.id, max_agents=1)
    job = _wait_for_terminal(service, started["id"])

    assert job["status"] == "partial"
    assert job["result"]["finding_count"] == 0
    assert job["result"]["invalid_finding_count"] == 1
    invalid = job["result"]["invalid_findings"][0]
    assert invalid["evidence_status"] == "invalid"
    assert "does not match" in invalid["evidence_error"]


def test_concurrent_start_reuses_one_active_snapshot_and_job(
    tmp_path,
    monkeypatch,
):
    runtime = _GateReviewRuntime()
    service, _, session = _service(
        tmp_path,
        monkeypatch,
        runtime=runtime,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.start_review, session.id, max_agents=1)
            for _ in range(2)
        ]
        jobs = [future.result() for future in futures]

    assert jobs[0]["id"] == jobs[1]["id"]
    assert len(service._snapshot_manager.created) == 1
    runtime.release.set()
    _wait_for_terminal(service, jobs[0]["id"])
