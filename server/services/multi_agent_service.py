"""Persisted multi-agent scheduling and consistency projection."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from enum import Enum
import os
from typing import Any


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class MultiAgentService:
    """Build one read-only control-plane view from durable session facts."""

    TERMINAL = {"completed", "partial", "failed", "cancelled"}

    def __init__(self, agent_service: Any) -> None:
        self._service = agent_service
        self._store = agent_service._store
        from agent.session.integration_coordinator import (
            DelegationIntegrationCoordinator,
        )
        self._integration = (
            DelegationIntegrationCoordinator(
                agent_service._runtime,
                self._store,
                event_callback=self._emit_delegation_event,
            )
            if getattr(agent_service, "_runtime", None) is not None
            else None
        )

    def _emit_delegation_event(
        self, event_type: str, run_id: str, payload: dict[str, object],
    ) -> None:
        event_bus = getattr(self._service, "_event_bus", None)
        run = self._store.get_delegation_run(run_id)
        if event_bus is None or run is None:
            return
        from agent.task import Event, EventType

        event_bus.publish(Event(
            event_type=EventType(event_type),
            task_id=run_id,
            session_id=str(run["parent_session_id"]),
            payload={
                "delegation_run_id": run_id,
                "phase": str(run.get("phase", "")),
                **payload,
            },
        ))

    def get_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        run = self._store.get_delegation_run(run_id)
        if run is None or str(run["parent_session_id"]) != session_id:
            raise ValueError("Delegation run not found in session")
        return {
            "run": run,
            "tasks": self._store.list_delegation_tasks(run_id),
        }

    def integrate_run(
        self, session_id: str, run_id: str, decisions: list[dict[str, Any]],
    ) -> dict[str, object]:
        from agent.session.integration_coordinator import IntegrationDecision

        if self._integration is None:
            raise RuntimeError("Multi-agent runtime is unavailable")
        return self._integration.integrate(
            parent_session_id=session_id,
            run_id=run_id,
            decisions=[
                IntegrationDecision(
                    task_id=str(item.get("task_id", "")),
                    action=str(item.get("action", "")),
                    expected_revision=str(item.get("expected_revision", "")),
                )
                for item in decisions
            ],
        )

    def verify_run(self, session_id: str, run_id: str) -> dict[str, object]:
        if self._integration is None:
            raise RuntimeError("Multi-agent runtime is unavailable")
        return self._integration.verify(
            parent_session_id=session_id, run_id=run_id,
        )

    def retry_task(self, session_id: str, task_id: str) -> dict[str, object]:
        task = self._store.get_delegation_task(task_id)
        if task is None:
            raise ValueError("Unknown delegation task")
        run_id = str(task["delegation_run_id"])
        self.get_run(session_id, run_id)
        replacements = self._store.prepare_delegation_retry(task_id)
        for replacement in replacements:
            self._emit_delegation_event("delegation_task_retrying", run_id, {
                "task_id": str(replacement["id"]),
                "generation": int(replacement.get("retry_count", 0)),
                "status": "queued",
                "reason": f"Supersedes {replacement.get('supersedes_task_id')}",
                "dependencies": list(replacement.get("dependencies", [])),
            })
        self._emit_delegation_event("delegation_phase_changed", run_id, {
            "phase": "executing", "status": "running",
            "reason": "retry",
        })
        scheduler = self._scheduler()
        run = scheduler.execute(parent_session_id=session_id, run_id=run_id)
        return {
            "run": run,
            "replacement_tasks": replacements,
            "tasks": self._store.list_delegation_tasks(run_id),
        }

    def resume_run(self, session_id: str, run_id: str) -> dict[str, object]:
        self.get_run(session_id, run_id)
        replacements = self._store.prepare_delegation_resume(run_id)
        for replacement in replacements:
            self._emit_delegation_event("delegation_task_retrying", run_id, {
                "task_id": str(replacement["id"]),
                "generation": int(replacement.get("retry_count", 0)),
                "status": "queued",
                "reason": f"Resumes {replacement.get('supersedes_task_id')}",
                "dependencies": list(replacement.get("dependencies", [])),
            })
        self._emit_delegation_event("delegation_phase_changed", run_id, {
            "phase": "executing", "status": "running",
            "reason": "resume",
        })
        run = self._scheduler().execute(
            parent_session_id=session_id, run_id=run_id,
        )
        return {
            "run": run,
            "replacement_tasks": replacements,
            "tasks": self._store.list_delegation_tasks(run_id),
        }

    def cancel_run(
        self, session_id: str, run_id: str, detail: str,
    ) -> dict[str, object]:
        current = self.get_run(session_id, run_id)["run"]
        if str(current["status"]) != "running":
            raise ValueError(f"Delegation already converged as {current['status']}")
        cancelled: list[str] = []
        for task in self._store.list_delegation_tasks(run_id):
            if str(task["status"]) != "superseded" and str(task["status"]) not in {
                "completed", "partial", "failed", "cancelled", "interrupted",
                "no_findings", "budget_exhausted", "rejected",
            }:
                child_id = str(task.get("child_session_id") or "")
                if child_id:
                    try:
                        self._service._runtime.cancel_agent(
                            parent_session_id=session_id,
                            child_session_id=child_id,
                            detail=detail,
                        )
                    except (TypeError, ValueError):
                        pass
                if self._store.update_delegation_task(
                    str(task["id"]),
                    status="cancelled",
                    error=detail,
                    expected_statuses=("queued", "running"),
                ):
                    cancelled.append(str(task["id"]))
        terminal_event = self._store.finalize_delegation_run(
            run_id,
            status="cancelled",
            phase="cancelled",
            expected_version=int(current["version"]),
            report_count=len([
                task for task in self._store.list_delegation_tasks(run_id)
                if str(task["status"]) != "superseded"
            ]),
        )
        if terminal_event is None:
            actual = self._store.get_delegation_run(run_id) or current
            raise ValueError(f"Delegation already converged as {actual['status']}")
        self._emit_delegation_event(
            "delegation_completed", run_id,
            {"_persisted_event": terminal_event},
        )
        return {
            "delegation_run_id": run_id,
            "status": "cancelled",
            "cancelled_task_ids": cancelled,
        }

    def _scheduler(self):
        runtime = getattr(self._service, "_runtime", None)
        if runtime is None:
            raise RuntimeError("Multi-agent runtime is unavailable")
        from agent.session.delegation_scheduler import DelegationRunScheduler

        return DelegationRunScheduler(
            runtime,
            self._store,
            event_callback=self._emit_delegation_event,
        )

    def get_snapshot(self, session_id: str) -> dict[str, Any]:
        selected = self._store.get_session(session_id)
        if selected is None:
            raise ValueError(f"Unknown session: {session_id}")
        root_id = selected.root_id or selected.id
        root = self._store.get_session(root_id)
        if root is None:
            root = selected
            root_id = selected.id

        records = self._collect(root)
        by_id = {record.id: record for record in records}
        nodes = [self._node(record, selected.id) for record in records]
        notifications = [
            item
            for record in records
            for item in self._list_notifications(record.id)
        ]
        communications = self._communications(records, notifications)
        worktrees = self._worktrees(records)
        checks = self._checks(records, by_id, notifications)
        raw_delegation_runs = [
            run
            for record in records
            for run in self._list_delegation_runs(record.id)
        ]
        raw_delegation_tasks = [
            task
            for run in raw_delegation_runs
            for task in self._list_delegation_tasks(str(run["id"]))
        ]
        delegation_tasks = [
            self._delegation_task_projection(task)
            for task in raw_delegation_tasks
        ]
        tasks_by_run: dict[str, list[dict[str, Any]]] = {}
        for task in delegation_tasks:
            tasks_by_run.setdefault(str(task["delegation_run_id"]), []).append(
                task
            )
        delegation_runs = []
        for run in raw_delegation_runs:
            run_tasks = tasks_by_run.get(str(run["id"]), [])
            required = [task for task in run_tasks if bool(task["required"])]
            delegation_runs.append({
                **run,
                "required_count": len(required),
                "completed_count": sum(
                    str(task["status"]) in {"completed", "no_findings"}
                    for task in required
                ),
                "failed_count": sum(
                    str(task["status"]) in {
                        "partial", "failed", "cancelled", "interrupted",
                        "budget_exhausted", "rejected",
                    }
                    for task in required
                ),
                "retry_count": sum(
                    int(task.get("retry_count", 0)) for task in run_tasks
                ),
            })
        latest_routing = (
            {
                "topology": delegation_runs[-1]["topology"],
                "reason_code": delegation_runs[-1]["reason_code"],
                "explanation": delegation_runs[-1]["explanation"],
                "downgraded_from": delegation_runs[-1]["downgraded_from"],
            }
            if delegation_runs else None
        )
        status_counts = Counter(str(_value(record.status)) for record in records)
        placement_counts = Counter(
            str(_value(record.execution_placement)) for record in records
        )
        active = sum(
            count for status, count in status_counts.items()
            if status not in self.TERMINAL
        )
        unresolved = sum(
            item["consistency_state"] == "needs_resolution"
            for item in worktrees
        )
        from agent.session.multi_agent_config import MultiAgentFeatureConfig

        feature = MultiAgentFeatureConfig.from_environment()
        run_status_counts = Counter(
            str(run.get("status", "unknown")) for run in delegation_runs
        )
        phase_counts = Counter(
            str(run.get("phase", "unknown")) for run in delegation_runs
        )
        task_status_counts = Counter(
            str(task.get("status", "unknown")) for task in delegation_tasks
        )
        integration_counts = Counter(
            str(task.get("integration_status", "unknown"))
            for task in delegation_tasks
        )
        verification_counts = Counter(
            str((run.get("verification") or {}).get("status", "not_run"))
            if isinstance(run.get("verification"), dict) else "not_run"
            for run in delegation_runs
        )
        observability = {
            "run_count": len(delegation_runs),
            "task_count": len(delegation_tasks),
            "run_status_counts": dict(run_status_counts),
            "phase_counts": dict(phase_counts),
            "task_status_counts": dict(task_status_counts),
            "integration_status_counts": dict(integration_counts),
            "verification_status_counts": dict(verification_counts),
            "retry_count": sum(
                int(task.get("retry_count", 0)) for task in delegation_tasks
            ),
            "tokens_used": sum(
                int(task.get("tokens_used", 0)) for task in delegation_tasks
            ),
            "worker_duration_ms": sum(
                int(task.get("elapsed_ms", 0)) for task in delegation_tasks
            ),
        }

        return {
            "selected_session_id": selected.id,
            "root_session_id": root_id,
            "routing": latest_routing,
            "delegation_runs": delegation_runs,
            "delegation_tasks": delegation_tasks,
            "limits": self._limits(),
            "feature": {
                "enabled": feature.enabled,
                "environment_variable": "GRACE_MULTI_AGENT_MODE_ENABLED",
                "max_tasks": feature.max_tasks,
                "max_wave_fanout": feature.max_wave_fanout,
                "max_concurrent": feature.max_concurrent,
            },
            "observability": observability,
            "team": self._team_projection(root_id),
            "nodes": nodes,
            "edges": [
                {
                    "source": record.parent_id,
                    "target": record.id,
                    "kind": "delegation",
                    "context_origin": str(_value(record.context_origin)),
                    "execution_placement": str(_value(record.execution_placement)),
                    "workspace_mode": str(_value(record.workspace_mode)),
                }
                for record in records if record.parent_id in by_id
            ],
            "scheduler": {
                "total_agents": len(records),
                "active_agents": active,
                "terminal_agents": len(records) - active,
                "peak_observed_parallelism": self._peak_parallelism(records),
                "status_counts": dict(status_counts),
                "placement_counts": dict(placement_counts),
                "max_depth": max(
                    (int(record.agent_depth.value) for record in records),
                    default=0,
                ),
            },
            "communications": communications,
            "communication_summary": {
                "delegations": sum(
                    item["kind"] == "delegation" for item in communications
                ),
                "completion_notifications": len(notifications),
                "pending_delivery": sum(
                    item["delivery_state"] == "pending"
                    for item in notifications
                ),
                "delivered": sum(
                    item["delivery_state"] == "delivered"
                    for item in notifications
                ),
            },
            "contexts": [self._context(record) for record in records],
            "worktrees": worktrees,
            "consistency": {
                "state": (
                    "blocked" if unresolved
                    else "healthy" if all(check["passed"] for check in checks)
                    else "warning"
                ),
                "unresolved_worktrees": unresolved,
                "checks": checks,
            },
            "invariants": [
                {
                    "name": "Parent owns scheduling",
                    "detail": "Foreground blocks the caller; background returns a durable handle.",
                },
                {
                    "name": "Context origin is explicit",
                    "detail": "Fresh, parent_snapshot, and resumed are persisted rather than inferred from message text.",
                },
                {
                    "name": "Completion delivery is at-most-once per generation",
                    "detail": "A unique child/generation notification is atomically claimed by its direct parent.",
                },
                {
                    "name": "Worktree convergence is explicit",
                    "detail": "Preserved changes block silent convergence until apply, discard, or retain is recorded.",
                },
            ],
            "disclosure": {
                "source": (
                    "persisted_sessions_notifications_and_delegation_tasks"
                ),
                "arbitrary_agent_message_bus": bool(
                    self._team_projection(root_id).get("active")
                ),
                "scheduler_simulation_performed": False,
                "parallelism_is_interval_projection": True,
            },
        }

    def _collect(self, root: Any) -> list[Any]:
        records: list[Any] = []
        seen: set[str] = set()

        def visit(record: Any) -> None:
            if record.id in seen:
                return
            seen.add(record.id)
            records.append(record)
            for child in self._store.list_child_sessions(record.id):
                visit(child)

        visit(root)
        return records

    def _list_notifications(self, parent_id: str) -> list[dict[str, Any]]:
        reader = getattr(self._store, "list_agent_notifications", None)
        return list(reader(parent_id)) if callable(reader) else []

    def _list_delegation_runs(self, parent_id: str) -> list[dict[str, Any]]:
        reader = getattr(self._store, "list_delegation_runs", None)
        return list(reader(parent_id)) if callable(reader) else []

    def _list_delegation_tasks(self, run_id: str) -> list[dict[str, Any]]:
        reader = getattr(self._store, "list_delegation_tasks", None)
        return list(reader(run_id)) if callable(reader) else []

    @staticmethod
    def _delegation_task_projection(
        task: dict[str, Any],
    ) -> dict[str, Any]:
        report = (
            task.get("report")
            if isinstance(task.get("report"), dict) else {}
        )
        return {
            **task,
            "run_id": str(task.get("delegation_run_id", "")),
            "title": str(task.get("goal", "")) or str(task.get("id", "")),
            "description": str(task.get("prompt", "")),
            "agent_name": str(task.get("agent_type", "unassigned")),
            "tokens_used": int(report.get("tokens_used", 0) or 0),
            "elapsed_ms": int(report.get("duration_ms", 0) or 0),
            "evidence_status": (
                "available"
                if report.get("findings")
                or report.get("changed_files")
                or report.get("verification")
                else "none"
            ),
            "failure_detail": str(task.get("error", "")) or None,
        }

    @staticmethod
    def _int_env(name: str, default: int, *, maximum: int = 10_000) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            value = default
        return max(1, min(value, maximum))

    def _limits(self) -> dict[str, int]:
        from agent.session.multi_agent_config import MultiAgentFeatureConfig

        multi_agent = MultiAgentFeatureConfig.from_environment()
        return {
            "max_multi_agent_tasks": multi_agent.max_tasks,
            "max_wave_fanout": multi_agent.max_wave_fanout,
            "max_spawn_per_session": self._int_env(
                "GRACE_MAX_SUBAGENTS_PER_SESSION", 64,
            ),
            "max_concurrent_subagents": self._int_env(
                "GRACE_MAX_CONCURRENT_SUBAGENTS", 4, maximum=64,
            ),
            "max_subagent_spawn_depth": self._int_env(
                "GRACE_MAX_SUBAGENT_SPAWN_DEPTH", 1, maximum=5,
            ),
            "max_fanout_per_turn": self._int_env(
                "GRACE_MAX_FANOUT_PER_TURN", 3, maximum=16,
            ),
        }

    def _team_projection(self, root_id: str) -> dict[str, Any]:
        from agent.team.feature_flags import TeamFeatureConfig

        config = TeamFeatureConfig.from_environment()
        runtime = getattr(self._service, "_runtime", None)
        teams = getattr(runtime, "_teams", {})
        proposals = getattr(runtime, "_team_proposals", {})
        team = teams.get(root_id) if isinstance(teams, dict) else None
        if team is None:
            persisted = [
                run for run in self._list_delegation_runs(root_id)
                if bool(run.get("is_team"))
            ]
            if persisted:
                latest = persisted[-1]
                board = self._list_delegation_tasks(str(latest["id"]))
                return {
                    "id": latest["id"],
                    "available": config.enabled,
                    "enabled": config.enabled,
                    "active": False,
                    "state": "recovery_required",
                    "approval_required": False,
                    "direct_messaging": False,
                    "shared_task_board": True,
                    "task_board": board,
                    "mailbox": {"pending": 0, "persisted": False},
                    "members": [],
                    "recovery_note": (
                        "The durable task board was restored, but live teammate "
                        "mailboxes require an explicit new activation."
                    ),
                }
            return {
                "available": config.enabled,
                "enabled": config.enabled,
                "active": False,
                "approval_required": False,
                "proposal_requires_approval": config.require_user_approval,
                "direct_messaging": False,
                "shared_task_board": False,
                "task_board": [],
                "mailbox": {"pending": 0},
                "members": [],
            }
        members = [
            {
                "id": member.id,
                "role": member.role,
                "state": _value(member.state),
            }
            for member in team.members
        ]
        board = [
            {
                "id": task.id,
                "goal": task.goal,
                "dependencies": list(task.dependencies),
                "status": _value(task.state),
                "assignee_id": task.assignee_id,
                "result_summary": task.result_summary,
            }
            for task in team.task_board.list()
        ]
        state = str(_value(team.state))
        pending = sum(
            team.mailbox.pending_count(member.id) for member in team.members
        )
        proposal = (
            proposals.get(root_id)
            if isinstance(proposals, dict) else None
        )
        if proposal and state == "awaiting_approval":
            members.extend({
                "id": str(item.get("id", "")),
                "role": str(item.get("role", "")),
                "state": "proposed",
            } for item in proposal.get("members", []))
            board = [{
                "id": str(item.get("id", "")),
                "goal": str(item.get("goal", "")),
                "dependencies": [
                    str(value)
                    for value in item.get("dependencies", [])
                ],
                "status": "proposed",
                "assignee_id": "",
                "result_summary": "",
            } for item in proposal.get("tasks", [])]
        return {
            "id": team.team_id,
            "available": True,
            "enabled": True,
            "active": state == "active",
            "state": state,
            "approval_required": state == "awaiting_approval",
            "direct_messaging": state == "active",
            "arbitrary_agent_message_bus": state == "active",
            "shared_task_board": True,
            "task_board": board,
            "mailbox": {"pending": pending},
            "members": members,
        }

    def _node(self, record: Any, selected_id: str) -> dict[str, Any]:
        result = record.agent_result
        return {
            "id": record.id,
            "parent_id": record.parent_id,
            "agent_name": record.agent_name,
            "title": record.title,
            "status": str(_value(record.status)),
            "agent_kind": str(_value(record.agent_kind)),
            "context_origin": str(_value(record.context_origin)),
            "execution_placement": str(_value(record.execution_placement)),
            "workspace_mode": str(_value(record.workspace_mode)),
            "depth": int(record.agent_depth.value),
            "generation": int(record.generation),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
            "selected": record.id == selected_id,
            "result_status": (
                str(_value(result.status)) if result is not None else None
            ),
        }

    def _context(self, record: Any) -> dict[str, Any]:
        try:
            messages = self._store.list_messages(record.id)
        except Exception:
            messages = []
        return {
            "session_id": record.id,
            "agent_name": record.agent_name,
            "origin": str(_value(record.context_origin)),
            "generation": int(record.generation),
            "message_count": len(messages),
            "token_estimate": sum(
                max(1, len(str(message.content or "")) // 3)
                for message in messages
            ),
            "isolation_boundary": (
                "snapshot_copy"
                if str(_value(record.context_origin)) == "parent_snapshot"
                else "own_history"
            ),
            "tool_contract_persisted": bool(
                (record.metadata or {}).get("parent_tool_schemas")
            ),
        }

    def _communications(
        self, records: list[Any], notifications: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items = [
            {
                "id": f"delegation:{record.id}:{record.generation}",
                "kind": "delegation",
                "source_session_id": record.parent_id,
                "target_session_id": record.id,
                "generation": int(record.generation),
                "created_at": record.created_at,
                "delivery_state": "created",
                "status": str(_value(record.status)),
                "summary": record.title or record.agent_name,
                "source": "session_record",
            }
            for record in records if record.parent_id
        ]
        for notification in notifications:
            payload = notification.get("payload", {})
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            items.append({
                "id": f"completion:{notification['id']}",
                "kind": "completion",
                "source_session_id": notification["child_session_id"],
                "target_session_id": notification["parent_session_id"],
                "generation": notification["generation"],
                "created_at": notification["created_at"],
                "delivered_at": notification["delivered_at"],
                "delivery_state": notification["delivery_state"],
                "status": str(result.get("status", "unknown")),
                "summary": str(result.get("summary", ""))[:240],
                "source": "agent_notifications",
            })
        return sorted(items, key=lambda item: (item["created_at"], item["id"]))

    def _worktrees(self, records: list[Any]) -> list[dict[str, Any]]:
        items = []
        for record in records:
            result = record.agent_result
            if result is None:
                continue
            disposition = str(_value(result.worktree_disposition))
            evidence = result.worktree
            if disposition == "not_applicable" and evidence is None:
                continue
            state = {
                "preserved": "needs_resolution",
                "applied": "converged",
                "discarded": "converged",
                "cleaned": "converged",
                "retained": "intentionally_separate",
            }.get(disposition, "observed")
            items.append({
                "session_id": record.id,
                "parent_session_id": record.parent_id,
                "agent_name": record.agent_name,
                "disposition": disposition,
                "consistency_state": state,
                "change": (
                    str(_value(evidence.change)) if evidence is not None else "none"
                ),
                "changed_files": (
                    list(evidence.changed_files) if evidence is not None else []
                ),
                "branch": evidence.branch if evidence is not None else "",
                "base_branch": evidence.base_branch if evidence is not None else "",
                "revision": evidence.revision if evidence is not None else "",
            })
        return items

    def _checks(
        self,
        records: list[Any],
        by_id: dict[str, Any],
        notifications: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        root_ids = {record.root_id for record in records}
        parent_links = all(
            record.parent_id is None or record.parent_id in by_id
            for record in records
        )
        depth_links = all(
            record.parent_id is None
            or int(record.agent_depth.value)
            == int(by_id[record.parent_id].agent_depth.value) + 1
            for record in records if record.parent_id in by_id
        )
        direct_delivery = all(
            item["child_session_id"] in by_id
            and by_id[item["child_session_id"]].parent_id
            == item["parent_session_id"]
            for item in notifications
        )
        evidence_consistent = all(
            record.agent_result is None
            or (
                (str(_value(record.agent_result.worktree_disposition))
                 in {"preserved", "retained"})
                == (record.agent_result.worktree is not None)
            )
            for record in records
        )
        return [
            {
                "id": "root_identity",
                "label": "One root identity",
                "passed": len(root_ids) == 1,
                "detail": f"{len(root_ids)} root id(s) observed",
            },
            {
                "id": "parent_links",
                "label": "Parent links resolve",
                "passed": parent_links,
                "detail": "Every non-root node points to a node in this topology",
            },
            {
                "id": "depth_monotonic",
                "label": "Depth increments by one",
                "passed": depth_links,
                "detail": "Persisted depth agrees with the delegation edge",
            },
            {
                "id": "direct_delivery",
                "label": "Completion returns to direct parent",
                "passed": direct_delivery,
                "detail": f"{len(notifications)} durable completion notification(s)",
            },
            {
                "id": "worktree_evidence",
                "label": "Worktree evidence matches disposition",
                "passed": evidence_consistent,
                "detail": "Evidence exists only for preserved or retained worktrees",
            },
        ]

    @staticmethod
    def _peak_parallelism(records: list[Any]) -> int:
        points: list[tuple[float, int]] = []
        now = datetime.now(timezone.utc).timestamp()
        for record in records:
            start = _timestamp(record.created_at)
            if start is None:
                continue
            end = _timestamp(record.completed_at)
            if end is None and str(_value(record.status)) in MultiAgentService.TERMINAL:
                end = _timestamp(record.updated_at)
            points.append((start, 1))
            points.append((end if end is not None else now, -1))
        active = peak = 0
        for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
            active += delta
            peak = max(peak, active)
        return peak
