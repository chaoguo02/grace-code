"""Evidence retrieval tools for phased analysis.

These tools read from the RunEvidenceStore attached to the current
RunContext.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tools.artifact_tool import ArtifactStoreRef
from core.base import (
    BaseTool, ToolDependency, ToolEffect, ToolMetadata, ToolResult,
)


class EvidenceListTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_AGENT_STATE}),
    )

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(self) -> None:
        self._store = None

    def bind_store(self, store: Any) -> None:
        self._store = store

    def with_run_context(self, context: Any) -> "EvidenceListTool":
        from copy import copy
        bound = copy(self)
        bound._store = getattr(context, "evidence_store", None)
        return bound

    @property
    def name(self) -> str:
        return "evidence_list"

    @property
    def description(self) -> str:
        return "List captured evidence records and phase summaries from the current run."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "phase": {"type": "string", "description": "Optional phase filter such as inspect or verify."},
                "limit": {"type": "integer", "description": "Maximum evidence rows to return.", "default": 10},
            },
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        store = self._store
        if store is None:
            return ToolResult(success=True, output="No evidence store is attached to this run.")
        phase = str(params.get("phase", "")).strip()
        limit = max(1, int(params.get("limit", 10) or 10))
        entries = store.snapshot()
        if phase:
            entries = [
                e for e in entries
                if e.metadata.get("phase") == phase
            ]
        entries = entries[:limit]
        if not entries:
            return ToolResult(success=True, output="No evidence captured yet.")

        lines = ["Evidence records:"]
        for entry in entries:
            location = entry.path or "(no path)"
            lines.append(
                f"- {entry.evidence_id} | kind={entry.kind.value} | "
                f"tool={entry.tool_name} | {location} | "
                f"artifact={entry.artifact_id or '(none)'}"
            )
        return ToolResult(success=True, output="\n".join(lines))


class EvidenceGetTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_AGENT_STATE}),
    )

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(self) -> None:
        self._store = None

    def bind_store(self, store: Any) -> None:
        self._store = store

    def with_run_context(self, context: Any) -> "EvidenceGetTool":
        from copy import copy
        bound = copy(self)
        bound._store = getattr(context, "evidence_store", None)
        return bound

    @property
    def name(self) -> str:
        return "evidence_get"

    @property
    def description(self) -> str:
        return "Get one evidence record by id from the current run."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "description": "Evidence id such as ev_ab12cd34."},
            },
            "required": ["evidence_id"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        store = self._store
        if store is None:
            return ToolResult(success=False, output="", error="No evidence store is attached.")

        evidence_id = str(params.get("evidence_id", "")).strip()
        entry = store.get_by_id(evidence_id)
        if entry is None:
            return ToolResult(success=False, output="", error=f"Evidence not found: {evidence_id}")
        return ToolResult(success=True, output=(
            f"[Evidence {entry.evidence_id}]\n"
            f"kind={entry.kind.value} status={entry.status.value}\n"
            f"tool={entry.tool_name} path={entry.path}\n"
            f"summary: {entry.summary}"
        ))


class ArtifactSearchTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_AGENT_STATE}),
        dependency=ToolDependency.ARTIFACT_STORE,
    )

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(self, store_ref: ArtifactStoreRef) -> None:
        self._store_ref = store_ref

    @property
    def name(self) -> str:
        return "artifact_search"

    @property
    def description(self) -> str:
        return "Search raw evidence artifacts by id, tool name, summary, or content."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text to match against artifact summaries or content."},
                "limit": {"type": "integer", "description": "Maximum matches to return.", "default": 5},
            },
            "required": ["query"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        store = self._store_ref.store
        if store is None:
            return ToolResult(success=True, output="No artifact store is attached.")
        query = str(params.get("query", "")).strip()
        if not query:
            return ToolResult(success=False, output="", error="query is required")
        limit = max(1, int(params.get("limit", 5) or 5))
        matches = store.search(query, limit=limit)
        if not matches:
            return ToolResult(success=True, output=f"No artifacts matched query: {query}")
        lines = [f"Artifact matches for: {query}"]
        for artifact in matches:
            summary = artifact.summary.splitlines()[0] if artifact.summary else "(no summary)"
            lines.append(
                f"- {artifact.artifact_id} | {artifact.tool_name} | ~{artifact.token_count} tokens | {summary}"
            )
        return ToolResult(success=True, output="\n".join(lines))
