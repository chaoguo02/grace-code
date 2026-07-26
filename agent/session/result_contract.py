"""Typed, Runtime-validated result contract for analysis subagents."""

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


class WorkerReportStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NO_FINDINGS = "no_findings"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    PRODUCT_FAILURE = "product_failure"
    ENVIRONMENT_FAILURE = "environment_failure"
    TIMEOUT = "timeout"
    NOT_RUN = "not_run"


@dataclass(frozen=True)
class VerificationResult:
    command: str
    status: VerificationStatus
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", VerificationStatus(self.status))

    def to_dict(self) -> dict[str, str]:
        return {
            "command": self.command,
            "status": self.status.value,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VerificationResult":
        return cls(
            command=str(data.get("command", "")),
            status=VerificationStatus(data.get("status", "not_run")),
            summary=str(data.get("summary", "")),
        )


@dataclass(frozen=True)
class ChangedFile:
    path: str
    change: str = "modified"

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "change": self.change}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangedFile":
        return cls(
            path=str(data.get("path", "")),
            change=str(data.get("change", "modified")),
        )


@dataclass(frozen=True)
class WorkerReport:
    """Uniform result envelope for analysis, edit, and verification workers."""

    task_id: str
    session_id: str
    generation: int
    agent_type: str
    status: WorkerReportStatus
    summary: str = ""
    findings: tuple[Finding, ...] = ()
    changed_files: tuple[ChangedFile, ...] = ()
    verification: tuple[VerificationResult, ...] = ()
    unresolved: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    tokens_used: int = 0
    duration_ms: int = 0
    worktree: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", WorkerReportStatus(self.status))
        if not self.task_id:
            raise ValueError("WorkerReport.task_id must be non-empty")
        if not self.session_id:
            raise ValueError("WorkerReport.session_id must be non-empty")
        if self.generation < 0:
            raise ValueError("WorkerReport.generation cannot be negative")
        if self.tokens_used < 0 or self.duration_ms < 0:
            raise ValueError("WorkerReport usage values cannot be negative")
        if self.status is WorkerReportStatus.NO_FINDINGS and self.findings:
            raise ValueError("A no_findings WorkerReport cannot contain findings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "generation": self.generation,
            "agent_type": self.agent_type,
            "status": self.status.value,
            "summary": self.summary,
            "findings": [item.to_dict() for item in self.findings],
            "changed_files": [item.to_dict() for item in self.changed_files],
            "verification": [item.to_dict() for item in self.verification],
            "unresolved": list(self.unresolved),
            "warnings": list(self.warnings),
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "worktree": self.worktree,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerReport":
        return cls(
            task_id=str(data.get("task_id", "")),
            session_id=str(data.get("session_id", "")),
            generation=int(data.get("generation", 0)),
            agent_type=str(data.get("agent_type", "")),
            status=WorkerReportStatus(data.get("status", "failed")),
            summary=str(data.get("summary", "")),
            findings=tuple(
                Finding.from_dict(item)
                for item in data.get("findings", [])
                if isinstance(item, dict)
            ),
            changed_files=tuple(
                ChangedFile.from_dict(item)
                for item in data.get("changed_files", [])
                if isinstance(item, dict)
            ),
            verification=tuple(
                VerificationResult.from_dict(item)
                for item in data.get("verification", [])
                if isinstance(item, dict)
            ),
            unresolved=tuple(str(item) for item in data.get("unresolved", [])),
            warnings=tuple(str(item) for item in data.get("warnings", [])),
            tokens_used=int(data.get("tokens_used", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
            worktree=(
                dict(data["worktree"])
                if isinstance(data.get("worktree"), dict) else None
            ),
        )


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
