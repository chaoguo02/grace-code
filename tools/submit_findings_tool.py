"""Runtime-validated structured report submission for analysis subagents."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.v2.result_contract import (
    Finding,
    FindingCategory,
    FindingSeverity,
    SubagentReport,
    SubagentReportStatus,
)
from tools.base import BaseTool, ToolEffect, ToolMetadata, ToolResult

logger = logging.getLogger(__name__)


FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {
            "type": "string",
            "enum": [item.value for item in FindingSeverity],
            "description": "Impact and urgency of this finding",
        },
        "category": {
            "type": "string",
            "enum": [item.value for item in FindingCategory],
            "description": (
                "bug=confirmed defect, improvement=robustness suggestion, "
                "hypothesis=unverified suspicion"
            ),
        },
        "file_path": {
            "type": "string",
            "description": "Absolute path inside the target project",
        },
        "line_start": {
            "type": "integer",
            "minimum": 1,
            "description": "1-indexed starting line number",
        },
        "line_end": {
            "type": "integer",
            "minimum": 1,
            "description": "1-indexed ending line number",
        },
        "title": {"type": "string", "description": "One-line finding summary"},
        "description": {
            "type": "string",
            "description": "Detailed explanation of the finding",
        },
        "code_snippet": {
            "type": "string",
            "description": "Actual cited source lines",
        },
        "verification": {
            "type": "string",
            "description": "How the finding was confirmed",
        },
        "recommendation": {
            "type": "string",
            "description": "Recommended corrective action",
        },
    },
    "required": ["severity", "category", "title", "description"],
}

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [item.value for item in SubagentReportStatus],
            "description": "Completion status of this submitted report batch",
        },
        "summary": {
            "type": "string",
            "description": "Short summary of the analysis results",
        },
        "findings": {
            "type": "array",
            "items": FINDING_SCHEMA,
            "description": "Validated findings; empty for no_findings",
        },
    },
    "required": ["status", "findings"],
}


@dataclass
class FindingsAccumulator:
    """Collect typed report batches for one isolated child run."""

    reports: list[SubagentReport] = field(default_factory=list)

    def submit(self, report: SubagentReport) -> None:
        self.reports.append(report)

    def all_findings(self) -> list[Finding]:
        return [finding for report in self.reports for finding in report.findings]

    def combined_report(self) -> SubagentReport | None:
        if not self.reports:
            return None
        statuses = {report.status for report in self.reports}
        if SubagentReportStatus.PARTIAL in statuses:
            status = SubagentReportStatus.PARTIAL
        elif any(report.findings for report in self.reports):
            status = SubagentReportStatus.COMPLETED
        elif SubagentReportStatus.COMPLETED in statuses:
            status = SubagentReportStatus.COMPLETED
        else:
            status = SubagentReportStatus.NO_FINDINGS
        summaries = [report.summary.strip() for report in self.reports if report.summary.strip()]
        return SubagentReport(
            status=status,
            findings=tuple(self.all_findings()),
            summary="\n".join(summaries),
        )

    def has_any(self) -> bool:
        return bool(self.all_findings())

    def reset(self) -> None:
        self.reports.clear()


class SubmitFindingsTool(BaseTool):
    """Accept a report only after Runtime validation and path normalization."""

    metadata = ToolMetadata(effects=frozenset({ToolEffect.PRODUCE_DELIVERABLE}))

    def __init__(
        self, *, repo_path: str, accumulator: FindingsAccumulator | None = None,
    ) -> None:
        self._repo_path = str(Path(repo_path).resolve())
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
            "Submit a Runtime-validated structured analysis report. "
            "Confirmed bugs require an absolute project file path, line, "
            "source snippet, and verification. Call with status=no_findings "
            "and an empty findings array when nothing was found."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"report": REPORT_SCHEMA},
            "required": ["report"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        raw_report = params.get("report")
        if not isinstance(raw_report, dict):
            return _invalid("'report' must be an object matching the report schema")

        try:
            report_status = SubagentReportStatus(raw_report.get("status"))
        except (TypeError, ValueError):
            return _invalid(
                "Invalid report status; expected completed, partial, or no_findings"
            )

        raw_findings = raw_report.get("findings")
        if not isinstance(raw_findings, list):
            return _invalid("'findings' must be an array")
        if report_status is SubagentReportStatus.NO_FINDINGS and raw_findings:
            return _invalid("status=no_findings requires an empty findings array")

        for index, raw_finding in enumerate(raw_findings):
            error = _validate_finding_shape(raw_finding, index)
            if error:
                return _invalid(error)

        try:
            typed_report = SubagentReport.from_dict(
                raw_report, repo_path=self._repo_path,
            )
        except (OSError, TypeError, ValueError) as exc:
            return _invalid(f"Invalid report evidence: {exc}")

        self._accumulator.submit(typed_report)
        total_findings = len(typed_report.findings)
        logger.info(
            "SubmitFindings: status=%s, findings=%d, total_accumulated=%d",
            typed_report.status.value,
            total_findings,
            len(self._accumulator.all_findings()),
        )
        return ToolResult(
            success=True,
            output=(
                f"Report accepted. Status: {typed_report.status.value}. "
                f"Findings submitted: {total_findings}. "
                f"Total accumulated: {len(self._accumulator.all_findings())}."
            ),
        )


def _validate_finding_shape(raw_finding: object, index: int) -> str:
    if not isinstance(raw_finding, dict):
        return f"findings[{index}] must be an object"
    try:
        FindingSeverity(raw_finding.get("severity"))
    except (TypeError, ValueError):
        return f"findings[{index}].severity must be HIGH, MEDIUM, or LOW"
    try:
        category = FindingCategory(raw_finding.get("category"))
    except (TypeError, ValueError):
        return f"findings[{index}].category is invalid"
    if not raw_finding.get("title"):
        return f"findings[{index}].title is required"
    if not raw_finding.get("description"):
        return f"findings[{index}].description is required"
    if category is FindingCategory.BUG:
        required_evidence = ("file_path", "line_start", "code_snippet", "verification")
        missing = [name for name in required_evidence if not raw_finding.get(name)]
        if missing:
            return f"findings[{index}] confirmed bug lacks evidence: {', '.join(missing)}"
    return ""


def _invalid(error: str) -> ToolResult:
    return ToolResult(success=False, output="", error=error)
