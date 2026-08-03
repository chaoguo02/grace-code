"""observability/replay_validator.py

Replay validation and harness gates.

Validates replay records for:
- Schema compatibility (version, required fields)
- Step completeness (no missing or malformed step records)
- Boundary preservation (termination reason → status consistency)
- Replay consistency (deterministic decision fields present)

Gates reject runs that:
- Have incomplete or schema-incompatible records
- Violate boundary preservation (wrong status for termination reason)
- Have ambiguous or missing termination reasons
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from agent.task import RunStatus, TerminationReason
from observability.failure_policy import verify_boundary_preserved
from observability.models import (
    REPLAY_CONTRACT_VERSION,
    ReplayContractSnapshot,
    ReplayRunRecord,
    ReplayStepRecord,
)


@dataclass
class ValidationIssue:
    """A single validation failure."""

    severity: str  # "error" or "warning"
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.field}: {self.message}"


@dataclass
class ReplayValidationResult:
    """Result of validating a replay record."""

    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    boundary_preserved: bool = True
    steps_validated: int = 0

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


def validate_replay_snapshot(snapshot: dict[str, Any]) -> ReplayValidationResult:
    """Validate a serialized replay contract snapshot.

    Accepts the dict form (as read from JSONL) rather than the dataclass
    to support validation of persisted records without deserialization.
    """
    issues: list[ValidationIssue] = []

    # Schema version check
    version = snapshot.get("version")
    if version is None:
        issues.append(ValidationIssue("error", "version", "missing version field"))
    elif version != REPLAY_CONTRACT_VERSION:
        issues.append(ValidationIssue(
            "error", "version",
            f"unsupported version {version} (expected {REPLAY_CONTRACT_VERSION})",
        ))

    # Run record presence
    run = snapshot.get("run")
    if run is None:
        issues.append(ValidationIssue("error", "run", "missing run record"))
        return ReplayValidationResult(valid=False, issues=issues)

    # Validate run record
    run_issues, steps_validated, boundary_ok = _validate_run_record(run)
    issues.extend(run_issues)

    has_errors = any(i.severity == "error" for i in issues)
    return ReplayValidationResult(
        valid=not has_errors,
        issues=issues,
        boundary_preserved=boundary_ok,
        steps_validated=steps_validated,
    )


def validate_replay_run(run: dict[str, Any]) -> ReplayValidationResult:
    """Validate a serialized run record directly."""
    issues, steps_validated, boundary_ok = _validate_run_record(run)
    has_errors = any(i.severity == "error" for i in issues)
    return ReplayValidationResult(
        valid=not has_errors,
        issues=issues,
        boundary_preserved=boundary_ok,
        steps_validated=steps_validated,
    )


def validate_replay_step(step: dict[str, Any]) -> list[ValidationIssue]:
    """Validate a single step record."""
    return _validate_step_record(step, step.get("step", -1))


def gate_boundary_preservation(
    termination_reason: str,
    actual_status: str,
) -> tuple[bool, str]:
    """Harness gate: verify boundary was preserved.

    Returns (passed, message). Used by CI gates to reject boundary violations.
    """
    try:
        reason_enum = TerminationReason(termination_reason)
    except ValueError:
        return False, f"unknown termination_reason: {termination_reason}"

    try:
        status_enum = RunStatus(actual_status)
    except ValueError:
        return False, f"unknown run status: {actual_status}"

    return verify_boundary_preserved(reason_enum, status_enum)


def gate_replay_completeness(
    run: dict[str, Any],
    *,
    min_steps: int = 0,
) -> tuple[bool, str]:
    """Harness gate: verify replay record is complete.

    Returns (passed, message). Fails closed on any ambiguity.
    """
    if not isinstance(run, dict):
        return False, "run record is not a dict"

    version = run.get("version")
    if version != REPLAY_CONTRACT_VERSION:
        return False, f"version mismatch: got {version}, expected {REPLAY_CONTRACT_VERSION}"

    run_id = run.get("run_id")
    if not run_id:
        return False, "missing run_id"

    task_id = run.get("task_id")
    if not task_id:
        return False, "missing task_id"

    steps = run.get("steps", ())
    if len(steps) < min_steps:
        return False, f"too few steps: got {len(steps)}, expected at least {min_steps}"

    termination_reason = run.get("termination_reason", "none")
    if termination_reason == "none" and run.get("termination_status"):
        return False, "termination_status set but termination_reason is 'none'"

    return True, "replay record complete"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_run_record(run: dict[str, Any]) -> tuple[list[ValidationIssue], int, bool]:
    """Validate a run record. Returns (issues, steps_validated, boundary_preserved)."""
    issues: list[ValidationIssue] = []
    boundary_ok = True

    # Required identity fields
    for req_field in ("version", "run_id", "task_id"):
        if not run.get(req_field):
            issues.append(ValidationIssue("error", req_field, f"missing required field '{req_field}'"))

    # Version check
    version = run.get("version")
    if version is not None and version != REPLAY_CONTRACT_VERSION:
        issues.append(ValidationIssue(
            "error", "version",
            f"unsupported version {version}",
        ))

    # Steps validation
    steps = run.get("steps", ())
    steps_validated = 0
    if not isinstance(steps, (list, tuple)):
        issues.append(ValidationIssue("error", "steps", "steps must be a list"))
    else:
        for i, step in enumerate(steps):
            step_issues = _validate_step_record(step, i + 1)
            issues.extend(step_issues)
            if not any(si.severity == "error" for si in step_issues):
                steps_validated += 1

    # Boundary preservation check
    termination_reason = run.get("termination_reason", "none")
    termination_status = run.get("termination_status", "")
    if termination_reason != "none" and termination_status:
        passed, msg = gate_boundary_preservation(termination_reason, termination_status)
        if not passed:
            boundary_ok = False
            issues.append(ValidationIssue("error", "boundary", msg))

    return issues, steps_validated, boundary_ok


def _validate_step_record(step: dict[str, Any], expected_step_num: int) -> list[ValidationIssue]:
    """Validate a single step record."""
    issues: list[ValidationIssue] = []
    prefix = f"step[{expected_step_num}]"

    if not isinstance(step, dict):
        issues.append(ValidationIssue("error", prefix, "step record must be a dict"))
        return issues

    # Step number
    step_num = step.get("step")
    if step_num is None:
        issues.append(ValidationIssue("error", f"{prefix}.step", "missing step number"))

    # Runtime decision
    decision = step.get("runtime_decision")
    if decision is None:
        issues.append(ValidationIssue("error", f"{prefix}.runtime_decision", "missing runtime decision"))
    elif isinstance(decision, dict):
        action = decision.get("action")
        if action not in ("continue", "terminate", "inject"):
            issues.append(ValidationIssue(
                "warning", f"{prefix}.runtime_decision.action",
                f"unexpected action value: {action}",
            ))

    # Outcome field
    outcome = step.get("outcome")
    if outcome is None:
        issues.append(ValidationIssue("warning", f"{prefix}.outcome", "missing outcome field"))

    # Model action (optional but expected for non-terminal steps)
    model_action = step.get("model_action")
    if model_action is None and outcome == "continue":
        issues.append(ValidationIssue("warning", f"{prefix}.model_action", "continue step has no model_action"))

    return issues
