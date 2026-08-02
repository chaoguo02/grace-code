"""Explicit, run-local dependency scope for produced artifacts."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvidenceScope:
    """Tracks only evidence explicitly relevant to the current contract."""

    task_input_ids: tuple[str, ...] = ()
    active_skill_id: str | None = None
    parent_evidence_ids: tuple[str, ...] = ()
    required_tool_calls: tuple[Any, ...] = ()
    _required_evidence_ids: list[str] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note_tool_evidence(
        self,
        entry: Any,
        arguments: dict[str, object],
    ) -> None:
        for requirement in self.required_tool_calls:
            if getattr(requirement, "tool", "") != getattr(entry, "tool_name", ""):
                continue
            producer = str(
                getattr(requirement, "producer_session_id", "") or "",
            )
            if producer and producer != getattr(entry, "producer_session_id", ""):
                continue
            expected = dict(getattr(requirement, "arguments_match", {}) or {})
            if expected and any(arguments.get(k) != v for k, v in expected.items()):
                continue
            with self._lock:
                if entry.evidence_id not in self._required_evidence_ids:
                    self._required_evidence_ids.append(entry.evidence_id)
            return

    def resolved_dependency_ids(self, store_entries: list[Any]) -> tuple[str, ...]:
        valid = {
            entry.evidence_id
            for entry in store_entries
            if getattr(entry, "evidence_id", "")
            and getattr(getattr(entry, "status", None), "value", "") == "succeeded"
        }
        with self._lock:
            candidates = (
                list(self._required_evidence_ids)
                + list(self.parent_evidence_ids)
                + ([self.active_skill_id] if self.active_skill_id else [])
                + list(self.task_input_ids)
            )
        return tuple(dict.fromkeys(
            evidence_id for evidence_id in candidates if evidence_id in valid
        ))

    def fork(self) -> "EvidenceScope":
        """Create an isolated child scope from evidence consumed so far."""
        with self._lock:
            inherited = tuple(self._required_evidence_ids)
        return EvidenceScope(
            task_input_ids=self.task_input_ids,
            active_skill_id=self.active_skill_id,
            parent_evidence_ids=tuple(dict.fromkeys(
                (*self.parent_evidence_ids, *inherited)
            )),
            required_tool_calls=self.required_tool_calls,
        )

    def merge_consumed_child(self, child: "EvidenceScope") -> None:
        """Merge facts only after the parent consumes the child's result."""
        with child._lock:
            child_ids = tuple(child._required_evidence_ids)
        with self._lock:
            for evidence_id in child_ids:
                if evidence_id not in self._required_evidence_ids:
                    self._required_evidence_ids.append(evidence_id)

    def set_required_tool_calls(
        self, requirements: tuple[Any, ...],
    ) -> None:
        """Bind a contract before this isolated producer starts."""
        with self._lock:
            self.required_tool_calls = requirements
