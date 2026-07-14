"""Explicit parent controls for preserved subagent worktrees."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from agent.v2.worktree_service import (
    WorktreeOperationResult,
    WorktreeOperationStatus,
)
from tools.base import (
    BaseTool,
    PathAccess,
    RiskLevel,
    ToolConcurrency,
    ToolEffect,
    ToolErrorType,
    ToolMetadata,
    ToolResult,
)

if TYPE_CHECKING:
    from agent.v2.models import WorktreeEvidence
    from agent.v2.runtime import SessionRuntime


def _child_session_id(params: dict[str, Any]) -> str | None:
    value = params.get("child_session_id")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _expected_revision(params: dict[str, Any]) -> str | None:
    value = params.get("expected_revision")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _format_evidence(evidence: "WorktreeEvidence") -> str:
    lines = [
        f"<subagent-worktree change='{evidence.change.value}'>",
        f"  <path>{escape(evidence.path)}</path>",
        f"  <branch>{escape(evidence.branch)}</branch>",
        f"  <base-branch>{escape(evidence.base_branch)}</base-branch>",
        f"  <base-commit>{escape(evidence.base_commit)}</base-commit>",
        f"  <revision>{escape(evidence.revision)}</revision>",
    ]
    for changed_file in evidence.changed_files:
        lines.append(f"  <changed-file>{escape(changed_file)}</changed-file>")
    if evidence.error:
        lines.append(f"  <inspection-error>{escape(evidence.error)}</inspection-error>")
    lines.append("</subagent-worktree>")
    return "\n".join(lines)


def _format_operation(result: WorktreeOperationResult) -> str:
    return (
        f"<subagent-worktree-operation status='{result.status.value}'>\n"
        f"{_format_evidence(result.evidence)}\n"
        "</subagent-worktree-operation>"
    )


def _operation_error(result: WorktreeOperationResult) -> ToolResult:
    error_type = {
        WorktreeOperationStatus.STALE: ToolErrorType.UNAVAILABLE,
        WorktreeOperationStatus.PARENT_DIRTY: ToolErrorType.UNAVAILABLE,
        WorktreeOperationStatus.CONFLICT: ToolErrorType.PROCESS_FAILED,
        WorktreeOperationStatus.FAILED: ToolErrorType.PROCESS_FAILED,
    }.get(result.status, ToolErrorType.INTERNAL)
    return ToolResult.from_error(
        error_type=error_type,
        detail=(
            f"{result.error or result.status.value}\n"
            f"Current facts:\n{_format_evidence(result.evidence)}"
        ),
    )


class SubagentWorktreeInspectTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_AGENT_STATE, ToolEffect.READ_VCS}),
    )

    def __init__(self, runtime: "SessionRuntime", parent_session_id: str) -> None:
        self._runtime = runtime
        self._parent_session_id = parent_session_id

    @property
    def name(self) -> str:
        return "subagent_worktree_inspect"

    @property
    def description(self) -> str:
        return (
            "Inspect fresh Git facts for a preserved direct-child worktree. "
            "Review its revision and changed files before applying or discarding it."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "child_session_id": {
                    "type": "string",
                    "description": "Direct child session returned by the task tool.",
                },
            },
            "required": ["child_session_id"],
        }

    def concurrency_mode(self, params: dict[str, Any]) -> ToolConcurrency:
        return ToolConcurrency.PARALLEL_SAFE

    def execute(self, params: dict[str, Any]) -> ToolResult:
        child_session_id = _child_session_id(params)
        if child_session_id is None:
            return ToolResult.from_error(
                ToolErrorType.INVALID_PARAMS,
                "child_session_id is required",
            )
        try:
            evidence = self._runtime.inspect_subagent_worktree(
                self._parent_session_id, child_session_id,
            )
        except ValueError as exc:
            return ToolResult.from_error(ToolErrorType.NOT_FOUND, str(exc))
        return ToolResult(success=True, output=_format_evidence(evidence))


class SubagentWorktreeApplyTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.WRITE_WORKSPACE, ToolEffect.WRITE_VCS}),
        path_access=PathAccess.WORKSPACE_WIDE,
    )

    def __init__(self, runtime: "SessionRuntime", parent_session_id: str) -> None:
        self._runtime = runtime
        self._parent_session_id = parent_session_id

    @property
    def name(self) -> str:
        return "subagent_worktree_apply"

    @property
    def risk_level(self) -> str:
        return RiskLevel.HIGH

    @property
    def description(self) -> str:
        return (
            "Apply an inspected direct-child worktree to the parent's current Git "
            "branch. Requires the exact reviewed revision and a clean parent worktree."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return _write_parameters_schema()

    def execute(self, params: dict[str, Any]) -> ToolResult:
        values = _validated_write_params(params)
        if values is None:
            return ToolResult.from_error(
                ToolErrorType.INVALID_PARAMS,
                "child_session_id and expected_revision are required",
            )
        try:
            result = self._runtime.apply_subagent_worktree(
                self._parent_session_id,
                values[0],
                expected_revision=values[1],
            )
        except ValueError as exc:
            return ToolResult.from_error(ToolErrorType.NOT_FOUND, str(exc))
        if not result.is_success:
            return _operation_error(result)
        return ToolResult(success=True, output=_format_operation(result))


class SubagentWorktreeDiscardTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.WRITE_AGENT_STATE, ToolEffect.WRITE_VCS}),
    )

    def __init__(self, runtime: "SessionRuntime", parent_session_id: str) -> None:
        self._runtime = runtime
        self._parent_session_id = parent_session_id

    @property
    def name(self) -> str:
        return "subagent_worktree_discard"

    @property
    def risk_level(self) -> str:
        return RiskLevel.HIGH

    @property
    def description(self) -> str:
        return (
            "Permanently discard an inspected direct-child worktree. Requires "
            "the exact reviewed revision so newer changes cannot be deleted."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return _write_parameters_schema()

    def execute(self, params: dict[str, Any]) -> ToolResult:
        values = _validated_write_params(params)
        if values is None:
            return ToolResult.from_error(
                ToolErrorType.INVALID_PARAMS,
                "child_session_id and expected_revision are required",
            )
        try:
            result = self._runtime.discard_subagent_worktree(
                self._parent_session_id,
                values[0],
                expected_revision=values[1],
            )
        except ValueError as exc:
            return ToolResult.from_error(ToolErrorType.NOT_FOUND, str(exc))
        if not result.is_success:
            return _operation_error(result)
        return ToolResult(success=True, output=_format_operation(result))


def _write_parameters_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "child_session_id": {
                "type": "string",
                "description": "Direct child session returned by the task tool.",
            },
            "expected_revision": {
                "type": "string",
                "description": "Exact revision returned by worktree inspection.",
            },
        },
        "required": ["child_session_id", "expected_revision"],
    }


def _validated_write_params(params: dict[str, Any]) -> tuple[str, str] | None:
    child_session_id = _child_session_id(params)
    expected_revision = _expected_revision(params)
    if child_session_id is None or expected_revision is None:
        return None
    return child_session_id, expected_revision
