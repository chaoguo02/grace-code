"""Persisted replay-contract projection for the Web Replay Lab."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Any
import uuid

from agent.task import TerminationReason
from observability.failure_policy import FAILURE_TAXONOMY
from observability.models import REPLAY_CONTRACT_VERSION
from observability.replay_validator import (
    gate_replay_completeness,
    validate_replay_run,
    validate_replay_step,
)


class ReplayService:
    """Build verifiable run records from append-only trace events."""

    def __init__(self, agent_service: Any) -> None:
        self._service = agent_service
        self._executions: dict[str, dict[str, Any]] = {}
        self._worktrees: dict[str, tuple[Any, Any]] = {}
        self._execution_lock = threading.RLock()
        self._ensure_execution_schema()

    def _connection_factory(self) -> Any:
        storage = getattr(self._service, "_storage", None)
        store = getattr(storage, "store", None)
        return getattr(store, "_connect", None)

    def _ensure_execution_schema(self) -> None:
        connect = self._connection_factory()
        if not callable(connect):
            return
        with connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS replay_executions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workspace_path TEXT NOT NULL DEFAULT '',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def start_execution(self, session_id: str, run_id: str) -> dict[str, Any]:
        replay = self.get_session_replay(session_id)
        run = next(
            (item for item in replay["runs"] if item["run_id"] == run_id),
            None,
        )
        if run is None:
            raise ValueError(f"Unknown replay run: {run_id}")
        if run["contract_source"] != "persisted_replay_run":
            raise ValueError("Only complete persisted replay contracts can execute")

        execution_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": execution_id,
            "session_id": session_id,
            "run_id": run_id,
            "status": "queued",
            "classification": "",
            "workspace_path": "",
            "pinned": False,
            "steps": [],
            "diff": "",
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
        self._save_record(record)
        threading.Thread(
            target=self._execute_contract,
            args=(execution_id, run),
            daemon=True,
            name=f"replay-{execution_id[:8]}",
        ).start()
        self._publish_execution(record, "replay_queued")
        return dict(record)

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        with self._execution_lock:
            record = self._executions.get(execution_id)
            if record is not None:
                return dict(record)
        loaded = self._load_execution(execution_id)
        if loaded is None:
            raise ValueError(f"Unknown replay execution: {execution_id}")
        with self._execution_lock:
            self._executions[execution_id] = loaded
        return dict(loaded)

    def pin_execution(self, execution_id: str) -> dict[str, Any]:
        record = self.get_execution(execution_id)
        record["pinned"] = True
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_record(record)
        return record

    def delete_workspace(self, execution_id: str) -> dict[str, Any]:
        record = self.get_execution(execution_id)
        managed = self._worktrees.pop(execution_id, None)
        if managed is not None:
            manager, worktree = managed
            manager.discard(worktree)
        elif record.get("workspace_path") and Path(record["workspace_path"]).exists():
            metadata = record.get("worktree") or {}
            required = {"name", "path", "branch", "base_branch", "base_commit"}
            if not required.issubset(metadata):
                raise ValueError(
                    "Legacy replay workspace lacks safe deletion metadata"
                )
            from agent.session.worktree_manager import Worktree, WorktreeManager
            manager = WorktreeManager(self._service.repo_path)
            worktree = Worktree(**{
                key: str(metadata[key]) for key in required
            })
            manager.discard(worktree)
        record["workspace_path"] = ""
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_record(record)
        return record

    def _execute_contract(self, execution_id: str, run: dict[str, Any]) -> None:
        record = self.get_execution(execution_id)
        record["status"] = "running"
        self._save_record(record)
        self._publish_execution(record, "replay_started")
        try:
            base_commit = str(
                run["record"].get("runtime_snapshot", {}).get("base_commit")
                or ""
            )
            if not base_commit:
                raise ValueError("Replay contract does not contain a base_commit")
            registry = getattr(self._service, "_registry", None)
            if registry is None:
                raise ValueError("Tool registry is unavailable for replay")

            from agent.session.worktree_manager import WorktreeManager
            from core.base import ExecutionContext

            manager = WorktreeManager(self._service.repo_path)
            worktree = manager.create(
                f"replay-{execution_id[:12]}",
                base_branch=base_commit,
            )
            self._worktrees[execution_id] = (manager, worktree)
            record["workspace_path"] = worktree.path
            record["worktree"] = {
                "name": worktree.name,
                "path": worktree.path,
                "branch": worktree.branch,
                "base_branch": worktree.base_branch,
                "base_commit": worktree.base_commit,
            }
            scoped = registry.scoped(ExecutionContext(
                workspace_root=worktree.path,
                repo_path=worktree.path,
            ))
            replay_steps: list[dict[str, Any]] = []
            all_attempts: list[dict[str, Any]] = []
            unexpected = False
            expected = False
            blocked = False

            for step in run["record"].get("steps", []):
                originals = {
                    str(item.get("tool_call_id") or ""): item
                    for item in step.get("tool_executions", [])
                }
                attempts = []
                for call in step.get("model_action", {}).get("tool_calls", []):
                    call_id = str(call.get("id") or "")
                    result = scoped.execute_tool(
                        str(call.get("name") or ""),
                        dict(call.get("params") or {}),
                        invocation_id=f"replay:{execution_id}:{call_id}",
                    )
                    original = originals.get(call_id, {})
                    classification = self._classify_tool_result(
                        original, result,
                    )
                    unexpected = (
                        unexpected
                        or classification == "unexpected_divergence"
                    )
                    expected = expected or classification == "expected_divergence"
                    blocked = blocked or classification == "blocked"
                    attempt = {
                        "tool_call_id": call_id,
                        "tool_name": str(call.get("name") or ""),
                        "success": result.success,
                        "outcome": result.normalized_outcome().value,
                        "output_fingerprint": self._fingerprint(result.output),
                        "error": result.format_error_for_observation() or "",
                        "attempt_count": result.attempt_count,
                        "classification": classification,
                        "step": step.get("step"),
                        "eventual_success": result.eventual_success,
                    }
                    attempts.append(attempt)
                    all_attempts.append(attempt)
                    self._publish_execution(
                        {
                            **record,
                            "step": step.get("step"),
                            "attempt": attempt,
                        },
                        "replay_attempt",
                    )
                replay_steps.append({
                    "step": step.get("step"),
                    "attempts": attempts,
                    "runtime_decision": step.get("runtime_decision", {}),
                })

            record["steps"] = replay_steps
            record["attempts"] = all_attempts
            record["diff"] = manager.get_diff(worktree)
            record["classification"] = (
                "blocked" if blocked
                else "unexpected_divergence" if unexpected
                else "expected_divergence" if expected
                else "matched"
            )
            record["status"] = "completed"
        except Exception as exc:
            record["status"] = "failed"
            record["classification"] = "blocked"
            record["error"] = str(exc)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_record(record)
        self._publish_execution(record, "replay_completed")
        self._enforce_retention()

    @staticmethod
    def _classify_tool_result(original: dict[str, Any], result: Any) -> str:
        if result.normalized_outcome().value == "blocked":
            return "blocked"
        if not original:
            return "expected_divergence"
        same_success = bool(original.get("success")) == bool(result.success)
        same_outcome = (
            not original.get("outcome")
            or str(original.get("outcome"))
            == result.normalized_outcome().value
        )
        return (
            "matched"
            if same_success and same_outcome
            else "unexpected_divergence"
        )

    @staticmethod
    def _fingerprint(value: str) -> str:
        return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]

    def _publish_execution(
        self, record: dict[str, Any], event_type: str,
    ) -> None:
        bus = getattr(self._service, "_event_bus", None)
        if bus is None:
            return
        bus.publish_raw(record["session_id"], {
            "type": event_type,
            "replay_execution": record,
        })

    def _save_record(self, record: dict[str, Any]) -> None:
        with self._execution_lock:
            self._executions[record["id"]] = dict(record)
            self._persist_execution(record)

    def _persist_execution(self, record: dict[str, Any]) -> None:
        connect = self._connection_factory()
        if not callable(connect):
            return
        with connect() as conn:
            conn.execute(
                """INSERT INTO replay_executions
                   (id, session_id, run_id, status, workspace_path, pinned,
                    result_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     status=excluded.status,
                     workspace_path=excluded.workspace_path,
                     pinned=excluded.pinned,
                     result_json=excluded.result_json,
                     updated_at=excluded.updated_at""",
                (
                    record["id"], record["session_id"], record["run_id"],
                    record["status"], record.get("workspace_path", ""),
                    int(bool(record.get("pinned"))),
                    json.dumps(record, ensure_ascii=True),
                    record["created_at"], record["updated_at"],
                ),
            )

    def _load_execution(self, execution_id: str) -> dict[str, Any] | None:
        connect = self._connection_factory()
        if not callable(connect):
            return None
        with connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM replay_executions WHERE id=?",
                (execution_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def _enforce_retention(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        candidates = [
            item for item in self._executions.values()
            if not item.get("pinned")
            and item.get("status") not in {"queued", "running"}
            and datetime.fromisoformat(item["updated_at"]) < cutoff
        ]
        for item in sorted(candidates, key=lambda row: row["updated_at"]):
            try:
                self.delete_workspace(item["id"])
            except ValueError:
                continue
        # Project-wide replay workspaces are capped at 2 GiB. Pinned and live
        # executions are never selected for automatic quota cleanup.
        quota = 2 * 1024 * 1024 * 1024
        eligible = [
            item for item in self._executions.values()
            if not item.get("pinned")
            and item.get("status") not in {"queued", "running"}
            and item.get("workspace_path")
        ]
        sizes: dict[str, int] = {}
        for item in eligible:
            path = Path(item["workspace_path"])
            try:
                sizes[item["id"]] = sum(
                    entry.stat().st_size for entry in path.rglob("*")
                    if entry.is_file()
                )
            except OSError:
                sizes[item["id"]] = 0
        total = sum(sizes.values())
        for item in sorted(eligible, key=lambda row: row["updated_at"]):
            if total <= quota:
                break
            try:
                self.delete_workspace(item["id"])
                total -= sizes.get(item["id"], 0)
            except ValueError:
                continue

    def get_session_replay(self, session_id: str) -> dict[str, Any]:
        session = self._service.session_service.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")

        events = self._service._storage.list_trace_events(
            session_id,
            limit=5000,
        )
        step_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        terminal_events: dict[str, dict[str, Any]] = {}
        started_events: dict[str, dict[str, Any]] = {}
        contracts: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

        for event in events:
            event_type = str(event.get("type") or "")
            run_id = str(event.get("run_id") or "")
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if event_type == "replay_step":
                if run_id:
                    step_events[run_id].append(dict(payload))
            elif event_type == "replay_run":
                record = payload.get("run")
                if not isinstance(record, dict):
                    record = payload
                record_run_id = str(record.get("run_id") or run_id)
                if record_run_id:
                    contracts[record_run_id] = (dict(record), event)
            elif event_type == "run_terminal" and run_id:
                terminal_events[run_id] = event
            elif event_type == "run_started" and run_id:
                started_events[run_id] = event

        run_ids = (
            set(step_events)
            | set(contracts)
            | set(terminal_events)
            | set(started_events)
        )
        runs = [
            self._build_run(
                run_id=run_id,
                session_id=session_id,
                steps=step_events.get(run_id, []),
                contract=contracts.get(run_id),
                terminal=terminal_events.get(run_id),
                started=started_events.get(run_id),
            )
            for run_id in run_ids
        ]
        runs.sort(
            key=lambda item: (
                int(item.get("turn_index", 0)),
                int(item.get("last_sequence", 0)),
            ),
            reverse=True,
        )

        return {
            "session_id": session_id,
            "agent_name": getattr(session, "agent_name", ""),
            "runs": runs,
            "summary": {
                "run_count": len(runs),
                "contract_count": sum(
                    1 for run in runs
                    if run["contract_source"] == "persisted_replay_run"
                ),
                "valid_count": sum(
                    1 for run in runs if run["validation"]["valid"]
                ),
                "boundary_preserved_count": sum(
                    1 for run in runs
                    if run["validation"]["boundary_preserved"]
                ),
                "step_count": sum(len(run["record"]["steps"]) for run in runs),
                "failed_tool_count": sum(
                    run["metrics"]["failed_tools"] for run in runs
                ),
            },
            "failure_taxonomy": self._failure_taxonomy(),
            "contract_version": REPLAY_CONTRACT_VERSION,
            "disclosure": {
                "source": "persisted_trace_events",
                "historical_runs_may_be_reconstructed": True,
                "reconstructed_provenance_is_not_inferred": True,
                "tool_outputs_are_runtime_truncated": True,
            },
        }

    def _build_run(
        self,
        *,
        run_id: str,
        session_id: str,
        steps: list[dict[str, Any]],
        contract: tuple[dict[str, Any], dict[str, Any]] | None,
        terminal: dict[str, Any] | None,
        started: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if contract is not None:
            record, contract_event = contract
            source = "persisted_replay_run"
            last_sequence = int(contract_event.get("sequence") or 0)
        else:
            record = self._reconstruct_record(
                run_id=run_id,
                session_id=session_id,
                steps=steps,
                terminal=terminal,
            )
            source = (
                "reconstructed_from_steps"
                if steps else "legacy_terminal_only"
            )
            last_sequence = max(
                int((terminal or {}).get("sequence") or 0),
                int((started or {}).get("sequence") or 0),
            )

        serialized_steps = record.get("steps")
        if not isinstance(serialized_steps, list):
            serialized_steps = list(serialized_steps or ())
            record["steps"] = serialized_steps

        validation = validate_replay_run(record)
        complete, completeness_message = gate_replay_completeness(record)
        step_models = [
            self._step_model(step, index + 1)
            for index, step in enumerate(serialized_steps)
            if isinstance(step, dict)
        ]
        event_step_count = len(steps)
        record_step_count = len(serialized_steps)
        evidence_complete = (
            source == "persisted_replay_run"
            and complete
            and event_step_count == record_step_count
        )
        return {
            "run_id": run_id,
            "turn_id": str(
                (terminal or started or {}).get("turn_id") or ""
            ),
            "turn_index": int(
                (terminal or started or {}).get("turn_index") or 0
            ),
            "status": str(
                record.get("termination_status")
                or (terminal or {}).get("status")
                or "running"
            ),
            "started_at": str((started or {}).get("timestamp") or ""),
            "completed_at": str((terminal or {}).get("timestamp") or ""),
            "contract_source": source,
            "evidence_complete": evidence_complete,
            "last_sequence": last_sequence,
            "record": {
                **record,
                "steps": step_models,
            },
            "validation": {
                "valid": validation.valid and complete,
                "schema_valid": validation.valid,
                "complete": complete,
                "completeness_message": completeness_message,
                "boundary_preserved": validation.boundary_preserved,
                "steps_validated": validation.steps_validated,
                "issues": [
                    {
                        "severity": issue.severity,
                        "field": issue.field,
                        "message": issue.message,
                    }
                    for issue in validation.issues
                ],
                "event_step_count": event_step_count,
                "record_step_count": record_step_count,
            },
            "metrics": self._metrics(serialized_steps),
        }

    @staticmethod
    def _reconstruct_record(
        *,
        run_id: str,
        session_id: str,
        steps: list[dict[str, Any]],
        terminal: dict[str, Any] | None,
    ) -> dict[str, Any]:
        terminal = terminal or {}
        reason = str(terminal.get("termination_reason") or "none")
        status = ReplayService._expected_run_status(
            reason,
            str(terminal.get("status") or ""),
        )
        return {
            "version": REPLAY_CONTRACT_VERSION,
            "run_id": run_id,
            "task_id": session_id,
            "session_id": session_id,
            "generation": 0,
            "task": {},
            "provenance": {},
            "permission_snapshot": {},
            "runtime_snapshot": {},
            "visible_tools": [],
            "steps": list(steps),
            "termination_reason": reason,
            "termination_status": status,
            "summary": str(terminal.get("summary") or ""),
        }

    @staticmethod
    def _expected_run_status(reason: str, fallback: str) -> str:
        try:
            policy = FAILURE_TAXONOMY[TerminationReason(reason)]
        except (ValueError, KeyError):
            return fallback
        return (
            policy.expected_status.value
            if policy.expected_status is not None else fallback
        )

    @staticmethod
    def _step_model(step: dict[str, Any], expected: int) -> dict[str, Any]:
        issues = validate_replay_step(step)
        visible_tools = step.get("visible_tools")
        executions = step.get("tool_executions")
        action = step.get("model_action")
        decision = step.get("runtime_decision")
        return {
            **step,
            "visible_tools": (
                visible_tools if isinstance(visible_tools, list)
                else list(visible_tools or ())
            ),
            "tool_executions": (
                executions if isinstance(executions, list)
                else list(executions or ())
            ),
            "model_action": action if isinstance(action, dict) else {},
            "runtime_decision": (
                decision if isinstance(decision, dict) else {}
            ),
            "validation": {
                "valid": not any(
                    issue.severity == "error" for issue in issues
                ),
                "expected_position": expected,
                "issues": [
                    {
                        "severity": issue.severity,
                        "field": issue.field,
                        "message": issue.message,
                    }
                    for issue in issues
                ],
            },
        }

    @staticmethod
    def _metrics(steps: list[dict[str, Any]]) -> dict[str, Any]:
        tools = [
            execution
            for step in steps
            for execution in (
                step.get("tool_executions", [])
                if isinstance(step, dict) else []
            )
            if isinstance(execution, dict)
        ]
        visible_sets = [
            {
                str(tool.get("name") or "")
                for tool in step.get("visible_tools", [])
                if isinstance(tool, dict) and tool.get("visible", True)
            }
            for step in steps
            if isinstance(step, dict)
        ]
        visibility_changes = sum(
            1
            for index in range(1, len(visible_sets))
            if visible_sets[index] != visible_sets[index - 1]
        )
        return {
            "tool_executions": len(tools),
            "failed_tools": sum(
                1 for execution in tools if not execution.get("success")
            ),
            "visible_tool_peak": max(
                (len(names) for names in visible_sets),
                default=0,
            ),
            "visibility_changes": visibility_changes,
            "strip_tools_decisions": sum(
                1
                for step in steps
                if isinstance(step, dict)
                and isinstance(step.get("runtime_decision"), dict)
                and step["runtime_decision"].get("strip_tools")
            ),
        }

    @staticmethod
    def _failure_taxonomy() -> list[dict[str, Any]]:
        return [
            {
                "reason": reason.value,
                "category": policy.category.value,
                "behavior": policy.behavior.value,
                "max_recovery_attempts": policy.max_recovery_attempts,
                "preserves_history": policy.preserves_history,
                "expected_status": (
                    policy.expected_status.value
                    if policy.expected_status is not None else ""
                ),
            }
            for reason, policy in FAILURE_TAXONOMY.items()
            if reason is not TerminationReason.NONE
        ]
