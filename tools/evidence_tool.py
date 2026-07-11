"""Evidence retrieval tools for phased analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from context.evidence import EvidenceLedger
from tools.artifact_tool import ArtifactStoreRef
from tools.base import BaseTool, ToolResult


@dataclass
class EvidenceLedgerRef:
    """Mutable reference used to bind tools to the active agent evidence ledger."""

    ledger: EvidenceLedger | None = None


class EvidenceListTool(BaseTool):
    is_read_only = True
    def __init__(self, ledger_ref: EvidenceLedgerRef) -> None:
        self._ledger_ref = ledger_ref

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
        ledger = self._ledger_ref.ledger
        if ledger is None:
            return ToolResult(success=True, output="No evidence ledger is attached.")
        phase = str(params.get("phase", "")).strip()
        limit = max(1, int(params.get("limit", 10) or 10))
        records = ledger.records
        if phase:
            records = [record for record in records if record.phase == phase]
        records = records[:limit]
        if not records:
            return ToolResult(success=True, output="No evidence captured yet.")

        lines = ["Evidence records:"]
        for record in records:
            location = record.path or "(no path)"
            if record.range_text:
                location = f"{location} {record.range_text}"
            lines.append(
                f"- {record.evidence_id} | phase={record.phase} | tool={record.tool_name} | {location} | artifact={record.artifact_id or '(none)'}"
            )
        summaries = ledger.phase_summaries
        if phase:
            summaries = [summary for summary in summaries if summary.phase == phase]
        if summaries:
            lines.append("Phase summaries:")
            for summary in summaries[: max(1, min(limit, 5))]:
                lines.append(
                    f"- {summary.phase} | evidence={len(summary.evidence_ids)} | claims={len(summary.claims)} | recommended_reads={len(summary.recommended_verification_reads)}"
                )
        return ToolResult(success=True, output="\n".join(lines))


class EvidenceGetTool(BaseTool):
    is_read_only = True
    def __init__(self, ledger_ref: EvidenceLedgerRef) -> None:
        self._ledger_ref = ledger_ref

    @property
    def name(self) -> str:
        return "evidence_get"

    @property
    def description(self) -> str:
        return "Get one evidence record or phase summary by id from the current run."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "description": "Evidence id such as ev_ab12cd34."},
                "phase": {"type": "string", "description": "Phase summary name such as inspect or verify."},
            },
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        ledger = self._ledger_ref.ledger
        if ledger is None:
            return ToolResult(success=False, output="", error="No evidence ledger is attached.")

        evidence_id = str(params.get("evidence_id", "")).strip()
        phase = str(params.get("phase", "")).strip()
        if evidence_id:
            for record in ledger.records:
                if record.evidence_id == evidence_id:
                    return ToolResult(success=True, output=record.reference_text())
            return ToolResult(success=False, output="", error=f"Evidence not found: {evidence_id}")
        if phase:
            summary = ledger.phase_summary_for(phase)
            if summary is None:
                return ToolResult(success=False, output="", error=f"Phase summary not found: {phase}")
            return ToolResult(success=True, output=summary.prompt_text())
        return ToolResult(success=False, output="", error="evidence_id or phase is required")


class ArtifactSearchTool(BaseTool):
    is_read_only = True
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
