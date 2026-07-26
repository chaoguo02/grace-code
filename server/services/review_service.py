"""Persistent, read-only multi-agent code review orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.session.models import ExplicitDelegationRequest, SessionMode
from agent.session.task_contract import TaskContract
from agent.task import TaskIntent
from context.workspace_facts import capture_workspace_snapshot
from server.events import WsReviewUpdated
from server.services.review_snapshot import ReviewSnapshotManager

logger = logging.getLogger(__name__)

_LENSES = (
    (
        "correctness",
        "Correctness and data flow",
        "Trace logic, state transitions, error handling, edge cases, and "
        "cross-file behavior. Prioritize reproducible defects.",
    ),
    (
        "concurrency_security",
        "Concurrency, security, and consistency",
        "Inspect races, stale state, authorization, unsafe boundaries, "
        "idempotency, transactionality, and failure recovery.",
    ),
    (
        "tests_contracts",
        "Tests, API contracts, and regressions",
        "Check public contracts, persistence/reload behavior, missing regression "
        "tests, compatibility risks, and verification gaps.",
    ),
)

_TERMINAL_TASK_STATES = {"completed", "partial", "failed", "cancelled"}
_ACTIVE_JOB_STATES = {"queued", "running", "aggregating", "cancelling"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewService:
    """Own review persistence, dispatch, snapshot validation, and aggregation."""

    def __init__(
        self,
        *,
        storage,
        runtime,
        repo_path: str,
        max_task_steps: int = 30,
        task_token_budget: int = 30_000,
        snapshot_manager: ReviewSnapshotManager | None = None,
        event_callback=None,
    ) -> None:
        self._storage = storage
        self._store = storage.store
        self._runtime = runtime
        self._repo_path = str(Path(repo_path).resolve())
        self._max_task_steps = max_task_steps
        self._task_token_budget = task_token_budget
        self._snapshot_manager = snapshot_manager or ReviewSnapshotManager(
            self._repo_path,
        )
        self._event_callback = event_callback
        self._start_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()
        self._init_tables()
        recoverable_jobs = self._reconcile_interrupted_jobs()
        for job_id in recoverable_jobs:
            try:
                self._launch_job(job_id, self._snapshot_diff(job_id))
            except Exception as exc:
                logger.exception("Unable to resume review job %s", job_id)
                self._update_job(
                    job_id,
                    status="failed",
                    error=f"Review recovery failed: {exc}",
                    terminal=True,
                )

    def _init_tables(self) -> None:
        with self._store._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_jobs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workspace_revision TEXT NOT NULL,
                    head_commit TEXT NOT NULL DEFAULT '',
                    snapshot_path TEXT NOT NULL DEFAULT '',
                    retry_of TEXT NOT NULL DEFAULT '',
                    diff_hash TEXT NOT NULL,
                    changed_files_json TEXT NOT NULL DEFAULT '[]',
                    focus TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_review_jobs_session_created
                    ON review_jobs(session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_review_jobs_status
                    ON review_jobs(status);

                CREATE TABLE IF NOT EXISTS review_tasks (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    lens TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    child_session_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NULL,
                    completed_at TEXT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES review_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_review_tasks_job
                    ON review_tasks(job_id, created_at);

                CREATE TABLE IF NOT EXISTS review_task_attempts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    child_session_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES review_jobs(id),
                    FOREIGN KEY (task_id) REFERENCES review_tasks(id),
                    UNIQUE (task_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS idx_review_task_attempts_task
                    ON review_task_attempts(task_id, attempt_number);

                CREATE TABLE IF NOT EXISTS review_messages (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sender_session_id TEXT NOT NULL DEFAULT '',
                    recipient_session_id TEXT NOT NULL DEFAULT '',
                    message_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    delivery_state TEXT NOT NULL DEFAULT 'acknowledged',
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT NULL,
                    FOREIGN KEY (job_id) REFERENCES review_jobs(id),
                    FOREIGN KEY (task_id) REFERENCES review_tasks(id)
                );
                CREATE INDEX IF NOT EXISTS idx_review_messages_job
                    ON review_messages(job_id, created_at);
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(review_jobs)")
            }
            if "snapshot_path" not in columns:
                conn.execute(
                    "ALTER TABLE review_jobs "
                    "ADD COLUMN snapshot_path TEXT NOT NULL DEFAULT ''"
                )
            if "retry_of" not in columns:
                conn.execute(
                    "ALTER TABLE review_jobs "
                    "ADD COLUMN retry_of TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                INSERT INTO review_task_attempts (
                    id, job_id, task_id, attempt_number, status,
                    child_session_id, result_json, error, started_at,
                    completed_at, created_at, updated_at
                )
                SELECT
                    lower(hex(randomblob(16))), task.job_id, task.id, 1,
                    task.status, task.child_session_id, task.result_json,
                    task.error, COALESCE(task.started_at, task.created_at),
                    task.completed_at, task.created_at, task.updated_at
                FROM review_tasks AS task
                WHERE task.status != 'queued'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM review_task_attempts AS attempt
                    WHERE attempt.task_id = task.id
                  )
                """
            )

    def _reconcile_interrupted_jobs(self) -> list[str]:
        """Recover durable tasks without pretending old worker threads survived."""
        now = _utc_now()
        recoverable: list[str] = []
        with self._store._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, status, snapshot_path
                FROM review_jobs
                WHERE status IN ('queued', 'running', 'aggregating', 'cancelling')
                ORDER BY created_at, id
                """
            ).fetchall()
            for row in rows:
                job_id = str(row["id"])
                if row["status"] == "cancelling":
                    conn.execute(
                        """
                        UPDATE review_task_attempts
                        SET status = 'cancelled',
                            error = CASE
                                WHEN error = '' THEN
                                    'Cancelled during service restart'
                                ELSE error
                            END,
                            completed_at = COALESCE(completed_at, ?),
                            updated_at = ?
                        WHERE job_id = ? AND status IN ('queued', 'running')
                        """,
                        (now, now, job_id),
                    )
                    conn.execute(
                        """
                        UPDATE review_tasks
                        SET status = 'cancelled',
                            error = CASE
                                WHEN error = '' THEN 'Cancelled during service restart'
                                ELSE error
                            END,
                            completed_at = COALESCE(completed_at, ?),
                            updated_at = ?
                        WHERE job_id = ? AND status IN ('queued', 'running')
                        """,
                        (now, now, job_id),
                    )
                    conn.execute(
                        """
                        UPDATE review_jobs
                        SET status = 'cancelled',
                            error = 'Cancellation completed during service restart',
                            completed_at = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, job_id),
                    )
                    continue

                snapshot_path = str(row["snapshot_path"] or "")
                try:
                    self._snapshot_manager.validate(snapshot_path)
                except ValueError as exc:
                    conn.execute(
                        """
                        UPDATE review_task_attempts
                        SET status = 'interrupted',
                            error = CASE
                                WHEN error = '' THEN ? ELSE error
                            END,
                            completed_at = COALESCE(completed_at, ?),
                            updated_at = ?
                        WHERE job_id = ? AND status IN ('queued', 'running')
                        """,
                        (f"Recovery unavailable: {exc}", now, now, job_id),
                    )
                    conn.execute(
                        """
                        UPDATE review_tasks
                        SET status = 'failed',
                            error = CASE
                                WHEN status IN ('queued', 'running') THEN ?
                                ELSE error
                            END,
                            completed_at = CASE
                                WHEN status IN ('queued', 'running') THEN ?
                                ELSE completed_at
                            END,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (f"Recovery unavailable: {exc}", now, now, job_id),
                    )
                    conn.execute(
                        """
                        UPDATE review_jobs
                        SET status = 'failed', error = ?,
                            completed_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (f"Review recovery unavailable: {exc}", now, now, job_id),
                    )
                    continue

                conn.execute(
                    """
                    UPDATE review_task_attempts
                    SET status = 'interrupted',
                        error = CASE
                            WHEN error = '' THEN 'Interrupted by service restart'
                            ELSE error
                        END,
                        completed_at = COALESCE(completed_at, ?),
                        updated_at = ?
                    WHERE job_id = ? AND status IN ('queued', 'running')
                    """,
                    (now, now, job_id),
                )
                conn.execute(
                    """
                    UPDATE review_tasks
                    SET status = 'queued',
                        child_session_id = '',
                        error = 'Retrying after service restart',
                        started_at = NULL,
                        completed_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND status IN ('queued', 'running')
                    """,
                    (now, job_id),
                )
                conn.execute(
                    """
                    UPDATE review_jobs
                    SET status = 'queued',
                        error = 'Resuming interrupted review',
                        completed_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
                recoverable.append(job_id)
        return recoverable

    def start_review(
        self,
        session_id: str,
        *,
        focus: str = "",
        max_agents: int = 3,
        retry_of: str = "",
    ) -> dict[str, Any]:
        session = self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        if session.mode is not SessionMode.PRIMARY:
            raise ValueError("Multi-agent review requires a primary session")
        if max_agents < 1 or max_agents > 3:
            raise ValueError("max_agents must be between 1 and 3")
        if self._storage.get_active_run(session_id) is not None:
            raise ValueError(
                "Wait for the active session run to finish before starting review"
            )

        snapshot = capture_workspace_snapshot(self._repo_path)
        if not snapshot.is_git_repo:
            raise ValueError(
                snapshot.error or "Multi-agent review requires a Git repository"
            )
        diff_text, changed_files = self._review_input(session_id, snapshot)
        if not diff_text.strip() and not changed_files:
            raise ValueError("No code changes are available to review")
        diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

        selected_lenses = _LENSES[:max_agents]
        with self._start_lock:
            existing = self._active_job(
                session_id,
                snapshot.revision,
                diff_hash,
            )
            if existing is not None:
                return self.get_review(existing)
            job_id = self._create_review_job(
                session_id=session_id,
                snapshot=snapshot,
                diff_hash=diff_hash,
                changed_files=changed_files,
                focus=focus,
                selected_lenses=selected_lenses,
                retry_of=retry_of,
            )

        self._launch_job(job_id, diff_text)
        job = self.get_review(job_id)
        self._emit_update(job)
        return job

    def retry_review(self, job_id: str) -> dict[str, Any]:
        previous = self.get_review(job_id)
        if previous["status"] in _ACTIVE_JOB_STATES:
            raise ValueError("An active review cannot be retried")
        return self.start_review(
            previous["session_id"],
            focus=previous["focus"],
            max_agents=max(1, min(len(previous["tasks"]), 3)),
            retry_of=job_id,
        )

    def retry_task(self, job_id: str, task_id: str) -> dict[str, Any]:
        with self._start_lock:
            job = self.get_review(job_id)
            if job["status"] in _ACTIVE_JOB_STATES:
                raise ValueError("A task cannot be retried while its review is active")
            if not job["snapshot_available"]:
                raise ValueError("The frozen review snapshot is unavailable")
            self._snapshot_manager.validate(job["snapshot_path"])
            task = next(
                (item for item in job["tasks"] if item["id"] == task_id),
                None,
            )
            if task is None:
                raise ValueError(f"Unknown review task: {task_id}")
            if task["status"] not in {"partial", "failed", "cancelled"}:
                raise ValueError(
                    "Only partial, failed, or cancelled reviewers can be retried"
                )
            now = _utc_now()
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE review_tasks
                    SET status = 'queued', child_session_id = '',
                        result_json = '{}', error = '',
                        started_at = NULL, completed_at = NULL, updated_at = ?
                    WHERE id = ? AND job_id = ?
                    """,
                    (now, task_id, job_id),
                )
                conn.execute(
                    """
                    UPDATE review_jobs
                    SET status = 'queued', result_json = '{}', error = '',
                        completed_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
                self._insert_message(
                    conn,
                    job_id=job_id,
                    task_id=task_id,
                    sender_session_id=job["session_id"],
                    message_type="task_retry_requested",
                    payload={"previous_status": task["status"]},
                    now=now,
                )
            self._launch_job(job_id, self._snapshot_diff(job_id))
        retried = self.get_review(job_id)
        self._emit_update(retried)
        return retried

    def cancel_review(self, job_id: str) -> dict[str, Any]:
        job = self.get_review(job_id)
        if job["status"] not in _ACTIVE_JOB_STATES:
            return job
        now = _utc_now()
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM review_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown review: {job_id}")
            if row["status"] == "queued":
                conn.execute(
                    """
                    UPDATE review_jobs
                    SET status = 'cancelled', updated_at = ?, completed_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now, now, job_id),
                )
                conn.execute(
                    """
                    UPDATE review_tasks
                    SET status = 'cancelled', updated_at = ?, completed_at = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (now, now, job_id),
                )
            elif row["status"] in {"running", "aggregating"}:
                conn.execute(
                    """
                    UPDATE review_jobs
                    SET status = 'cancelling', updated_at = ?
                    WHERE id = ? AND status IN ('running', 'aggregating')
                    """,
                    (now, job_id),
                )

        for child in self._store.list_child_sessions(job["session_id"]):
            if child.metadata.get("review_job_id") == job_id:
                self._runtime.cancel_session(
                    child.id,
                    detail="Multi-agent review cancelled by user",
                )
        cancelled = self.get_review(job_id)
        self._emit_update(cancelled)
        return cancelled

    def release_snapshot(self, job_id: str) -> dict[str, Any]:
        job = self.get_review(job_id)
        if job["status"] in _ACTIVE_JOB_STATES:
            raise ValueError("An active review snapshot cannot be released")
        snapshot_path = str(job.get("snapshot_path", "") or "")
        if not snapshot_path:
            return job
        self._snapshot_manager.discard(snapshot_path)
        now = _utc_now()
        with self._store._connect() as conn:
            conn.execute(
                """
                UPDATE review_jobs
                SET snapshot_path = '', updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
        released = self.get_review(job_id)
        self._emit_update(released)
        return released

    def get_review(self, job_id: str) -> dict[str, Any]:
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM review_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown review: {job_id}")
            tasks = conn.execute(
                "SELECT * FROM review_tasks WHERE job_id = ? ORDER BY created_at, id",
                (job_id,),
            ).fetchall()
            attempts = conn.execute(
                """
                SELECT * FROM review_task_attempts
                WHERE job_id = ?
                ORDER BY task_id, attempt_number
                """,
                (job_id,),
            ).fetchall()
        return self._render_job(row, tasks, attempts)

    def get_latest_review(self, session_id: str) -> dict[str, Any] | None:
        with self._store._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM review_jobs
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self.get_review(row["id"]) if row is not None else None

    def _active_job(
        self,
        session_id: str,
        revision: str,
        diff_hash: str,
    ) -> str | None:
        with self._store._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM review_jobs
                WHERE session_id = ? AND workspace_revision = ? AND diff_hash = ?
                  AND status IN ('queued', 'running', 'aggregating', 'cancelling')
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id, revision, diff_hash),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def _create_review_job(
        self,
        *,
        session_id: str,
        snapshot,
        diff_hash: str,
        changed_files: list[str],
        focus: str,
        selected_lenses,
        retry_of: str,
    ) -> str:
        job_id = uuid.uuid4().hex[:16]
        materialized = self._snapshot_manager.materialize(job_id, snapshot)
        now = _utc_now()
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO review_jobs (
                        id, session_id, status, workspace_revision, head_commit,
                        snapshot_path, retry_of, diff_hash, changed_files_json,
                        focus, result_json, error, created_at, updated_at
                    ) VALUES (
                        ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, '{}', '', ?, ?
                    )
                    """,
                    (
                        job_id,
                        session_id,
                        snapshot.revision,
                        snapshot.head_commit,
                        materialized.path,
                        retry_of,
                        diff_hash,
                        json.dumps(changed_files, ensure_ascii=False),
                        focus.strip(),
                        now,
                        now,
                    ),
                )
                for lens, title, _ in selected_lenses:
                    task_id = uuid.uuid4().hex[:16]
                    conn.execute(
                        """
                        INSERT INTO review_tasks (
                            id, job_id, lens, title, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
                        """,
                        (task_id, job_id, lens, title, now, now),
                    )
                    self._insert_message(
                        conn,
                        job_id=job_id,
                        task_id=task_id,
                        sender_session_id=session_id,
                        message_type="task_assigned",
                        payload={
                            "lens": lens,
                            "workspace_revision": snapshot.revision,
                            "snapshot_path": materialized.path,
                            "diff_hash": diff_hash,
                        },
                        now=now,
                    )
        except Exception:
            self._snapshot_manager.discard(materialized.path)
            raise
        return job_id

    def _review_input(self, session_id: str, snapshot) -> tuple[str, list[str]]:
        changed_files = {
            str(Path(fact.path).resolve().relative_to(Path(self._repo_path)))
            for fact in snapshot.files
        }
        diff_text = snapshot.current_patch
        if not diff_text.strip():
            rows = self._storage.get_session_diffs(session_id)
            chunks = []
            for row in rows:
                path = str(row.get("file_path", "") or "")
                if path:
                    changed_files.add(path)
                content = str(row.get("diff_content", "") or "")
                if content:
                    chunks.append(content)
            diff_text = "\n".join(chunks)
        return diff_text, sorted(changed_files)

    def _snapshot_diff(self, job_id: str) -> str:
        job = self.get_review(job_id)
        snapshot_path = self._snapshot_manager.validate(job["snapshot_path"])
        return capture_workspace_snapshot(snapshot_path).current_patch

    def _launch_job(self, job_id: str, diff_text: str) -> None:
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, diff_text),
            name=f"review-{job_id}",
            daemon=False,
        )
        with self._threads_lock:
            existing = self._threads.get(job_id)
            if existing is not None and existing.is_alive():
                raise ValueError("Review job already has an active coordinator")
            self._threads[job_id] = thread
        thread.start()

    def _run_job(self, job_id: str, diff_text: str) -> None:
        try:
            if not self._transition_job(
                job_id,
                from_status="queued",
                to_status="running",
            ):
                return
            job = self.get_review(job_id)
            tasks = [
                task for task in job["tasks"] if task["status"] == "queued"
            ]
            if tasks:
                with ThreadPoolExecutor(
                    max_workers=len(tasks),
                    thread_name_prefix=f"review-{job_id}",
                ) as pool:
                    futures = {
                        pool.submit(
                            self._run_task,
                            job,
                            task,
                            diff_text,
                        ): task["id"]
                        for task in tasks
                    }
                    for future in as_completed(futures):
                        task_id = futures[future]
                        try:
                            future.result()
                        except Exception as exc:
                            logger.exception("Review task %s failed", task_id)
                            self._finish_task(
                                task_id,
                                status="failed",
                                error=str(exc) or type(exc).__name__,
                            )

            if self.get_review(job_id)["status"] == "cancelling":
                self._aggregate(job_id, forced_status="cancelled")
            else:
                self._update_job(job_id, status="aggregating")
                self._aggregate(job_id)
        except Exception as exc:
            logger.exception("Review job %s failed", job_id)
            self._update_job(
                job_id,
                status="failed",
                error=str(exc) or type(exc).__name__,
                terminal=True,
            )
        finally:
            with self._threads_lock:
                self._threads.pop(job_id, None)

    def _run_task(
        self,
        job: dict[str, Any],
        task: dict[str, Any],
        diff_text: str,
    ) -> None:
        task_id = task["id"]
        attempt_id = self._start_task_attempt(task_id)
        lens = next(item for item in _LENSES if item[0] == task["lens"])
        prompt = self._build_task_prompt(job, lens, diff_text)
        result = self._runtime.run_explicit_delegation(
            job["session_id"],
            request=ExplicitDelegationRequest(
                agent_name="code-reviewer",
                description=lens[1],
                prompt=prompt,
            ),
            parent_intent=TaskIntent.ANALYSIS,
            contract=TaskContract(
                max_steps=self._max_task_steps,
                budget_tokens=self._task_token_budget,
                require_deliverables={"ReportFindings": 1},
            ),
            execution_repo_path=job["snapshot_path"],
            child_metadata={
                "review_job_id": job["id"],
                "review_task_id": task_id,
                "review_workspace_revision": job["workspace_revision"],
            },
            child_created_callback=lambda child: self._record_child_session(
                task_id,
                child.id,
                attempt_id=attempt_id,
            ),
        )
        payload = result.to_dict()
        status = result.status.value
        if status not in _TERMINAL_TASK_STATES:
            status = "failed"
        self._finish_task(
            task_id,
            attempt_id=attempt_id,
            status=status,
            child_session_id=result.session_id,
            result=payload,
            error=result.error,
        )

    def _build_task_prompt(
        self,
        job: dict[str, Any],
        lens: tuple[str, str, str],
        diff_text: str,
    ) -> str:
        focus = (
            f"\nUser focus:\n{job['focus']}\n"
            if job.get("focus") else ""
        )
        clipped_diff = diff_text[:80_000]
        return (
            "OBJECTIVE\n"
            f"Review the code changes from the {lens[1]} perspective.\n\n"
            "IMMUTABLE REVIEW SNAPSHOT\n"
            f"- workspace_revision: {job['workspace_revision']}\n"
            f"- head_commit: {job['head_commit']}\n"
            f"- diff_hash: {job['diff_hash']}\n"
            f"- changed_files: {', '.join(job['changed_files'])}\n"
            f"{focus}\n"
            "LENS\n"
            f"{lens[2]}\n\n"
            "BOUNDARIES\n"
            "- Read-only: do not edit files or run write-capable commands.\n"
            "- Review only this snapshot and the listed change scope.\n"
            "- Bugs require path, line range, exact source snippet, and verification.\n"
            "- The aggregator independently rejects evidence that does not match.\n"
            "- Do not report style nits unless they cause a concrete maintenance risk.\n"
            "- Submit exactly one ReportFindings result, including no_findings.\n\n"
            "DIFF\n"
            f"{clipped_diff}"
        )

    def _aggregate(
        self,
        job_id: str,
        *,
        forced_status: str | None = None,
    ) -> None:
        job = self.get_review(job_id)
        current = capture_workspace_snapshot(self._repo_path)
        stale = current.revision != job["workspace_revision"]
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        invalid_findings: list[dict[str, Any]] = []
        total_tokens = 0
        task_states: dict[str, str] = {}

        for task in job["tasks"]:
            task_states[task["lens"]] = task["status"]
            raw_result = task.get("result") or {}
            total_tokens += int(raw_result.get("tokens_used", 0) or 0)
            report = raw_result.get("report")
            if not isinstance(report, dict):
                continue
            for finding in report.get("findings", []):
                if not isinstance(finding, dict):
                    continue
                item, evidence_error = self._validate_finding(
                    finding,
                    job["snapshot_path"],
                )
                if evidence_error:
                    invalid = dict(finding)
                    invalid["evidence_status"] = "invalid"
                    invalid["evidence_error"] = evidence_error
                    invalid["reported_by"] = [task["lens"]]
                    invalid_findings.append(invalid)
                    continue
                key = self._finding_key(item)
                existing = deduped.get(key)
                if existing is None:
                    item["reported_by"] = [task["lens"]]
                    item["corroboration_count"] = 1
                    deduped[key] = item
                else:
                    reporters = existing["reported_by"]
                    if task["lens"] not in reporters:
                        reporters.append(task["lens"])
                    existing["corroboration_count"] = len(reporters)

        severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        findings = sorted(
            deduped.values(),
            key=lambda item: (
                severity_order.get(str(item.get("severity")), 9),
                str(item.get("file_path", "")),
                int(item.get("line_start", 0) or 0),
            ),
        )
        incomplete = any(
            state in {"partial", "failed", "cancelled"}
            for state in task_states.values()
        ) or bool(invalid_findings)
        status = forced_status or (
            "stale" if stale else ("partial" if incomplete else "completed")
        )
        result = {
            "findings": findings,
            "finding_count": len(findings),
            "invalid_findings": invalid_findings,
            "invalid_finding_count": len(invalid_findings),
            "task_states": task_states,
            "total_tokens": total_tokens,
            "snapshot_current": not stale,
            "current_workspace_revision": current.revision,
        }
        self._update_job(
            job_id,
            status=status,
            result=result,
            terminal=True,
        )

    def _validate_finding(
        self,
        finding: dict[str, Any],
        snapshot_path: str,
    ) -> tuple[dict[str, Any], str]:
        item = dict(finding)
        category = str(item.get("category", ""))
        raw_path = str(item.get("file_path", "") or "").strip()
        if not raw_path:
            if category == "hypothesis":
                item["evidence_status"] = "hypothesis"
                return item, ""
            return item, "Concrete findings require file_path"

        snapshot = Path(snapshot_path).resolve()
        path = Path(raw_path)
        resolved = (path if path.is_absolute() else snapshot / path).resolve()
        try:
            relative = resolved.relative_to(snapshot)
        except ValueError:
            return item, "Finding path is outside the frozen snapshot"
        if not resolved.is_file():
            return item, "Finding path does not exist in the frozen snapshot"

        try:
            line_start = int(item.get("line_start", 0) or 0)
            line_end = int(item.get("line_end", 0) or line_start)
        except (TypeError, ValueError):
            return item, "Finding line range must be numeric"
        if line_start < 1 or line_end < line_start:
            return item, "Finding requires a valid one-based line range"

        lines = resolved.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        if line_end > len(lines):
            return item, (
                f"Finding line {line_end} exceeds snapshot file length "
                f"{len(lines)}"
            )
        selected_source = "\n".join(lines[line_start - 1:line_end])
        snippet = str(item.get("code_snippet", "") or "").strip()
        if category == "bug" and not snippet:
            return item, "Bug findings require an exact code_snippet"
        if snippet and snippet not in selected_source:
            return item, "code_snippet does not match the reported snapshot lines"
        if category == "bug" and not str(
            item.get("verification", "") or ""
        ).strip():
            return item, "Bug findings require verification evidence"

        item["file_path"] = relative.as_posix()
        item["line_start"] = line_start
        item["line_end"] = line_end
        item["evidence_status"] = "verified"
        return item, ""

    def _transition_job(
        self,
        job_id: str,
        *,
        from_status: str,
        to_status: str,
    ) -> bool:
        now = _utc_now()
        with self._store._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE review_jobs
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (to_status, now, job_id, from_status),
            )
        if cursor.rowcount:
            self._emit_update(self.get_review(job_id))
        return bool(cursor.rowcount)

    @staticmethod
    def _finding_key(finding: dict[str, Any]) -> tuple[Any, ...]:
        title = re.sub(r"\W+", " ", str(finding.get("title", "")).lower()).strip()
        return (
            str(finding.get("file_path", "")).lower(),
            int(finding.get("line_start", 0) or 0),
            str(finding.get("category", "")),
            title,
        )

    def _update_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
        terminal: bool = False,
    ) -> None:
        now = _utc_now()
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM review_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown review: {job_id}")
            if terminal and row["status"] == "cancelling":
                status = "cancelled"
            conn.execute(
                """
                UPDATE review_jobs
                SET status = ?, result_json = COALESCE(?, result_json),
                    error = ?, updated_at = ?,
                    completed_at = CASE WHEN ? THEN ? ELSE completed_at END
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False)
                    if result is not None else None,
                    error,
                    now,
                    1 if terminal else 0,
                    now,
                    job_id,
                ),
            )
        self._emit_update(self.get_review(job_id))

    def _start_task_attempt(self, task_id: str) -> str:
        now = _utc_now()
        attempt_id = uuid.uuid4().hex
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT job_id
                FROM review_tasks
                WHERE id = ? AND status = 'queued'
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Review task is not queued for dispatch: {task_id}"
                )
            number_row = conn.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_number
                FROM review_task_attempts
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            attempt_number = int(number_row["next_number"])
            conn.execute(
                """
                INSERT INTO review_task_attempts (
                    id, job_id, task_id, attempt_number, status,
                    started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    attempt_id,
                    row["job_id"],
                    task_id,
                    attempt_number,
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE review_tasks
                SET status = 'running', updated_at = ?, started_at = ?
                WHERE id = ?
                """,
                (now, now, task_id),
            )
            self._insert_message(
                conn,
                job_id=row["job_id"],
                task_id=task_id,
                sender_session_id="",
                message_type="task_attempt_started",
                payload={
                    "attempt_id": attempt_id,
                    "attempt_number": attempt_number,
                },
                now=now,
            )
        self._emit_update(self.get_review(row["job_id"]))
        return attempt_id

    def _record_child_session(
        self,
        task_id: str,
        child_session_id: str,
        *,
        attempt_id: str = "",
    ) -> None:
        now = _utc_now()
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE review_tasks
                SET child_session_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (child_session_id, now, task_id),
            )
            if attempt_id:
                conn.execute(
                    """
                    UPDATE review_task_attempts
                    SET child_session_id = ?, updated_at = ?
                    WHERE id = ? AND task_id = ?
                    """,
                    (child_session_id, now, attempt_id, task_id),
                )
            row = conn.execute(
                "SELECT job_id FROM review_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is not None:
            self._emit_update(self.get_review(row["job_id"]))

    def _finish_task(
        self,
        task_id: str,
        *,
        attempt_id: str = "",
        status: str,
        child_session_id: str = "",
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        now = _utc_now()
        payload = result or {}
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT job_id, child_session_id
                FROM review_tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return
            effective_child_session_id = (
                child_session_id or str(row["child_session_id"] or "")
            )
            if not attempt_id:
                attempt = conn.execute(
                    """
                    SELECT id
                    FROM review_task_attempts
                    WHERE task_id = ? AND status = 'running'
                    ORDER BY attempt_number DESC
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                attempt_id = str(attempt["id"]) if attempt is not None else ""
            conn.execute(
                """
                UPDATE review_tasks
                SET status = ?, child_session_id = ?, result_json = ?,
                    error = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    effective_child_session_id,
                    json.dumps(payload, ensure_ascii=False),
                    error,
                    now,
                    now,
                    task_id,
                ),
            )
            if attempt_id:
                conn.execute(
                    """
                    UPDATE review_task_attempts
                    SET status = ?, child_session_id = ?, result_json = ?,
                        error = ?, completed_at = ?, updated_at = ?
                    WHERE id = ? AND task_id = ?
                    """,
                    (
                        status,
                        effective_child_session_id,
                        json.dumps(payload, ensure_ascii=False),
                        error,
                        now,
                        now,
                        attempt_id,
                        task_id,
                    ),
                )
            self._insert_message(
                conn,
                job_id=row["job_id"],
                task_id=task_id,
                sender_session_id=child_session_id,
                message_type="task_completed",
                payload={
                    "status": status,
                    "attempt_id": attempt_id,
                    "child_session_id": effective_child_session_id,
                    "error": error,
                },
                now=now,
            )
        self._emit_update(self.get_review(row["job_id"]))

    def _emit_update(self, job: dict[str, Any]) -> None:
        if self._event_callback is None:
            return
        try:
            self._event_callback(
                job["session_id"],
                WsReviewUpdated(
                    job_id=job["id"],
                    status=job["status"],
                    task_states={
                        task["id"]: task["status"] for task in job["tasks"]
                    },
                    finding_count=int(
                        job.get("result", {}).get("finding_count", 0) or 0
                    ),
                    workspace_revision=job["workspace_revision"],
                    timestamp=_utc_now(),
                ),
            )
        except Exception:
            logger.debug("Review update callback failed", exc_info=True)

    @staticmethod
    def _insert_message(
        conn,
        *,
        job_id: str,
        task_id: str,
        sender_session_id: str,
        message_type: str,
        payload: dict[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO review_messages (
                id, job_id, task_id, sender_session_id, message_type,
                payload_json, delivery_state, created_at, acknowledged_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'acknowledged', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                job_id,
                task_id,
                sender_session_id,
                message_type,
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )

    @staticmethod
    def _render_job(row, tasks, attempts) -> dict[str, Any]:
        def parsed(raw: str) -> dict[str, Any]:
            try:
                value = json.loads(raw or "{}")
                return value if isinstance(value, dict) else {}
            except (TypeError, json.JSONDecodeError):
                return {}

        attempts_by_task: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            attempts_by_task.setdefault(attempt["task_id"], []).append(
                {
                    "id": attempt["id"],
                    "attempt_number": attempt["attempt_number"],
                    "status": attempt["status"],
                    "child_session_id": attempt["child_session_id"],
                    "result": parsed(attempt["result_json"]),
                    "error": attempt["error"],
                    "started_at": attempt["started_at"],
                    "completed_at": attempt["completed_at"],
                }
            )

        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "status": row["status"],
            "workspace_revision": row["workspace_revision"],
            "head_commit": row["head_commit"],
            "snapshot_path": row["snapshot_path"],
            "retry_of": row["retry_of"],
            "snapshot_available": bool(row["snapshot_path"]),
            "diff_hash": row["diff_hash"],
            "changed_files": json.loads(row["changed_files_json"] or "[]"),
            "focus": row["focus"],
            "result": parsed(row["result_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "tasks": [
                {
                    "id": task["id"],
                    "lens": task["lens"],
                    "title": task["title"],
                    "status": task["status"],
                    "child_session_id": task["child_session_id"],
                    "result": parsed(task["result_json"]),
                    "error": task["error"],
                    "started_at": task["started_at"],
                    "completed_at": task["completed_at"],
                    "attempts": attempts_by_task.get(task["id"], []),
                }
                for task in tasks
            ],
        }
