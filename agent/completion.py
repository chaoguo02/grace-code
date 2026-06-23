"""Completion validation based on EventLog facts."""

from __future__ import annotations

from dataclasses import dataclass

from agent.event_log import EventLog
from agent.policy import READ_TOOLS, WRITE_TOOLS, TaskPolicy, normalize_repo_path
from agent.task import EventType


@dataclass(frozen=True)
class CompletionVerdict:
    success: bool
    reason: str = ""


class CompletionValidator:
    """Validate task completion from logged tool calls, not assistant prose."""

    def validate(self, log: EventLog, policy: TaskPolicy, repo_path: str) -> CompletionVerdict:
        try:
            events = log.replay()
        except Exception as exc:
            return CompletionVerdict(False, f"Could not validate completion from event log: {exc}")

        read_paths: set[str] = set()
        write_paths: set[str] = set()
        saw_write = False

        observations_by_step: dict[int, dict[str, bool]] = {}
        for event in events:
            if event.event_type != EventType.OBSERVATION:
                continue
            observation = event.payload.get("observation", {})
            observations_by_step.setdefault(event.payload.get("step", 0), {})[observation.get("tool_name", "")] = observation.get("status") == "success"

        for event in events:
            if event.event_type != EventType.ACTION:
                continue
            action = event.payload.get("action", {})
            for tool_call in action.get("tool_calls") or []:
                name = tool_call.get("name", "")
                params = tool_call.get("params", {}) or {}
                path = normalize_repo_path(str(params.get("path", "")), repo_path)

                call_succeeded = observations_by_step.get(event.payload.get("step", 0), {}).get(name, True)

                if name in policy.completion.forbidden_tools and call_succeeded:
                    return CompletionVerdict(False, f"Forbidden tool '{name}' was used during execution.")

                if name in READ_TOOLS:
                    if call_succeeded and policy.execution.strict_file_scope and not self._path_allowed(path, policy.execution.allowed_read_paths):
                        return CompletionVerdict(False, f"Read outside allowed paths: {path}")
                    if call_succeeded:
                        read_paths.add(path)
                elif name in WRITE_TOOLS or name in {"file_edit", "edit_file", "edit"}:
                    if call_succeeded:
                        saw_write = True
                    if call_succeeded and policy.execution.strict_file_scope and not self._path_allowed(path, policy.execution.allowed_write_paths):
                        return CompletionVerdict(False, f"Write outside allowed paths: {path}")
                    if call_succeeded:
                        write_paths.add(path)

        missing_reads = sorted(path for path in policy.completion.required_reads if path not in read_paths)
        if missing_reads:
            return CompletionVerdict(False, f"Approved analysis plan finished without reading required source file: {', '.join(missing_reads)}")

        missing_writes = sorted(path for path in policy.completion.required_writes if path not in write_paths)
        if missing_writes:
            return CompletionVerdict(False, f"Approved edit plan finished without writing required file: {', '.join(missing_writes)}")

        if policy.completion.require_any_write and not saw_write:
            return CompletionVerdict(False, "Approved edit plan finished without performing any file write.")

        return CompletionVerdict(True)

    def _path_allowed(self, path: str, allowed_paths: frozenset[str] | None) -> bool:
        if allowed_paths is None:
            return True
        return path in allowed_paths
