"""Persisted replay-contract projection for the Web Replay Lab."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

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
