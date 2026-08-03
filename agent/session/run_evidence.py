"""Run-scoped evidence model and repository-backed projection.

Evidence belongs to one real top-level run.  Primary agents and workers use
the same ``root_run_id`` while keeping their own ``producer_session_id``.
SQLite (when configured) is authoritative; the in-memory store is only a
thread-safe projection used by the active runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping


class EvidenceKind(str, Enum):
    """Kinds of run evidence.

    Phase 4: SKILL_LOADED is deprecated as of v2.0 (sunset v2.0+60d).
    Skill activations through ToolExecutionPipeline produce TOOL_CALL_COMPLETED.
    Lifecycle paths (preload/CLI/HTTP) retain SKILL_LOADED until sunset.
    """
    SKILL_LOADED = "skill_loaded"  # @deprecated(since="v2.0", sunset="v2.0+60d")
    MCP_CONNECTED = "mcp_connected"
    MCP_TOOLS_EXPOSED = "mcp_tools_exposed"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_BLOCKED = "tool_call_blocked"
    CACHE_HIT = "cache_hit"
    ARTIFACT_WRITTEN = "artifact_written"
    ARTIFACT_OBSERVED = "artifact_observed"
    ARTIFACT_INTEGRITY_CHECKED = "artifact_integrity_checked"
    WORKER_STARTED = "worker_started"
    WORKER_COMPLETED = "worker_completed"
    VALIDATION_COMPLETED = "validation_completed"
    COMPLETION_EVALUATED = "completion_evaluated"
    RESUME_MARKER = "resume_marker"
    """Phase 3A: turn 边界续传标记。metadata 携带 tool_calls_hash 和
    files_hash；重启时比对当前 workspace 状态决定是否可跳过已完成 turns。
    生产恢复基于此 + Git，Checkpoint 仅为调试工具。"""


class EvidenceStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RequiredToolCall:
    tool: str
    arguments_match: Mapping[str, object] = field(default_factory=dict)
    minimum_count: int = 1
    producer_session_id: str = ""
    requirement_id: str = ""

    def __post_init__(self) -> None:
        if not self.tool.strip():
            raise ValueError("required tool name cannot be empty")
        if self.minimum_count < 1:
            raise ValueError("minimum_count must be positive")
        object.__setattr__(
            self, "arguments_match", _json_mapping(self.arguments_match),
        )
        if not self.requirement_id:
            seed = json.dumps({
                "tool": self.tool,
                "arguments": self.arguments_match,
                "minimum_count": self.minimum_count,
                "producer": self.producer_session_id,
            }, ensure_ascii=False, sort_keys=True)
            object.__setattr__(
                self,
                "requirement_id",
                f"req_tool_{hashlib.sha256(seed.encode()).hexdigest()[:12]}",
            )


@dataclass(frozen=True)
class RequiredArtifact:
    path: str
    must_depend_on_required_calls: bool = True
    require_integrity_check: bool = False
    requirement_id: str = ""

    def __post_init__(self) -> None:
        normalized = self.path.replace("\\", "/").strip()
        if not normalized:
            raise ValueError("required artifact path cannot be empty")
        object.__setattr__(self, "path", normalized)
        if not self.requirement_id:
            seed = (
                f"{normalized}|{self.must_depend_on_required_calls}|"
                f"{self.require_integrity_check}"
            )
            object.__setattr__(
                self,
                "requirement_id",
                f"req_artifact_{hashlib.sha256(seed.encode()).hexdigest()[:12]}",
            )


@dataclass(frozen=True)
class RunEvidenceRequirements:
    required_skills: frozenset[str] = frozenset()
    required_mcp_servers: frozenset[str] = frozenset()
    required_tool_calls: tuple[RequiredToolCall, ...] = ()
    required_artifacts: tuple[RequiredArtifact, ...] = ()
    verification_requirement: str = "not_required"
    require_started_workers_succeed: bool = True
    producer_session_id: str = ""

    def __post_init__(self) -> None:
        allowed = {"not_required", "required_if_workspace_changed", "required"}
        if self.verification_requirement not in allowed:
            raise ValueError(
                f"unknown verification requirement: {self.verification_requirement}",
            )

    @property
    def is_empty(self) -> bool:
        return not (
            self.required_skills
            or self.required_mcp_servers
            or self.required_tool_calls
            or self.required_artifacts
            or self.verification_requirement == "required"
            or self.require_started_workers_succeed
        )


@dataclass(frozen=True)
class MissingEvidence:
    code: str
    requirement_id: str = ""
    tool: str = ""
    arguments: Mapping[str, object] = field(default_factory=dict)
    retryable: bool = True
    blocking_reason: str = ""


@dataclass(frozen=True)
class FailedEvidence:
    evidence_id: str
    kind: str
    reason: str
    retryable: bool = True
    requirement_id: str = ""


@dataclass(frozen=True)
class EvidenceEvaluation:
    satisfied: bool
    missing: tuple[MissingEvidence, ...] = ()
    failed: tuple[FailedEvidence, ...] = ()
    total_required: int = 0
    total_satisfied: int = 0
    satisfied_evidence_ids: tuple[str, ...] = ()
    satisfied_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceEntry:
    """One immutable, JSON-safe piece of evidence."""

    evidence_id: str
    idempotency_key: str
    root_run_id: str
    session_id: str
    producer_session_id: str
    kind: EvidenceKind
    status: EvidenceStatus
    schema_version: int = 1
    sequence: int = 0
    root_session_id: str = ""
    turn_id: str = ""
    tool_name: str = ""
    call_id: str = ""
    invocation_id: str = ""
    parameters_digest: str = ""
    result_digest: str = ""
    source_fingerprint: str = ""
    cached: bool = False
    cache_key: str = ""
    path: str = ""
    artifact_id: str = ""
    depends_on: tuple[str, ...] = ()
    parent_evidence_id: str = ""
    summary: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            object.__setattr__(self, "kind", EvidenceKind(self.kind))
        if not isinstance(self.status, EvidenceStatus):
            object.__setattr__(self, "status", EvidenceStatus(self.status))
        if self.schema_version < 1:
            raise ValueError("evidence schema_version must be positive")
        object.__setattr__(self, "metadata", _json_mapping(self.metadata))
        object.__setattr__(
            self, "depends_on", tuple(dict.fromkeys(str(v) for v in self.depends_on if v)),
        )
        if self.path:
            object.__setattr__(self, "path", self.path.replace("\\", "/"))

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "idempotency_key": self.idempotency_key,
            "root_run_id": self.root_run_id,
            "root_session_id": self.root_session_id,
            "session_id": self.session_id,
            "producer_session_id": self.producer_session_id,
            "turn_id": self.turn_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "tool_name": self.tool_name,
            "call_id": self.call_id,
            "invocation_id": self.invocation_id,
            "parameters_digest": self.parameters_digest,
            "result_digest": self.result_digest,
            "source_fingerprint": self.source_fingerprint,
            "cached": self.cached,
            "cache_key": self.cache_key,
            "path": self.path,
            "artifact_id": self.artifact_id,
            "depends_on": list(self.depends_on),
            "parent_evidence_id": self.parent_evidence_id,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "EvidenceEntry":
        metadata = data.get("metadata", data.get("metadata_json", {}))
        depends_on = data.get("depends_on", data.get("depends_on_json", ()))
        if isinstance(metadata, str):
            metadata = _load_json(metadata, {})
        if isinstance(depends_on, str):
            depends_on = _load_json(depends_on, [])
        return cls(
            evidence_id=str(data["evidence_id"]),
            idempotency_key=str(data.get("idempotency_key", "")),
            root_run_id=str(data.get("root_run_id", "")),
            root_session_id=str(data.get("root_session_id", "")),
            session_id=str(data.get("session_id", "")),
            producer_session_id=str(data.get("producer_session_id", "")),
            turn_id=str(data.get("turn_id", "")),
            kind=EvidenceKind(str(data["kind"])),
            status=EvidenceStatus(str(data["status"])),
            schema_version=int(data.get("schema_version", 1) or 1),
            sequence=int(data.get("sequence", 0) or 0),
            tool_name=str(data.get("tool_name", "")),
            call_id=str(data.get("call_id", "")),
            invocation_id=str(data.get("invocation_id", "")),
            parameters_digest=str(data.get("parameters_digest", "")),
            result_digest=str(data.get("result_digest", "")),
            source_fingerprint=str(data.get("source_fingerprint", "")),
            cached=bool(data.get("cached", False)),
            cache_key=str(data.get("cache_key", "")),
            path=str(data.get("path", "")),
            artifact_id=str(data.get("artifact_id", "")),
            depends_on=tuple(str(v) for v in (depends_on or ())),
            parent_evidence_id=str(data.get("parent_evidence_id", "")),
            summary=str(data.get("summary", "")),
            metadata=dict(metadata or {}),
        )


PersistEvidence = Callable[[EvidenceEntry], EvidenceEntry | Mapping[str, object] | None]


class RunEvidenceStore:
    """Thread-safe projection for one real root run."""

    def __init__(
        self,
        root_run_id: str,
        *,
        root_session_id: str = "",
        turn_id: str = "",
        default_session_id: str = "",
        persist_fn: PersistEvidence | None = None,
        event_callback: Callable[[EvidenceEntry], None] | None = None,
    ) -> None:
        if not root_run_id.strip():
            raise ValueError("root_run_id must be a real run id")
        self._root_run_id = root_run_id
        self._root_session_id = root_session_id
        self._turn_id = turn_id
        # Standalone/in-memory callers historically used the run id as the
        # session id. Production always supplies the real session explicitly.
        self._default_session_id = default_session_id or root_run_id
        self._lock = threading.RLock()
        self._entries: list[EvidenceEntry] = []
        self._by_idempotency: dict[str, EvidenceEntry] = {}
        self._persist_fn = persist_fn
        self._event_callback = event_callback
        self._closed = False
        self._producer_refs = 1
        self._seq = 0
        self._last_evaluation: EvidenceEvaluation | None = None
        self._last_evaluation_sequence = -1

    @property
    def root_run_id(self) -> str:
        return self._root_run_id

    @property
    def root_session_id(self) -> str:
        return self._root_session_id

    @classmethod
    def load_from_db(
        cls,
        root_run_id: str,
        *,
        root_session_id: str = "",
        turn_id: str = "",
        default_session_id: str = "",
        list_fn: Callable[[str], list[Mapping[str, object]]] | None = None,
        persist_fn: PersistEvidence | None = None,
        event_callback: Callable[[EvidenceEntry], None] | None = None,
    ) -> "RunEvidenceStore":
        store = cls(
            root_run_id,
            root_session_id=root_session_id,
            turn_id=turn_id,
            default_session_id=default_session_id,
            persist_fn=persist_fn,
            event_callback=event_callback,
        )
        rows = list_fn(root_run_id) if list_fn is not None else []
        with store._lock:
            for row in rows:
                entry = EvidenceEntry.from_dict(row)
                if entry.root_run_id != root_run_id:
                    continue
                store._remember(entry)
            store._seq = max((e.sequence for e in store._entries), default=0)
        return store

    def close(self) -> None:
        self.release()

    def retain(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot retain a closed evidence store")
            self._producer_refs += 1

    def release(self) -> None:
        with self._lock:
            if self._producer_refs <= 0:
                return
            self._producer_refs -= 1
            if self._producer_refs == 0:
                self._closed = True

    def set_event_callback(
        self, callback: Callable[[EvidenceEntry], None] | None,
    ) -> None:
        with self._lock:
            self._event_callback = callback

    def record(self, entry: EvidenceEntry) -> EvidenceEntry:
        """Atomically persist and project one idempotent evidence entry."""
        with self._lock:
            if self._closed:
                raise RuntimeError(f"evidence store for run {self._root_run_id} is closed")
            existing = self._by_idempotency.get(entry.idempotency_key)
            if existing is not None:
                return existing
            candidate = self._bind(entry)
            if self._persist_fn is not None:
                persisted = self._persist_fn(candidate)
                if isinstance(persisted, EvidenceEntry):
                    canonical = persisted
                elif isinstance(persisted, Mapping):
                    canonical = EvidenceEntry.from_dict(persisted)
                else:
                    # Compatibility for a persistence callback that committed
                    # the supplied row but has not yet adopted return values.
                    canonical = candidate
            else:
                canonical = candidate
            if canonical.root_run_id != self._root_run_id:
                raise ValueError("persistence returned evidence for another run")
            existing = self._by_idempotency.get(canonical.idempotency_key)
            if existing is not None:
                return existing
            self._remember(canonical)

        if self._event_callback is not None:
            try:
                self._event_callback(canonical)
            except Exception:
                # Persistence is authoritative; a UI projection failure must not
                # roll back committed evidence.
                pass
        return canonical

    # ── Phase 3A: turn-boundary resume markers ───────────────────────────

    def record_resume_marker(self, session_id: str, turn_id: str,
                             tool_calls_hash: str,
                             files_hash: str) -> EvidenceEntry:
        """Record a turn-boundary resume marker.

        metadata carries the tool-call sequence hash and a workspace files
        snapshot hash.  On restart, should_resume_from_marker() compares the
        current workspace hash to decide whether completed turns can be
        skipped (rather than re-running from step 0).
        """
        entry = EvidenceEntry(
            evidence_id=f"resume_{self._root_run_id}_{turn_id}",
            idempotency_key=f"resume-marker:{self._root_run_id}:{turn_id}",
            root_run_id=self._root_run_id,
            session_id=session_id,
            producer_session_id=session_id,
            kind=EvidenceKind.RESUME_MARKER,
            status=EvidenceStatus.SUCCEEDED,
            turn_id=turn_id,
            metadata={
                "tool_calls_hash": tool_calls_hash,
                "files_hash": files_hash,
            },
        )
        return self.record(entry)

    def find_last_resume_marker(self, session_id: str) -> EvidenceEntry | None:
        """Return the most recent RESUME_MARKER for *session_id*, or None."""
        markers = [
            e for e in self._entries
            if e.kind is EvidenceKind.RESUME_MARKER and e.session_id == session_id
        ]
        if not markers:
            return None
        return max(markers, key=lambda e: e.sequence)

    def _bind(self, entry: EvidenceEntry) -> EvidenceEntry:
        if entry.root_run_id and entry.root_run_id != self._root_run_id:
            raise ValueError("evidence cannot cross root run boundaries")
        session_id = entry.session_id or self._default_session_id
        producer_id = entry.producer_session_id or session_id
        if not session_id or not producer_id:
            raise ValueError("session_id and producer_session_id are required")
        sequence = entry.sequence
        if self._persist_fn is None and sequence <= 0:
            self._seq += 1
            sequence = self._seq
        return replace(
            entry,
            root_run_id=self._root_run_id,
            root_session_id=entry.root_session_id or self._root_session_id,
            turn_id=entry.turn_id or self._turn_id,
            session_id=session_id,
            producer_session_id=producer_id,
            sequence=sequence,
        )

    def _remember(self, entry: EvidenceEntry) -> None:
        existing = self._by_idempotency.get(entry.idempotency_key)
        if existing is not None:
            return
        self._entries.append(entry)
        self._entries.sort(key=lambda value: (value.sequence, value.evidence_id))
        self._by_idempotency[entry.idempotency_key] = entry
        self._seq = max(self._seq, entry.sequence)

    def snapshot(self) -> list[EvidenceEntry]:
        with self._lock:
            return list(self._entries)

    def entries_by_producer(self, session_id: str) -> list[EvidenceEntry]:
        return [e for e in self.snapshot() if e.producer_session_id == session_id]

    def entries_by_kind(self, kind: EvidenceKind) -> list[EvidenceEntry]:
        return [e for e in self.snapshot() if e.kind == kind]

    def entries_by_path(self, path: str) -> list[EvidenceEntry]:
        normalized = path.replace("\\", "/")
        return [e for e in self.snapshot() if e.path == normalized]

    def get_by_id(self, evidence_id: str) -> EvidenceEntry | None:
        return next((e for e in self.snapshot() if e.evidence_id == evidence_id), None)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def last_evaluation(self) -> EvidenceEvaluation | None:
        with self._lock:
            if self._last_evaluation_sequence != self._seq:
                return None
            return self._last_evaluation

    def evaluate(self, requirements: RunEvidenceRequirements) -> EvidenceEvaluation:
        entries = self.snapshot()
        dynamic_mcp_servers: set[str] = set()
        dynamic_tool_calls: list[RequiredToolCall] = []
        for skill_entry in entries:
            if (
                skill_entry.kind != EvidenceKind.SKILL_LOADED
                or skill_entry.status != EvidenceStatus.SUCCEEDED
            ):
                continue
            dynamic_mcp_servers.update(
                str(value)
                for value in (
                    skill_entry.metadata.get("mcp_dependencies", []) or []
                )
                if str(value).strip()
            )
            raw_calls = skill_entry.metadata.get("required_tool_calls", [])
            if not isinstance(raw_calls, list):
                continue
            for raw_call in raw_calls:
                if not isinstance(raw_call, Mapping):
                    continue
                tool_name = str(raw_call.get("tool", "")).strip()
                if not tool_name:
                    continue
                try:
                    minimum_count = max(
                        1, int(raw_call.get("minimum_count", 1) or 1),
                    )
                except (TypeError, ValueError):
                    # Skill metadata is persisted input.  A malformed optional
                    # count must not crash terminal evaluation; ignore the
                    # malformed dynamic requirement and keep evaluating the
                    # remaining authoritative evidence.
                    continue
                dynamic_tool_calls.append(RequiredToolCall(
                    tool=tool_name,
                    arguments_match=(
                        raw_call.get("arguments_match", {})
                        if isinstance(
                            raw_call.get("arguments_match", {}), Mapping
                        )
                        else {}
                    ),
                    minimum_count=minimum_count,
                    producer_session_id=skill_entry.producer_session_id,
                ))
        missing: list[MissingEvidence] = []
        failed: list[FailedEvidence] = []
        satisfied_ids: list[str] = []
        satisfied_by: dict[str, tuple[str, ...]] = {}
        total = 0
        satisfied = 0

        for skill_name in sorted(requirements.required_skills):
            total += 1
            # Phase 4: Dual evidence — accept both TOOL_CALL_COMPLETED (tool path)
            # and SKILL_LOADED (lifecycle path). TOOL_CALL_COMPLETED uses flat
            # original name from SkillActivationTool; SKILL_LOADED uses "skill:{name}".
            found = None
            # Priority: check TOOL_CALL_COMPLETED first (primary path, flat name)
            found = next((
                e for e in entries
                if e.kind == EvidenceKind.TOOL_CALL_COMPLETED
                and e.status == EvidenceStatus.SUCCEEDED
                and e.tool_name == skill_name
            ), None)
            # Fallback: check SKILL_LOADED (lifecycle path, "skill:{name}" format)
            if found is None:
                found = next((
                    e for e in entries
                    if e.kind == EvidenceKind.SKILL_LOADED
                    and e.status == EvidenceStatus.SUCCEEDED
                    and e.tool_name == f"skill:{skill_name}"
                ), None)
            if found:
                satisfied += 1
                satisfied_ids.append(found.evidence_id)
                satisfied_by[f"req_skill:{skill_name}"] = (found.evidence_id,)
            else:
                missing.append(MissingEvidence(
                    code="required_skill_not_loaded",
                    requirement_id=f"req_skill:{skill_name}",
                    tool=skill_name,
                    retryable=True,
                    blocking_reason=f"Skill {skill_name!r} was not loaded",
                ))

        for server_name in sorted(
            set(requirements.required_mcp_servers) | dynamic_mcp_servers
        ):
            total += 1
            found = next((
                e for e in entries
                if e.kind == EvidenceKind.MCP_TOOLS_EXPOSED
                and e.status == EvidenceStatus.SUCCEEDED
                and e.tool_name == f"mcp:{server_name}"
            ), None)
            if found:
                satisfied += 1
                satisfied_ids.append(found.evidence_id)
                satisfied_by[f"req_mcp:{server_name}"] = (found.evidence_id,)
            else:
                missing.append(MissingEvidence(
                    code="required_mcp_not_exposed",
                    requirement_id=f"req_mcp:{server_name}",
                    tool=server_name,
                    retryable=True,
                    blocking_reason=f"MCP server {server_name!r} was not exposed",
                ))

        required_call_entries: list[EvidenceEntry] = []
        effective_tool_calls = list(requirements.required_tool_calls)
        known_requirement_ids = {
            requirement.requirement_id for requirement in effective_tool_calls
        }
        known_logical_calls = {
            (
                requirement.tool,
                json.dumps(
                    requirement.arguments_match,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                requirement.minimum_count,
            )
            for requirement in effective_tool_calls
        }
        for requirement in dynamic_tool_calls:
            logical_key = (
                requirement.tool,
                json.dumps(
                    requirement.arguments_match,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                requirement.minimum_count,
            )
            if (
                requirement.requirement_id in known_requirement_ids
                or logical_key in known_logical_calls
            ):
                continue
            effective_tool_calls.append(requirement)
            known_requirement_ids.add(requirement.requirement_id)
            known_logical_calls.add(logical_key)
        for requirement in effective_tool_calls:
            total += 1
            candidates = [
                e for e in entries
                if e.kind == EvidenceKind.TOOL_CALL_COMPLETED
                and e.tool_name == requirement.tool
                and (
                    not (
                        requirement.producer_session_id
                        or requirements.producer_session_id
                    )
                    or e.producer_session_id == (
                        requirement.producer_session_id
                        or requirements.producer_session_id
                    )
                )
                and (
                    not requirement.arguments_match
                    or _arguments_match(
                        _entry_arguments(e),
                        requirement.arguments_match,
                    )
                )
            ]
            succeeded_entries = [
                e for e in candidates if e.status == EvidenceStatus.SUCCEEDED
            ]
            if len(succeeded_entries) >= requirement.minimum_count:
                selected = succeeded_entries[-requirement.minimum_count:]
                required_call_entries.extend(selected)
                satisfied_ids.extend(e.evidence_id for e in selected)
                satisfied_by[requirement.requirement_id] = tuple(
                    e.evidence_id for e in selected
                )
                satisfied += 1
            elif candidates:
                latest = candidates[-1]
                failed.append(FailedEvidence(
                    evidence_id=latest.evidence_id,
                    kind="tool_call",
                    reason=(
                        f"Tool {requirement.tool!r} ended with "
                        f"status={latest.status.value}"
                    ),
                    retryable=latest.status != EvidenceStatus.BLOCKED,
                    requirement_id=requirement.requirement_id,
                ))
            else:
                blocked = next((
                    e for e in reversed(entries)
                    if e.kind == EvidenceKind.TOOL_CALL_BLOCKED
                    and e.tool_name == requirement.tool
                    and (
                        not (
                            requirement.producer_session_id
                            or requirements.producer_session_id
                        )
                        or e.producer_session_id == (
                            requirement.producer_session_id
                            or requirements.producer_session_id
                        )
                    )
                    and (
                        not requirement.arguments_match
                        or _arguments_match(
                            _entry_arguments(e),
                            requirement.arguments_match,
                        )
                    )
                ), None)
                if blocked is not None:
                    failed.append(FailedEvidence(
                        evidence_id=blocked.evidence_id,
                        kind="tool_call",
                        reason=blocked.summary or "required tool call was blocked",
                        retryable=False,
                        requirement_id=requirement.requirement_id,
                    ))
                else:
                    missing.append(MissingEvidence(
                        code=(
                            "required_mcp_evidence_missing"
                            if requirement.tool.startswith("mcp:")
                            else "required_tool_call_missing"
                        ),
                        requirement_id=requirement.requirement_id,
                        tool=requirement.tool,
                        arguments=dict(requirement.arguments_match),
                        retryable=True,
                    ))

        for requirement in requirements.required_artifacts:
            total += 1
            written = [
                e for e in entries
                if e.kind == EvidenceKind.ARTIFACT_WRITTEN
                and e.status == EvidenceStatus.SUCCEEDED
                and e.path == requirement.path
                and (
                    not requirements.producer_session_id
                    or e.producer_session_id
                    == requirements.producer_session_id
                )
            ]
            if not written:
                missing.append(MissingEvidence(
                    code="artifact_not_written",
                    requirement_id=requirement.requirement_id,
                    tool=requirement.path,
                    retryable=True,
                ))
                continue
            latest = written[-1]
            dependency_entries = {
                e.evidence_id: e for e in entries if e.evidence_id in latest.depends_on
            }
            unknown = set(latest.depends_on) - set(dependency_entries)
            future = [
                evidence_id for evidence_id, value in dependency_entries.items()
                if value.sequence >= latest.sequence
            ]
            invalid = [
                evidence_id for evidence_id, value in dependency_entries.items()
                if value.status != EvidenceStatus.SUCCEEDED
            ]
            if unknown or future or invalid:
                missing.append(MissingEvidence(
                    code="artifact_temporal_violation",
                    requirement_id=requirement.requirement_id,
                    tool=requirement.path,
                    arguments={
                        "unknown_dependencies": sorted(unknown),
                        "future_dependencies": sorted(future),
                        "invalid_dependencies": sorted(invalid),
                    },
                    retryable=True,
                ))
                continue
            if requirement.must_depend_on_required_calls:
                required_ids = {e.evidence_id for e in required_call_entries}
                absent = required_ids - set(latest.depends_on)
                if absent:
                    missing.append(MissingEvidence(
                        code="artifact_dependency_missing",
                        requirement_id=requirement.requirement_id,
                        tool=requirement.path,
                        arguments={"missing_dependencies": sorted(absent)},
                        retryable=True,
                    ))
                    continue
            if requirement.require_integrity_check:
                integrity = next((
                    e for e in reversed(entries)
                    if e.kind == EvidenceKind.ARTIFACT_INTEGRITY_CHECKED
                    and e.path == requirement.path
                    and e.sequence > latest.sequence
                    and latest.evidence_id in e.depends_on
                ), None)
                if integrity is None:
                    missing.append(MissingEvidence(
                        code="artifact_integrity_missing",
                        requirement_id=requirement.requirement_id,
                        tool=requirement.path,
                        retryable=True,
                    ))
                    continue
                if integrity.status != EvidenceStatus.SUCCEEDED:
                    failed.append(FailedEvidence(
                        evidence_id=integrity.evidence_id,
                        kind="artifact_integrity",
                        reason=integrity.summary or "artifact integrity failed",
                        retryable=True,
                        requirement_id=requirement.requirement_id,
                    ))
                    continue
                satisfied_ids.append(integrity.evidence_id)
            satisfied += 1
            satisfied_ids.append(latest.evidence_id)
            satisfied_by[requirement.requirement_id] = tuple(
                evidence_id
                for evidence_id in (
                    latest.evidence_id,
                    (
                        integrity.evidence_id
                        if requirement.require_integrity_check
                        else ""
                    ),
                )
                if evidence_id
            )

        if requirements.require_started_workers_succeed:
            started_workers = {
                e.producer_session_id: e
                for e in entries
                if e.kind == EvidenceKind.WORKER_STARTED
                and bool(e.metadata.get("required", True))
            }
            for producer_id, started_entry in started_workers.items():
                total += 1
                terminal = next((
                    e for e in reversed(entries)
                    if e.kind == EvidenceKind.WORKER_COMPLETED
                    and e.producer_session_id == producer_id
                    and e.sequence > started_entry.sequence
                ), None)
                if terminal is None:
                    missing.append(MissingEvidence(
                        code="worker_terminal_missing",
                        requirement_id=f"req_worker:{producer_id}",
                        tool=producer_id,
                        retryable=True,
                    ))
                elif terminal.status != EvidenceStatus.SUCCEEDED:
                    failed.append(FailedEvidence(
                        evidence_id=terminal.evidence_id,
                        kind="worker",
                        reason=terminal.summary or (
                            f"worker ended with {terminal.status.value}"
                        ),
                        retryable=terminal.status != EvidenceStatus.BLOCKED,
                        requirement_id=f"req_worker:{producer_id}",
                    ))
                else:
                    satisfied += 1
                    satisfied_ids.append(terminal.evidence_id)
                    satisfied_by[f"req_worker:{producer_id}"] = (
                        terminal.evidence_id,
                    )

        if requirements.verification_requirement == "required":
            total += 1
            latest_validation = next((
                e for e in reversed(entries)
                if e.kind == EvidenceKind.VALIDATION_COMPLETED
                and (
                    not requirements.producer_session_id
                    or e.producer_session_id
                    == requirements.producer_session_id
                )
            ), None)
            latest_write_seq = max((
                e.sequence for e in entries
                if e.kind == EvidenceKind.ARTIFACT_WRITTEN
                and e.status == EvidenceStatus.SUCCEEDED
                and (
                    not requirements.producer_session_id
                    or e.producer_session_id
                    == requirements.producer_session_id
                )
            ), default=0)
            if latest_validation is None:
                missing.append(MissingEvidence(
                    code="verification_missing",
                    requirement_id="req_verification",
                    retryable=True,
                ))
            elif latest_validation.status != EvidenceStatus.SUCCEEDED:
                failed.append(FailedEvidence(
                    evidence_id=latest_validation.evidence_id,
                    kind="validation",
                    reason=latest_validation.summary or "verification failed",
                    retryable=True,
                    requirement_id="req_verification",
                ))
            elif latest_validation.sequence <= latest_write_seq:
                missing.append(MissingEvidence(
                    code="verification_stale",
                    requirement_id="req_verification",
                    retryable=True,
                ))
            else:
                satisfied += 1
                satisfied_ids.append(latest_validation.evidence_id)
                satisfied_by["req_verification"] = (
                    latest_validation.evidence_id,
                )

        evaluation = EvidenceEvaluation(
            satisfied=not missing and not failed,
            missing=tuple(missing),
            failed=tuple(failed),
            total_required=total,
            total_satisfied=satisfied,
            satisfied_evidence_ids=tuple(dict.fromkeys(satisfied_ids)),
            satisfied_by=satisfied_by,
        )
        with self._lock:
            self._last_evaluation = evaluation
            self._last_evaluation_sequence = self._seq
        return evaluation


class EvidenceStoreManager:
    """Runtime owner of one active Store per real root run."""

    def __init__(
        self,
        *,
        persist_fn: PersistEvidence | None = None,
        list_fn: Callable[[str], list[Mapping[str, object]]] | None = None,
        event_callback: Callable[[EvidenceEntry], None] | None = None,
    ) -> None:
        self._persist_fn = persist_fn
        self._list_fn = list_fn
        self._event_callback = event_callback
        self._lock = threading.RLock()
        self._stores: dict[str, RunEvidenceStore] = {}

    def acquire(
        self,
        root_run_id: str,
        *,
        root_session_id: str = "",
        turn_id: str = "",
        default_session_id: str = "",
    ) -> RunEvidenceStore:
        with self._lock:
            existing = self._stores.get(root_run_id)
            if existing is not None:
                return existing
            store = RunEvidenceStore.load_from_db(
                root_run_id,
                root_session_id=root_session_id,
                turn_id=turn_id,
                default_session_id=default_session_id,
                list_fn=self._list_fn,
                persist_fn=self._persist_fn,
                event_callback=self._event_callback,
            )
            self._stores[root_run_id] = store
            return store

    def finish(self, root_run_id: str) -> None:
        with self._lock:
            store = self._stores.pop(root_run_id, None)
        if store is not None:
            store.close()

    def get(self, root_run_id: str) -> RunEvidenceStore | None:
        with self._lock:
            return self._stores.get(root_run_id)

    def set_event_callback(
        self, callback: Callable[[EvidenceEntry], None] | None,
    ) -> None:
        """Set the transport projector for future and active stores."""
        with self._lock:
            self._event_callback = callback
            for store in self._stores.values():
                store.set_event_callback(callback)


def idempotency_key_for_tool(
    phase: str,
    session_id: str,
    invocation_id: str,
    *,
    tool_name: str = "",
) -> str:
    return f"tool:{phase}:{session_id}:{invocation_id}:{tool_name}"


def idempotency_key_for_worker(phase: str, session_id: str, generation: int) -> str:
    return f"worker:{phase}:{session_id}:{generation}"


def idempotency_key_for_skill(
    skill_name: str,
    fingerprint: str,
    source: str = "",
    session_id: str = "",
) -> str:
    return f"skill:{session_id}:{skill_name}:{fingerprint}:{source}"


def canonical_tool_name(tool_name: str, tool: Any = None) -> str:
    props = getattr(tool, "mcp_props", None) if tool is not None else None
    server = str(getattr(props, "server_name", "") or "")
    if not server:
        return tool_name
    prefix = f"mcp:{server}:"
    if tool_name.startswith(prefix):
        return tool_name
    raw_prefix = f"mcp__{server}__"
    local_name = tool_name[len(raw_prefix):] if tool_name.startswith(raw_prefix) else tool_name
    return f"{prefix}{local_name}"


def build_evidence_projection(
    store: RunEvidenceStore | None,
    requirements: RunEvidenceRequirements | None = None,
) -> str:
    """Build a compact, reproducible runtime-state projection."""
    if store is None:
        return ""
    entries = store.snapshot()
    if not entries and (requirements is None or requirements.is_empty):
        return ""
    lines = [
        "[RUNTIME EVIDENCE STATE]",
        f"run_id={store.root_run_id}",
    ]
    critical_ids: set[str] = set()
    if requirements is not None and not requirements.is_empty:
        evaluation = store.evaluate(requirements)
        critical_ids.update(evaluation.satisfied_evidence_ids)
        critical_ids.update(item.evidence_id for item in evaluation.failed)
        lines.append(
            f"requirements={evaluation.total_satisfied}/"
            f"{evaluation.total_required} satisfied"
        )
        for item in evaluation.missing:
            lines.append(f"missing:{item.code}:{item.tool}")
        for item in evaluation.failed:
            lines.append(f"failed:{item.kind}:{item.evidence_id}:{item.reason}")
        if evaluation.satisfied_evidence_ids:
            lines.append(
                "satisfied_by="
                + ",".join(evaluation.satisfied_evidence_ids)
            )
        for requirement_id, evidence_ids in sorted(
            evaluation.satisfied_by.items()
        ):
            lines.append(
                f"requirement:{requirement_id}="
                + ",".join(evidence_ids)
            )
    selected: list[EvidenceEntry] = []
    for entry in entries:
        if entry.evidence_id in critical_ids:
            selected.append(entry)
    selected.extend(entries[-20:])
    selected = list({
        entry.evidence_id: entry for entry in selected
    }.values())
    selected.sort(key=lambda entry: entry.sequence)
    for entry in selected:
        if entry.kind in {
            EvidenceKind.SKILL_LOADED,
            EvidenceKind.MCP_TOOLS_EXPOSED,
            EvidenceKind.TOOL_CALL_COMPLETED,
            EvidenceKind.ARTIFACT_WRITTEN,
            EvidenceKind.VALIDATION_COMPLETED,
            EvidenceKind.WORKER_STARTED,
            EvidenceKind.WORKER_COMPLETED,
            EvidenceKind.COMPLETION_EVALUATED,
        }:
            target = entry.path or entry.tool_name
            lines.append(
                f"{entry.sequence}:{entry.kind.value}:{entry.status.value}:"
                f"{entry.evidence_id}:{target}"
            )
    return "\n".join(lines)


def _arguments_match(
    actual: object,
    expected: Mapping[str, object],
) -> bool:
    if not isinstance(actual, Mapping):
        return False
    return all(actual.get(key) == value for key, value in expected.items())


def _entry_arguments(entry: EvidenceEntry) -> Mapping[str, object]:
    """Read canonical arguments with a legacy-row migration fallback.

    New records always store parameters under ``metadata.arguments``. Rows
    written by the first Phase-2 prototype placed them at metadata's top
    level; accepting those rows here keeps persisted history evaluable without
    creating a second completion policy.
    """
    nested = entry.metadata.get("arguments")
    if isinstance(nested, Mapping):
        return nested
    return entry.metadata


def _json_mapping(value: Mapping[str, object] | object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    loaded = json.loads(serialized)
    return dict(loaded) if isinstance(loaded, dict) else {}


# ── Phase 3A: resume helpers ──────────────────────────────────────────────
# 不维护 step counter（对齐 CC）。用 workspace 文件快照哈希 + turn 边界
# marker 判定"已完成 turns 是否可跳过"。宁可重跑，不可错误跳过（R-D）。

_RESUME_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".grace"})


def workspace_files_hash(repo_path: str, *, limit: int = 2000) -> str:
    """Deterministic snapshot hash of the workspace files (path:size:mtime).

    Skips git metadata, dependencies, caches.  Returns a sha256 hex digest.
    A changed file (size or mtime) changes the hash — used to decide
    whether a recorded resume marker still reflects current workspace state.
    """
    h = hashlib.sha256()
    count = 0
    try:
        walk = os.walk(repo_path)
    except OSError:
        return h.hexdigest()
    for root, dirs, files in walk:
        dirs[:] = [d for d in dirs if d not in _RESUME_SKIP_DIRS]
        for name in sorted(files):
            full = os.path.join(root, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, repo_path).replace("\\", "/")
            h.update(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
            count += 1
            if count >= limit:
                return h.hexdigest()
    return h.hexdigest()


def should_resume_from_marker(
    marker: EvidenceEntry | None,
    current_files_hash: str,
) -> bool:
    """True when *marker* exists and its workspace hash matches the current state.

    Matching → the turn (and all before it) is considered complete; the run
    may skip straight past it.  Mismatch → discard the marker and re-run
    (never wrongly skip work, R-D).
    """
    if marker is None:
        return False
    stored = marker.metadata.get("files_hash", "")
    return bool(stored) and stored == current_files_hash


def _load_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
