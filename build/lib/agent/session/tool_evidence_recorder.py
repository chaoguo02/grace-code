"""Single evidence projector for all Tool executions."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Mapping

from agent.session.run_evidence import (
    EvidenceEntry,
    EvidenceKind,
    EvidenceStatus,
    RunEvidenceStore,
    canonical_tool_name,
    idempotency_key_for_tool,
)
from core.types import ToolEffect

_SECRET_KEY = re.compile(
    r"(api[_-]?key|token|password|secret|authorization|cookie|credential)",
    re.IGNORECASE,
)
_MAX_METADATA_JSON = 16_000


class ToolEvidenceRecorder:
    """Projects one logical Tool invocation into typed run evidence."""

    def __init__(self, store: RunEvidenceStore, *, scope: Any = None) -> None:
        self._store = store
        self._scope = scope

    def record_started(
        self,
        tool_name: str,
        params: dict[str, Any],
        invocation_id: str,
        tool: Any,
        session_id: str,
    ) -> EvidenceEntry:
        arguments = _sanitize_mapping(params)
        return self._record(
            kind=EvidenceKind.TOOL_CALL_STARTED,
            status=EvidenceStatus.STARTED,
            tool_name=tool_name,
            arguments=arguments,
            invocation_id=invocation_id,
            tool=tool,
            session_id=session_id,
        )

    def record_completed(
        self,
        tool_name: str,
        params: dict[str, Any],
        result: Any,
        invocation_id: str,
        tool: Any,
        session_id: str,
    ) -> EvidenceEntry:
        arguments = _sanitize_mapping(params)
        status = (
            EvidenceStatus.SUCCEEDED
            if bool(getattr(result, "success", False))
            else EvidenceStatus.FAILED
        )
        completed = self._record(
            kind=EvidenceKind.TOOL_CALL_COMPLETED,
            status=status,
            tool_name=tool_name,
            arguments=arguments,
            invocation_id=invocation_id,
            tool=tool,
            session_id=session_id,
            result=result,
        )
        if status == EvidenceStatus.SUCCEEDED and self._scope is not None:
            self._scope.note_tool_evidence(completed, arguments)

        if bool(getattr(result, "cached", False)):
            self._record(
                kind=EvidenceKind.CACHE_HIT,
                status=status,
                tool_name=tool_name,
                arguments=arguments,
                invocation_id=invocation_id,
                tool=tool,
                session_id=session_id,
                result=result,
                parent_evidence_id=completed.evidence_id,
            )

        evidence_meta = _domain_evidence(result)
        if status == EvidenceStatus.SUCCEEDED and evidence_meta.get("skill_name"):
            self._record_skill(
                evidence_meta, session_id, invocation_id, completed.evidence_id,
            )

        effects = set(getattr(getattr(tool, "metadata", None), "effects", ()) or ())
        if status == EvidenceStatus.SUCCEEDED and (
            ToolEffect.WRITE_WORKSPACE in effects or evidence_meta.get("path")
        ):
            self._record_artifact(
                completed=completed,
                evidence_meta=evidence_meta,
                result=result,
                tool=tool,
                session_id=session_id,
                invocation_id=invocation_id,
                tool_name=canonical_tool_name(tool_name, tool),
            )
        if status == EvidenceStatus.SUCCEEDED and evidence_meta.get("observed_path"):
            self._record_artifact_observation(
                completed=completed,
                evidence_meta=evidence_meta,
                tool=tool,
                session_id=session_id,
                invocation_id=invocation_id,
                tool_name=canonical_tool_name(tool_name, tool),
            )

        receipt = _receipt(result)
        if receipt is not None or ToolEffect.TEST in effects:
            self._record_validation(
                completed=completed,
                receipt=receipt or {},
                result=result,
                session_id=session_id,
                invocation_id=invocation_id,
                tool_name=canonical_tool_name(tool_name, tool),
            )
        if hasattr(result, "metadata") and isinstance(result.metadata, dict):
            related = [
                entry.evidence_id
                for entry in self._store.snapshot()
                if entry.parent_evidence_id == completed.evidence_id
            ]
            result.metadata["evidence_ref"] = {
                "evidence_id": completed.evidence_id,
                "kind": completed.kind.value,
                "status": completed.status.value,
                "cached": completed.cached,
                "source_fingerprint": completed.source_fingerprint,
                "related_evidence_ids": related,
            }
        return completed

    def record_blocked(
        self,
        tool_name: str,
        params: dict[str, Any],
        reason: str,
        invocation_id: str,
        session_id: str,
        tool: Any = None,
    ) -> EvidenceEntry:
        return self._record(
            kind=EvidenceKind.TOOL_CALL_BLOCKED,
            status=EvidenceStatus.BLOCKED,
            tool_name=tool_name,
            arguments=_sanitize_mapping(params),
            invocation_id=invocation_id,
            tool=tool,
            session_id=session_id,
            summary=reason,
        )

    def _record(
        self,
        *,
        kind: EvidenceKind,
        status: EvidenceStatus,
        tool_name: str,
        arguments: dict[str, object],
        invocation_id: str,
        tool: Any,
        session_id: str,
        result: Any = None,
        parent_evidence_id: str = "",
        summary: str = "",
    ) -> EvidenceEntry:
        props = getattr(tool, "mcp_props", None) if tool is not None else None
        fingerprint = _source_fingerprint(props, _domain_evidence(result))
        output = str(getattr(result, "output", "") or "") if result is not None else ""
        metadata = {
            "arguments": arguments,
            "result": _safe_result_metadata(result),
        }
        return self._store.record(EvidenceEntry(
            evidence_id=f"ev_{uuid.uuid4().hex[:16]}",
            idempotency_key=idempotency_key_for_tool(
                kind.value,
                session_id,
                invocation_id,
                tool_name=canonical_tool_name(tool_name, tool),
            ),
            root_run_id="",
            session_id=session_id,
            producer_session_id=session_id,
            kind=kind,
            status=status,
            tool_name=canonical_tool_name(tool_name, tool),
            call_id=invocation_id,
            invocation_id=invocation_id,
            parameters_digest=_digest_json(arguments),
            result_digest=_sha256(output) if output else "",
            source_fingerprint=fingerprint,
            cached=bool(getattr(result, "cached", False)) if result is not None else False,
            cache_key=str(
                getattr(result, "cache_key", "")
                or _domain_evidence(result).get("cache_key", "")
            ),
            parent_evidence_id=parent_evidence_id,
            summary=summary or _summarize(result),
            metadata=metadata,
        ))

    def _record_skill(
        self,
        evidence_meta: Mapping[str, object],
        session_id: str,
        invocation_id: str,
        parent_id: str,
    ) -> None:
        name = str(evidence_meta["skill_name"])
        fingerprint = str(evidence_meta.get("skill_fingerprint", ""))
        self._store.record(EvidenceEntry(
            evidence_id=f"ev_{uuid.uuid4().hex[:16]}",
            idempotency_key=(
                f"skill:{session_id}:{invocation_id}:{name}:{fingerprint}"
            ),
            root_run_id="",
            session_id=session_id,
            producer_session_id=session_id,
            kind=EvidenceKind.SKILL_LOADED,
            status=EvidenceStatus.SUCCEEDED,
            tool_name=f"skill:{name}",
            source_fingerprint=fingerprint,
            parent_evidence_id=parent_id,
            metadata={
                "source": "tool_call",
                "mcp_dependencies": list(
                    evidence_meta.get("mcp_dependencies", []) or [],
                ),
                "required_tool_calls": list(
                    evidence_meta.get("required_tool_calls", []) or [],
                ),
                "arguments_digest": str(
                    evidence_meta.get("arguments_digest", ""),
                ),
            },
        ))

    def _record_artifact(
        self,
        *,
        completed: EvidenceEntry,
        evidence_meta: Mapping[str, object],
        result: Any,
        tool: Any,
        session_id: str,
        invocation_id: str,
        tool_name: str,
    ) -> None:
        paths = list(getattr(result, "modified_files", []) or [])
        if evidence_meta.get("path"):
            paths.append(str(evidence_meta["path"]))
        for raw_path in dict.fromkeys(paths):
            path = _canonical_artifact_path(raw_path, tool)
            dependencies = (
                self._scope.resolved_dependency_ids(self._store.snapshot())
                if self._scope is not None else ()
            )
            dependencies = tuple(dict.fromkeys((*dependencies, completed.evidence_id)))
            content_hash = str(evidence_meta.get("content_hash", ""))
            self._store.record(EvidenceEntry(
                evidence_id=f"ev_{uuid.uuid4().hex[:16]}",
                idempotency_key=f"artifact:{session_id}:{invocation_id}:{path}",
                root_run_id="",
                session_id=session_id,
                producer_session_id=session_id,
                kind=EvidenceKind.ARTIFACT_WRITTEN,
                status=EvidenceStatus.SUCCEEDED,
                tool_name=tool_name,
                invocation_id=invocation_id,
                path=path,
                artifact_id=f"artifact:{_sha256(path)[:16]}",
                result_digest=content_hash,
                depends_on=dependencies,
                parent_evidence_id=completed.evidence_id,
                metadata={"content_hash": content_hash},
            ))

    def _record_validation(
        self,
        *,
        completed: EvidenceEntry,
        receipt: Mapping[str, object],
        result: Any,
        session_id: str,
        invocation_id: str,
        tool_name: str,
    ) -> None:
        passed = (
            str(receipt.get("status", "")).lower() == "passed"
            if receipt else bool(getattr(result, "success", False))
        )
        status = EvidenceStatus.SUCCEEDED if passed else EvidenceStatus.FAILED
        self._store.record(EvidenceEntry(
            evidence_id=f"ev_{uuid.uuid4().hex[:16]}",
            idempotency_key=f"validation:{session_id}:{invocation_id}",
            root_run_id="",
            session_id=session_id,
            producer_session_id=session_id,
            kind=EvidenceKind.VALIDATION_COMPLETED,
            status=status,
            tool_name=tool_name,
            invocation_id=invocation_id,
            parent_evidence_id=completed.evidence_id,
            summary=str(receipt.get("reason", "") or _summarize(result)),
            metadata=_sanitize_mapping(receipt),
        ))

    def _record_artifact_observation(
        self,
        *,
        completed: EvidenceEntry,
        evidence_meta: Mapping[str, object],
        tool: Any,
        session_id: str,
        invocation_id: str,
        tool_name: str,
    ) -> None:
        path = _canonical_artifact_path(evidence_meta["observed_path"], tool)
        current_hash = str(evidence_meta.get("content_hash", ""))
        observed = self._store.record(EvidenceEntry(
            evidence_id=f"ev_{uuid.uuid4().hex[:16]}",
            idempotency_key=f"artifact-observed:{session_id}:{invocation_id}:{path}",
            root_run_id="",
            session_id=session_id,
            producer_session_id=session_id,
            kind=EvidenceKind.ARTIFACT_OBSERVED,
            status=EvidenceStatus.SUCCEEDED,
            tool_name=tool_name,
            invocation_id=invocation_id,
            path=path,
            result_digest=current_hash,
            parent_evidence_id=completed.evidence_id,
            depends_on=(completed.evidence_id,),
            metadata={"content_hash": current_hash},
        ))
        prior_writes = [
            entry for entry in self._store.entries_by_path(path)
            if entry.kind == EvidenceKind.ARTIFACT_WRITTEN
            and entry.sequence < observed.sequence
        ]
        if not prior_writes:
            return
        expected_hash = str(prior_writes[-1].metadata.get("content_hash", ""))
        matched = bool(expected_hash and current_hash and expected_hash == current_hash)
        self._store.record(EvidenceEntry(
            evidence_id=f"ev_{uuid.uuid4().hex[:16]}",
            idempotency_key=f"artifact-integrity:{session_id}:{invocation_id}:{path}",
            root_run_id="",
            session_id=session_id,
            producer_session_id=session_id,
            kind=EvidenceKind.ARTIFACT_INTEGRITY_CHECKED,
            status=(
                EvidenceStatus.SUCCEEDED if matched else EvidenceStatus.FAILED
            ),
            tool_name=tool_name,
            invocation_id=invocation_id,
            path=path,
            result_digest=current_hash,
            parent_evidence_id=observed.evidence_id,
            depends_on=(prior_writes[-1].evidence_id, observed.evidence_id),
            summary="" if matched else "artifact content hash changed",
            metadata={
                "expected_hash": expected_hash,
                "observed_hash": current_hash,
            },
        ))


def _domain_evidence(result: Any) -> dict[str, object]:
    metadata = getattr(result, "metadata", {}) if result is not None else {}
    if not isinstance(metadata, Mapping):
        return {}
    value = metadata.get("evidence", {})
    return _sanitize_mapping(value) if isinstance(value, Mapping) else {}


def _receipt(result: Any) -> dict[str, object] | None:
    metadata = getattr(result, "metadata", {}) if result is not None else {}
    value = metadata.get("verification_receipt") if isinstance(metadata, Mapping) else None
    return _sanitize_mapping(value) if isinstance(value, Mapping) else None


def _safe_result_metadata(result: Any) -> dict[str, object]:
    metadata = getattr(result, "metadata", {}) if result is not None else {}
    if not isinstance(metadata, Mapping):
        return {}
    safe = {
        key: value
        for key, value in metadata.items()
        if key not in {"skill_modifier", "verification_receipt", "evidence"}
    }
    return _sanitize_mapping(safe)


def _sanitize_mapping(value: Mapping[str, object] | object) -> dict[str, object]:
    sanitized = _sanitize_value(value)
    if not isinstance(sanitized, dict):
        return {}
    encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    if len(encoded) > _MAX_METADATA_JSON:
        return {"truncated": True, "digest": _sha256(encoded)}
    return sanitized


def _sanitize_value(value: object, *, key: str = "") -> object:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(k): _sanitize_value(v, key=str(k))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _source_fingerprint(props: Any, domain: Mapping[str, object]) -> str:
    if props is None:
        return str(domain.get("skill_fingerprint", ""))
    explicit = (
        getattr(props, "fingerprint", "")
        or getattr(props, "server_fingerprint", "")
        or domain.get("server_fingerprint", "")
    )
    if explicit:
        return str(explicit)
    schema_version = str(getattr(props, "schema_fingerprint", "") or "")
    return _sha256(
        f"{getattr(props, 'server_name', '')}|{schema_version}",
    )[:16]


def _canonical_artifact_path(raw_path: object, tool: Any) -> str:
    """Return a stable workspace-relative path when possible."""
    from pathlib import Path

    candidate = Path(str(raw_path))
    workspace = (
        getattr(tool, "_workspace_root", None)
        or getattr(tool, "workspace_root", None)
    )
    if workspace:
        root = Path(str(workspace)).resolve()
        try:
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (root / candidate).resolve()
            )
            return resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            pass
    return candidate.as_posix()


def _digest_json(value: object) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _digest_params(params: dict[str, Any] | None) -> str:
    """Compatibility helper retained for callers/tests."""
    return _digest_json(_sanitize_mapping(params or {})) if params else ""


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8", errors="replace")).hexdigest()


def _summarize(result: Any) -> str:
    if result is None:
        return ""
    error = str(getattr(result, "error", "") or "")
    if error:
        return error[:500]
    output = str(getattr(result, "output", "") or "")
    # Avoid persisting arbitrary Tool output as evidence.  A small single-line
    # description plus the result digest is enough for UI/debugging.
    return output.replace("\r", " ").replace("\n", " ")[:200]
