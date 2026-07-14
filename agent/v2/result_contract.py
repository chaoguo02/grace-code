"""Typed, Runtime-validated result contract for forked analysis agents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class FindingSeverity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class FindingCategory(str, Enum):
    BUG = "bug"
    IMPROVEMENT = "improvement"
    HYPOTHESIS = "hypothesis"


class SubagentReportStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NO_FINDINGS = "no_findings"


@dataclass(frozen=True)
class Finding:
    severity: FindingSeverity
    category: FindingCategory
    title: str
    description: str
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    code_snippet: str = ""
    verification: str = ""
    recommendation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", FindingSeverity(self.severity))
        object.__setattr__(self, "category", FindingCategory(self.category))

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, repo_path: str | None = None,
    ) -> "Finding":
        file_path = _normalize_project_path(data.get("file_path", ""), repo_path)
        line_start = _non_negative_int(data.get("line_start", 0), "line_start")
        line_end = _non_negative_int(data.get("line_end", 0), "line_end")
        if line_start and not line_end:
            line_end = line_start
        if line_end and not line_start:
            raise ValueError("line_start is required when line_end is set")
        if line_end and line_end < line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        if (line_start or line_end) and not file_path:
            raise ValueError("file_path is required when a line range is set")
        if repo_path is not None and file_path and line_end:
            _verify_line_range(file_path, line_end)
        return cls(
            severity=FindingSeverity(data.get("severity", "")),
            category=FindingCategory(data.get("category", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            file_path=file_path,
            line_start=line_start,
            line_end=line_end,
            code_snippet=str(data.get("code_snippet", "")),
            verification=str(data.get("verification", "")),
            recommendation=str(data.get("recommendation", "")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity.value,
            "category": self.category.value,
            "title": self.title,
            "description": self.description,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "code_snippet": self.code_snippet,
            "verification": self.verification,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class SubagentReport:
    status: SubagentReportStatus
    findings: tuple[Finding, ...] = ()
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SubagentReportStatus(self.status))
        if self.status is SubagentReportStatus.NO_FINDINGS and self.findings:
            raise ValueError("A no_findings report cannot contain findings")

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, repo_path: str | None = None,
    ) -> "SubagentReport":
        return cls(
            status=SubagentReportStatus(data.get("status", "completed")),
            findings=tuple(
                Finding.from_dict(item, repo_path=repo_path)
                for item in data.get("findings", [])
            ),
            summary=str(data.get("summary", "")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    @property
    def bugs(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.category is FindingCategory.BUG)

    @property
    def improvements(self) -> tuple[Finding, ...]:
        return tuple(
            f for f in self.findings if f.category is FindingCategory.IMPROVEMENT
        )

    @property
    def hypotheses(self) -> tuple[Finding, ...]:
        return tuple(
            f for f in self.findings if f.category is FindingCategory.HYPOTHESIS
        )

    @property
    def high_severity(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.HIGH)


def _normalize_project_path(raw_path: object, repo_path: str | None) -> str:
    value = str(raw_path or "").strip()
    if not value:
        return ""
    path = Path(value)
    if repo_path is None:
        if not path.is_absolute():
            raise ValueError("file_path must be absolute")
        return str(path.resolve())
    repo = Path(repo_path).resolve()
    resolved = (path if path.is_absolute() else repo / path).resolve()
    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise ValueError(f"file_path is outside project scope: {resolved}") from exc
    if not resolved.is_file():
        raise ValueError(f"file_path does not exist: {resolved}")
    return str(resolved)


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _verify_line_range(file_path: str, line_end: int) -> None:
    with Path(file_path).open("r", encoding="utf-8", errors="replace") as handle:
        line_count = sum(1 for _ in handle)
    if line_end > line_count:
        raise ValueError(
            f"line_end {line_end} exceeds file length {line_count}: {file_path}"
        )
