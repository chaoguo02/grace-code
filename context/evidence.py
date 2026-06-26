"""
context/evidence.py

Evidence lifecycle for phased analysis.

Raw tool observations are facts collected during a phase. EvidenceLedger turns
those observations into structured records, phase summaries, and compact prompt
references so completed phases no longer need to carry raw tool output forever.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from context.token_budget import estimate_tokens

if TYPE_CHECKING:
    from llm.base import LLMBackend


@dataclass
class EvidenceRecord:
    """Structured evidence extracted from one successful tool observation."""

    evidence_id: str
    phase: str
    tool_name: str
    path: str = ""
    range_text: str = ""
    summary: str = ""
    artifact_id: str = ""
    token_count: int = 0
    key_evidence: bool = False

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "phase": self.phase,
            "tool_name": self.tool_name,
            "path": self.path,
            "range_text": self.range_text,
            "summary": self.summary,
            "artifact_id": self.artifact_id,
            "token_count": self.token_count,
            "key_evidence": self.key_evidence,
        }

    def reference_text(self) -> str:
        """Compact prompt-safe reference for completed phase evidence."""
        location = self.path or "(no path)"
        if self.range_text:
            location = f"{location} {self.range_text}"
        artifact = f" artifact={self.artifact_id}" if self.artifact_id else ""
        return (
            f"[Evidence {self.evidence_id} | phase={self.phase} | "
            f"tool={self.tool_name} | {location} | ~{self.token_count} tokens{artifact}]\n"
            f"{self.summary}"
        )


@dataclass
class Claim:
    claim_id: str
    text: str
    status: str
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "source_paths": list(self.source_paths),
        }

    def prompt_text(self) -> str:
        evidence = " ".join(f"[{evidence_id}]" for evidence_id in self.evidence_ids) or "[no-evidence]"
        return f"- {self.status} [{self.claim_id}] {evidence}: {self.text}"


@dataclass
class PhaseSummary:
    """Summary of evidence collected during one analysis phase."""

    phase: str
    evidence_ids: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    confirmed_facts: list[str] = field(default_factory=list)
    open_gaps: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    confidence_boundaries: list[str] = field(default_factory=list)
    recommended_verification_reads: list[str] = field(default_factory=list)
    semantic: bool = False
    token_count: int = 0

    def prompt_text(self) -> str:
        files = ", ".join(self.files[:8]) if self.files else "(none)"
        if len(self.files) > 8:
            files += f", ... and {len(self.files) - 8} more"
        facts = "\n".join(f"- {fact}" for fact in self.confirmed_facts) or "- (none yet)"
        gaps = "\n".join(f"- {gap}" for gap in self.open_gaps) or "- (none named yet)"
        boundaries = "\n".join(f"- {item}" for item in self.confidence_boundaries) or "- (none noted)"
        reads = "\n".join(f"- {path}" for path in self.recommended_verification_reads) or "- (none recommended)"
        evidence = ", ".join(self.evidence_ids) if self.evidence_ids else "(none)"
        artifacts = ", ".join(self.artifact_ids) if self.artifact_ids else "(none)"
        claims = "\n".join(claim.prompt_text() for claim in self.claims) or "- (none yet)"
        mode = "semantic" if self.semantic else "deterministic"
        return (
            f"## Phase Summary: {self.phase} ({mode})\n"
            f"Files: {files}\n"
            f"Evidence: {evidence}\n"
            f"Artifacts: {artifacts}\n"
            "Claims:\n"
            f"{claims}\n"
            "Confirmed facts:\n"
            f"{facts}\n"
            "Open gaps:\n"
            f"{gaps}\n"
            "Confidence boundaries:\n"
            f"{boundaries}\n"
            "Recommended verification reads:\n"
            f"{reads}"
        )


class EvidenceLedger:
    """In-memory evidence ledger for a single agent run."""

    def __init__(self, summary_chars: int = 700) -> None:
        self._records: list[EvidenceRecord] = []
        self._phase_summaries: dict[str, PhaseSummary] = {}
        self._summary_chars = summary_chars

    @property
    def records(self) -> list[EvidenceRecord]:
        return list(self._records)

    @property
    def phase_summaries(self) -> list[PhaseSummary]:
        return list(self._phase_summaries.values())

    @property
    def evidence_count(self) -> int:
        return len(self._records)

    @property
    def phase_summary_count(self) -> int:
        return len(self._phase_summaries)

    def add_observation(
        self,
        *,
        phase: str,
        tool_name: str,
        output: str,
        path: str = "",
        range_text: str = "",
        artifact_id: str = "",
        key_evidence: bool = False,
    ) -> EvidenceRecord:
        token_count = estimate_tokens(output or "")
        summary = self._summarize_output(output)
        evidence_id = self._make_evidence_id(phase, tool_name, path, range_text, summary)
        record = EvidenceRecord(
            evidence_id=evidence_id,
            phase=phase,
            tool_name=tool_name,
            path=path,
            range_text=range_text,
            summary=summary,
            artifact_id=artifact_id,
            token_count=token_count,
            key_evidence=key_evidence,
        )
        self._records.append(record)
        return record

    def summarize_phase(self, phase: str) -> PhaseSummary:
        phase_records = [record for record in self._records if record.phase == phase]
        files = sorted({record.path for record in phase_records if record.path})
        evidence_ids = [record.evidence_id for record in phase_records]
        artifact_ids = sorted({record.artifact_id for record in phase_records if record.artifact_id})
        token_count = sum(record.token_count for record in phase_records)
        confirmed = [
            self._fact_from_record(record)
            for record in phase_records[:8]
        ]
        claims = [
            self._claim_from_record(record)
            for record in phase_records[:8]
        ]
        gaps = [
            "Name one specific verification gap before reading more files.",
        ] if phase_records else []
        summary = PhaseSummary(
            phase=phase,
            evidence_ids=evidence_ids,
            files=files,
            claims=claims,
            confirmed_facts=confirmed,
            open_gaps=gaps,
            artifact_ids=artifact_ids,
            token_count=token_count,
        )
        self._phase_summaries[phase] = summary
        return summary

    def summarize_phase_semantically(
        self,
        phase: str,
        backend: "LLMBackend | None",
        task_description: str = "",
    ) -> PhaseSummary:
        """Summarize phase evidence with an LLM, falling back to deterministic summary."""
        fallback = self.summarize_phase(phase)
        if backend is None:
            return fallback
        if hasattr(backend, "_summary_responses") and not getattr(backend, "_summary_responses"):
            return fallback
        records = [record for record in self._records if record.phase == phase]
        if not records:
            return fallback

        try:
            from llm.base import LLMMessage

            evidence_text = self._format_records_for_summary(records)
            prompt = (
                "Summarize the evidence for this analysis phase as JSON only. "
                "Do not include markdown. Required keys: confirmed_facts, open_gaps, "
                "confidence_boundaries, recommended_verification_reads. "
                "Each value must be an array of short strings. "
                f"Current task: {task_description}\n"
                f"Phase: {phase}\n\n"
                f"Evidence:\n{evidence_text}"
            )
            response = backend.complete(
                messages=[
                    LLMMessage(
                        role="system",
                        content="You produce concise structured evidence summaries for coding agents.",
                    ),
                    LLMMessage(role="user", content=prompt),
                ],
                tools=[],
            )
            parsed = self._parse_summary_json(response.raw_content)
            if not parsed:
                return fallback
            summary = PhaseSummary(
                phase=phase,
                evidence_ids=fallback.evidence_ids,
                files=fallback.files,
                claims=fallback.claims,
                confirmed_facts=parsed.get("confirmed_facts") or fallback.confirmed_facts,
                open_gaps=parsed.get("open_gaps") or fallback.open_gaps,
                artifact_ids=fallback.artifact_ids,
                confidence_boundaries=parsed.get("confidence_boundaries") or [],
                recommended_verification_reads=parsed.get("recommended_verification_reads") or [],
                semantic=True,
                token_count=fallback.token_count,
            )
            self._phase_summaries[phase] = summary
            return summary
        except Exception:
            return fallback

    def latest_phase_summary_text(self) -> str:
        if not self._phase_summaries:
            return ""
        latest = list(self._phase_summaries.values())[-1]
        return latest.prompt_text()

    def compact_reference_for_tool_result(self, content: str) -> str | None:
        """Return an evidence reference if content matches a completed phase record."""
        for record in self._records:
            if record.summary and record.summary in content:
                if record.phase in self._phase_summaries:
                    return record.reference_text()
        return None

    def recommended_reads_for_phase(self, phase: str) -> set[str]:
        summary = self._phase_summaries.get(phase)
        if summary is None:
            return set()
        return {path for path in summary.recommended_verification_reads if path}

    def known_evidence_ids(self) -> set[str]:
        return {record.evidence_id for record in self._records if record.evidence_id}

    def key_evidence_records(self) -> list["EvidenceRecord"]:
        """Return records marked as key evidence, most recent first."""
        return [r for r in reversed(self._records) if r.key_evidence and r.evidence_id]

    def all_records(self) -> list["EvidenceRecord"]:
        """Return all records with evidence ids, most recent first."""
        return [r for r in reversed(self._records) if r.evidence_id]

    def latest_claims(self) -> list[Claim]:
        if not self._phase_summaries:
            return []
        latest = list(self._phase_summaries.values())[-1]
        return list(latest.claims)

    def phase_summary_for(self, phase: str) -> PhaseSummary | None:
        return self._phase_summaries.get(phase)

    def total_claim_count(self) -> int:
        return sum(len(summary.claims) for summary in self._phase_summaries.values())

    def _format_records_for_summary(self, records: list[EvidenceRecord]) -> str:
        parts = []
        for record in records:
            parts.append(record.reference_text())
        return "\n\n".join(parts)

    def _parse_summary_json(self, text: str) -> dict[str, list[str]] | None:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        result: dict[str, list[str]] = {}
        for key in (
            "confirmed_facts",
            "open_gaps",
            "confidence_boundaries",
            "recommended_verification_reads",
        ):
            value = data.get(key, [])
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                value = []
            result[key] = [str(item) for item in value if str(item).strip()]
        return result

    def _summarize_output(self, output: str) -> str:
        text = (output or "").strip()
        if not text:
            return "(no output)"
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return text[:self._summary_chars]
        selected = lines[:8]
        summary = "\n".join(selected)
        if len(summary) > self._summary_chars:
            summary = summary[:self._summary_chars].rstrip() + "..."
        elif len(lines) > len(selected):
            summary += f"\n... [{len(lines) - len(selected)} more lines summarized]"
        return summary

    def _make_evidence_id(
        self,
        phase: str,
        tool_name: str,
        path: str,
        range_text: str,
        summary: str,
    ) -> str:
        seed = f"{phase}|{tool_name}|{path}|{range_text}|{summary}|{len(self._records)}"
        digest = hashlib.sha1(seed.encode("utf-8", errors="replace")).hexdigest()[:8]
        return f"ev_{digest}"

    def _fact_from_record(self, record: EvidenceRecord) -> str:
        location = record.path or record.tool_name
        if record.range_text:
            location = f"{location} {record.range_text}"
        first_line = record.summary.splitlines()[0] if record.summary else "evidence captured"
        return f"{location}: {first_line}"

    def _claim_from_record(self, record: EvidenceRecord) -> Claim:
        text = self._fact_from_record(record)
        return Claim(
            claim_id=f"cl_{record.evidence_id[3:]}",
            text=text,
            status="confirmed",
            evidence_ids=[record.evidence_id],
            confidence=0.75 if record.key_evidence else 0.6,
            source_paths=[record.path] if record.path else [],
        )
