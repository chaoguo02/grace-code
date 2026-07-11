"""RunFinalizer — post-run memory extraction and structured precipitation.

Constitution: agent/ owns "finish/give_up 的统一收束". This module handles
the memory extraction side of that — what happens AFTER a task succeeds.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory.models import Anchor, Memory, MemoryMetadata

if TYPE_CHECKING:
    from agent.task import Task
    from agent.event_log import EventLog
    from llm.base import LLMBackend
    from memory.store import MemoryStore
    from memory.context import MemoryContext

logger = logging.getLogger(__name__)


class RunFinalizer:
    """Post-run memory extraction. Two paths: structured precipitation (Phase 4)
    and LLM reflection (legacy fallback). Neither blocks the task result."""

    def __init__(
        self,
        memory_context: "MemoryContext | None",
        backend: "LLMBackend | None",
    ) -> None:
        self._memory_context = memory_context
        self._backend = backend

    def extract(
        self,
        task: "Task",
        log: "EventLog",
        summary: str,
        *,
        accumulated_findings: list[dict] | None = None,
        skip_llm: bool = False,
    ) -> int:
        """Extract memories after successful task. Returns count of written memories.

        Never raises — failures are logged and swallowed.
        """
        if not self._memory_context or not self._memory_context.enabled:
            return 0
        store = getattr(self._memory_context, "store", None)
        if store is None:
            return 0
        try:
            written = 0
            # Path 1: structured precipitation (Phase 4)
            if accumulated_findings:
                written += self._precipitate(accumulated_findings, store)
            # Path 2: LLM reflection (fallback)
            if not skip_llm:
                written += self._extract_via_llm(task, log, summary, store)
            if written:
                logger.debug("Extracted %d success memories", written)
            return written
        except Exception as exc:
            logger.warning("Success memory extraction skipped: %s", exc)
            return 0

    # ── Path 1: structured precipitation ─────────────────────────────────

    def _precipitate(self, findings: list[dict], store: "MemoryStore") -> int:
        """From structured Finding dicts → Memory objects. Zero LLM cost."""
        written = 0
        for f in findings:
            severity = str(f.get("severity", "")).upper()
            category = str(f.get("category", "")).lower()
            if severity != "HIGH" or category != "bug":
                continue
            title = str(f.get("title", "")).strip()
            description = str(f.get("description", "")).strip()
            file_path = str(f.get("file_path", "")).strip()
            line_start = f.get("line_start", 0)
            recommendation = str(f.get("recommendation", "")).strip()
            if not title or not description:
                continue
            content_parts = [
                f"**Severity**: {severity}",
                f"**Category**: {category}",
                f"**Description**: {description}",
            ]
            if file_path:
                loc = file_path + (f":{line_start}" if line_start else "")
                content_parts.insert(0, f"**Location**: {loc}")
            if recommendation:
                content_parts.append(f"**Recommendation**: {recommendation}")
            anchors = []
            if file_path:
                ch = ""
                try:
                    p = Path(file_path)
                    if p.exists():
                        ch = hashlib.sha256(p.read_bytes()).hexdigest()
                except (OSError, IOError):
                    pass
                anchors.append(Anchor(kind="file", path=file_path, content_hash=ch))
            name = _slugify(title)
            memory = Memory(
                name=name, description=title[:120], content="\n".join(content_parts),
                metadata=MemoryMetadata(
                    type="project", status="active", scope="project",
                    confidence=0.8, ttl_seconds=30 * 24 * 3600,
                ),
                anchors=anchors,
            )
            try:
                consolidate = getattr(store, "consolidate", None)
                if callable(consolidate):
                    from memory.extractor import MemoryCandidate
                    candidate = MemoryCandidate(
                        type="project", name=name, description=title[:120],
                        content="\n".join(content_parts), anchors=anchors, confidence="high",
                    )
                    if consolidate(candidate, external_store=None, backend=None) != "NOOP":
                        written += 1
                elif store.write_memory(memory):
                    written += 1
            except Exception:
                pass
        if written:
            logger.info("Precipitated %d structured memories", written)
        return written

    # ── Path 2: LLM reflection ──────────────────────────────────────────

    def _extract_via_llm(
        self, task: "Task", log: "EventLog", summary: str, store: "MemoryStore",
    ) -> int:
        from memory.extractor import MemoryExtractor
        extractor = MemoryExtractor(backend=self._backend)
        external_store = None
        retriever = getattr(self._memory_context, "_retriever", None)
        if retriever is not None:
            external_store = getattr(retriever, "_store", None)
        return extractor.write_success_memories(
            task, log, summary, store, external_store=external_store, skip_auto_extract=False,
        )


# ── Utility ──────────────────────────────────────────────────────────────

def _slugify(title: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9\s-]", "", title.lower().strip())
    slug = re.sub(r"\s+", "-", slug)[:60].strip("-") or "finding"
    return f"{slug}-{hashlib.md5(title.encode('utf-8')).hexdigest()[:8]}"
