"""
tools/submit_findings_tool.py

SubmitFindingsTool: structured analysis report submission for subagents.

Claude Code pattern: the model MUST call a dedicated tool with a JSON Schema
to submit findings. No regex parsing, no format guessing — the Runtime validates
the structure before the parent agent ever sees it.

This replaces the fragile regex-based report validation in task_tool.py
(_BUG_CLAIM_PATTERN, _FILE_LINE_PATTERN, _SECTION_MARKERS) with a single
deterministic tool call. The parent receives structured data, not free text.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


# ── Type-safe data contracts (Python layer — parent agent can iterate directly) ──

@dataclass
class Finding:
    """A single analysis finding with typed fields.

    This is the canonical in-memory representation. When the parent agent
    receives structured findings, it gets a tuple[Finding, ...] — no
    regex parsing, no format guessing.
    """

    severity: str       # HIGH | MEDIUM | LOW
    category: str       # bug | improvement | hypothesis
    title: str
    description: str
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    code_snippet: str = ""
    verification: str = ""
    recommendation: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        return cls(
            severity=d.get("severity", ""),
            category=d.get("category", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            file_path=d.get("file_path", ""),
            line_start=d.get("line_start", 0),
            line_end=d.get("line_end", 0),
            code_snippet=d.get("code_snippet", ""),
            verification=d.get("verification", ""),
            recommendation=d.get("recommendation", ""),
        )


@dataclass
class SubagentReport:
    """Complete structured report from a subagent.

    The canonical result of an analysis subagent. When submit_findings
    is called, the Runtime validates the JSON Schema, constructs this
    object, and the parent agent receives it as typed data.
    """

    status: str          # completed | partial | no_findings
    findings: tuple[Finding, ...] = ()
    summary: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SubagentReport":
        findings = tuple(
            Finding.from_dict(f) for f in d.get("findings", [])
        )
        return cls(
            status=d.get("status", "completed"),
            findings=findings,
            summary=d.get("summary", ""),
        )

    @property
    def bugs(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.category == "bug")

    @property
    def improvements(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.category == "improvement")

    @property
    def hypotheses(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.category == "hypothesis")

    @property
    def high_severity(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "HIGH")


# ── JSON Schema (for LLM tool-call validation) ──

FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {
            "type": "string",
            "enum": ["HIGH", "MEDIUM", "LOW"],
            "description": "Impact/urgency of this finding",
        },
        "category": {
            "type": "string",
            "enum": ["bug", "improvement", "hypothesis"],
            "description": "bug=confirmed defect, improvement=style/robustness suggestion, hypothesis=unverified suspicion",
        },
        "file_path": {
            "type": "string",
            "description": "Repo-relative path to the file, e.g. agent/v2/task_tool.py",
        },
        "line_start": {
            "type": "integer",
            "description": "1-indexed starting line number (required if file_path is set)",
        },
        "line_end": {
            "type": "integer",
            "description": "1-indexed ending line number (can equal line_start for single-line)",
        },
        "title": {
            "type": "string",
            "description": "One-line summary of the finding",
        },
        "description": {
            "type": "string",
            "description": "Detailed explanation of what's wrong or what could be improved",
        },
        "code_snippet": {
            "type": "string",
            "description": "The actual code lines this finding refers to",
        },
        "verification": {
            "type": "string",
            "description": "How you confirmed this finding (cross-reference file, test, etc.)",
        },
        "recommendation": {
            "type": "string",
            "description": "What should be done to address this finding",
        },
    },
    "required": ["severity", "category", "title", "description"],
}


# ── JSON Schema for the full report ──

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "partial", "no_findings"],
            "description": "completed=analysis finished, partial=incomplete (hit limits), no_findings=nothing found",
        },
        "summary": {
            "type": "string",
            "description": "1-3 sentence summary of the analysis results",
        },
        "findings": {
            "type": "array",
            "items": FINDING_SCHEMA,
            "description": "List of findings. Empty if status=no_findings.",
        },
    },
    "required": ["status", "findings"],
}


# ── Accumulator ──

@dataclass
class FindingsAccumulator:
    """Mutable collector for structured findings during a subagent run."""

    reports: list[dict[str, Any]] = field(default_factory=list)

    def submit(self, report: dict[str, Any]) -> None:
        """Record a submitted report."""
        self.reports.append(report)

    def all_findings(self) -> list[dict[str, Any]]:
        """Flatten all findings across all reports."""
        findings: list[dict[str, Any]] = []
        for report in self.reports:
            for f in report.get("findings", []):
                findings.append(f)
        return findings

    def has_any(self) -> bool:
        """Whether any findings were submitted."""
        return len(self.all_findings()) > 0

    def reset(self) -> None:
        """Clear all accumulated reports. Call before each subagent run."""
        self.reports.clear()


# ── Tool ──

class SubmitFindingsTool(BaseTool):
    """Submit a structured analysis report.

    Subagents MUST call this tool (possibly multiple times) before finishing.
    Each call submits a batch of findings. The Runtime validates the JSON
    Schema, and the parent agent receives the structured data directly —
    no regex parsing, no format guessing.

    Usage (by subagent):
        submit_findings({
            "status": "completed",
            "summary": "Found 3 bugs in task_tool.py",
            "findings": [
                {
                    "severity": "LOW",
                    "category": "bug",
                    "file_path": "agent/v2/task_tool.py",
                    "line_start": 348,
                    "line_end": 356,
                    "title": "Redundant str() call",
                    "description": "...",
                    "code_snippet": "...",
                    "verification": "Cross-referenced with _xml_escape at L355",
                    "recommendation": "Remove outer str() call"
                }
            ]
        })
    """

    def __init__(self, accumulator: FindingsAccumulator | None = None) -> None:
        self._accumulator = accumulator or FindingsAccumulator()

    @property
    def accumulator(self) -> FindingsAccumulator:
        return self._accumulator

    @property
    def name(self) -> str:
        return "submit_findings"

    @property
    def description(self) -> str:
        return (
            "Submit a structured analysis report with findings. "
            "MUST be called before finishing an analysis task. "
            "Each finding requires severity, category, title, and description. "
            "Use multiple calls to submit findings in batches. "
            "Call with status='no_findings' and empty findings array if nothing was found."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "report": REPORT_SCHEMA,
            },
            "required": ["report"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        report = params.get("report")
        if not isinstance(report, dict):
            return ToolResult(
                success=False, output="",
                error="'report' parameter must be a JSON object matching the report schema",
            )

        # ── Validate required fields ──
        status = report.get("status")
        if status not in ("completed", "partial", "no_findings"):
            return ToolResult(
                success=False, output="",
                error=(
                    f"Invalid status: {status!r}. "
                    "Must be 'completed', 'partial', or 'no_findings'."
                ),
            )

        findings = report.get("findings")
        if not isinstance(findings, list):
            return ToolResult(
                success=False, output="",
                error="'findings' must be an array",
            )

        # ── Validate each finding ──
        valid_severities = {"HIGH", "MEDIUM", "LOW"}
        valid_categories = {"bug", "improvement", "hypothesis"}
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                return ToolResult(
                    success=False, output="",
                    error=f"findings[{i}] must be an object",
                )
            severity = f.get("severity")
            if severity not in valid_severities:
                return ToolResult(
                    success=False, output="",
                    error=f"findings[{i}].severity must be HIGH, MEDIUM, or LOW, got {severity!r}",
                )
            category = f.get("category")
            if category not in valid_categories:
                return ToolResult(
                    success=False, output="",
                    error=f"findings[{i}].category must be bug, improvement, or hypothesis, got {category!r}",
                )
            if not f.get("title"):
                return ToolResult(
                    success=False, output="",
                    error=f"findings[{i}].title is required",
                )
            if not f.get("description"):
                return ToolResult(
                    success=False, output="",
                    error=f"findings[{i}].description is required",
                )

        # ── Store ──
        self._accumulator.submit(report)
        total_findings = len(findings)
        logger.info(
            "SubmitFindings: status=%s, findings=%d, total_accumulated=%d",
            status, total_findings, len(self._accumulator.all_findings()),
        )

        return ToolResult(
            success=True,
            output=(
                f"✓ Report accepted. "
                f"Status: {status}. "
                f"Findings submitted: {total_findings}. "
                f"Total accumulated in this session: {len(self._accumulator.all_findings())}."
            ),
        )
