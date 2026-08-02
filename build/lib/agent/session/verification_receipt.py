"""VerificationReceipt — structured verification evidence.

The Completion Guard does NOT scan tool output text. It only accepts
a typed VerificationReceipt. This prevents the model from claiming
verification succeeded when no test was actually run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


VerificationKind = Literal[
    "test", "build", "lint", "review", "manual", "not_applicable",
]
VerificationStatus = Literal["passed", "failed", "unavailable"]


@dataclass(frozen=True)
class VerificationReceipt:
    """Structured proof that verification ran (or was legitimately skipped).

    The CompletionGuard only trusts this typed receipt — never text output.
    """

    kind: VerificationKind
    command_or_source: str = ""
    status: VerificationStatus = "unavailable"
    exit_code: int | None = None
    checked_revision: str = ""       # git revision that was verified
    affected_files: tuple[str, ...] = ()
    reason: str = ""                 # why not_applicable or why failed

    def __post_init__(self) -> None:
        if self.kind not in {
            "test", "build", "lint", "review", "manual", "not_applicable",
        }:
            raise ValueError(f"Unknown verification kind: {self.kind!r}")
        if self.status not in {"passed", "failed", "unavailable"}:
            raise ValueError(f"Unknown verification status: {self.status!r}")

    @classmethod
    def not_applicable(cls, reason: str = "") -> "VerificationReceipt":
        """Task did not modify the workspace — verification is not required."""
        return cls(
            kind="not_applicable",
            status="passed",  # not_applicable is NOT a failure
            reason=reason or "No workspace changes to verify",
        )

    @classmethod
    def passed_test(
        cls, command: str, *, exit_code: int = 0, revision: str = "",
        affected_files: tuple[str, ...] = (),
    ) -> "VerificationReceipt":
        return cls(
            kind="test",
            command_or_source=command,
            status="passed",
            exit_code=exit_code,
            checked_revision=revision,
            affected_files=affected_files,
        )

    @classmethod
    def failed_test(
        cls, command: str, *, exit_code: int, revision: str = "",
        reason: str = "",
    ) -> "VerificationReceipt":
        return cls(
            kind="test",
            command_or_source=command,
            status="failed",
            exit_code=exit_code,
            checked_revision=revision,
            reason=reason,
        )

    @classmethod
    def unavailable(
        cls, command: str, *, reason: str = "Test environment not available",
    ) -> "VerificationReceipt":
        return cls(
            kind="test",
            command_or_source=command,
            status="unavailable",
            reason=reason,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "command_or_source": self.command_or_source,
            "status": self.status,
            "exit_code": self.exit_code,
            "checked_revision": self.checked_revision,
            "affected_files": list(self.affected_files),
            "reason": self.reason,
        }

    @property
    def verification_passed(self) -> bool:
        """True if verification was either passed or legitimately not required."""
        return self.status == "passed"
