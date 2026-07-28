"""Session-aware active memory recall.

This module owns runtime memory retrieval, session-scoped recall records, and
session-level pin/disable overrides.  It is deliberately independent from the
agent loop so both prompt injection and the web UI can use the same facts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import logging
import sqlite3
from typing import Any, Callable, Literal
from uuid import uuid4

from memory.models import Memory, MemoryStatus
from memory.token_estimator import get_estimator

logger = logging.getLogger(__name__)

RecallSource = Literal["always", "semantic", "scoped", "pinned"]
OverrideAction = Literal["pin", "disable"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MemoryRecallQuery:
    session_id: str
    user_message: str = ""
    task_description: str = ""
    agent_name: str = ""
    mode: str = ""
    repo_path: str = "."
    session_title: str = ""
    active_files: tuple[str, ...] = ()
    recent_tools: tuple[str, ...] = ()
    turn_id: str = ""
    top_k: int = 8


@dataclass
class MemoryRecallRecord:
    session_id: str
    memory_name: str
    source: str
    score: float
    reason: str
    confidence: float
    scope: str
    injected: bool
    omitted_reason: str = ""
    turn_id: str = ""
    created_at: str = ""
    description: str = ""
    type: str = ""
    override: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryRecallResult:
    session_id: str
    injection_text: str
    records: list[MemoryRecallRecord]
    total_candidates: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "injection_text": self.injection_text,
            "records": [r.to_dict() for r in self.records],
            "items": [r.to_dict() for r in self.records],
            "total_candidates": self.total_candidates,
            "created_at": self.created_at,
        }


class MemoryRecallService:
    """Build and record session-aware memory recall results."""

    def __init__(
        self,
        store: Any,
        retriever: Any | None = None,
        *,
        max_injected: int = 8,
        max_tokens: int = 3000,
        min_confidence: float = 0.3,
        event_callback: Callable[[str, MemoryRecallResult], None] | None = None,
        recall_retention_days: int = 7,
    ) -> None:
        self._store = store
        self._retriever = retriever
        self._max_injected = max_injected
        self._max_tokens = max_tokens
        self._min_confidence = min_confidence
        self._event_callback = event_callback
        self._estimator = get_estimator()
        self._recall_retention_days = recall_retention_days
        self._db_path = self._detect_db_path(store)
        self._fallback_recalls: list[MemoryRecallRecord] = []
        self._fallback_overrides: dict[tuple[str, str], str] = {}
        self._prune_counter: int = 0
        self._init_tables()

    @staticmethod
    def _detect_db_path(store: Any) -> str | None:
        backend = getattr(store, "_backend", None)
        db_path = getattr(backend, "_db_path", None)
        return str(db_path) if db_path else None

    def _conn(self):
        if not self._db_path:
            return None
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_tables(self) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            with conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS memory_recalls (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL DEFAULT '',
                        memory_name TEXT NOT NULL,
                        source TEXT NOT NULL,
                        score REAL NOT NULL DEFAULT 0,
                        reason TEXT NOT NULL DEFAULT '',
                        confidence REAL NOT NULL DEFAULT 0,
                        scope TEXT NOT NULL DEFAULT '',
                        injected INTEGER NOT NULL DEFAULT 0,
                        omitted_reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        type TEXT NOT NULL DEFAULT '',
                        override TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_recalls_session
                        ON memory_recalls(session_id, created_at);
                    CREATE TABLE IF NOT EXISTS memory_session_overrides (
                        session_id TEXT NOT NULL,
                        memory_name TEXT NOT NULL,
                        action TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(session_id, memory_name)
                    );
                """)
        except Exception:
            logger.exception("Failed to initialize memory recall tables")

    def recall(self, query: MemoryRecallQuery, *, record: bool = True) -> MemoryRecallResult:
        try:
            overrides = self.get_overrides(query.session_id)
            candidates = self._collect_candidates(query, overrides)
            records, injected = self._select_for_injection(query, candidates, overrides)
            text = self._format_injection(injected)
            result = MemoryRecallResult(
                session_id=query.session_id,
                injection_text=text,
                records=records,
                total_candidates=len(candidates),
                created_at=_now(),
            )
            if record:
                self.record_result(result)
                if self._event_callback is not None:
                    try:
                        self._event_callback(query.session_id, result)
                    except Exception:
                        logger.debug("Memory recall event callback failed", exc_info=True)
            return result
        except Exception as exc:
            logger.warning("Memory recall failed: %s", exc)
            return MemoryRecallResult(query.session_id, "", [], 0, _now())

    def _collect_candidates(
        self,
        query: MemoryRecallQuery,
        overrides: dict[str, str],
    ) -> dict[str, tuple[Memory, str, float, str]]:
        candidates: dict[str, tuple[Memory, str, float, str]] = {}

        def add(mem: Memory | None, source: str, score: float, reason: str) -> None:
            if mem is None:
                return
            status = getattr(mem.metadata, "status", MemoryStatus.ACTIVE)
            if status is not MemoryStatus.ACTIVE and str(status) != "active":
                return
            confidence = float(getattr(mem.metadata, "confidence", 0.0))
            if confidence < self._min_confidence and overrides.get(mem.name) != "pin":
                return
            prev = candidates.get(mem.name)
            if prev is None or score > prev[2]:
                candidates[mem.name] = (mem, source, score, reason)

        # Manual pins win even if the memory would not otherwise rank.
        for name, action in overrides.items():
            if action == "pin":
                add(self._store.read_memory(name), "pinned", 1.0, "Pinned for this session")

        for summary in self._store.list_memories():
            mem = self._store.read_memory(summary.name)
            if mem is None:
                continue
            t = str(getattr(mem.metadata, "type", ""))
            if hasattr(mem.metadata.type, "value"):
                t = mem.metadata.type.value
            if t in {"user", "feedback"}:
                add(mem, "always", 0.95, "User/feedback memory is always eligible")

        # Deterministic scoped recall: survives without embeddings.
        for scope in ("project", "global", "session"):
            try:
                memories = self._store.list_by_scope(scope, min_confidence=self._min_confidence)
            except Exception:
                memories = []
            for mem in memories:
                score, reason = self._deterministic_score(mem, query)
                if score > 0 or scope in {"global", "project"}:
                    add(mem, "scoped", score, reason or f"{scope} scoped memory")

        # Semantic recall, if available.  External results are resolved back to
        # full Memory objects so injection obeys status/confidence/overrides.
        if self._retriever is not None:
            chunks = self._retriever.retrieve(query.user_message, query.task_description)
            for chunk in chunks:
                name = str(chunk.get("source_name") or chunk.get("name") or "")
                mem = self._store.read_memory(name) if name else None
                score = float(chunk.get("score", 0.0) or 0.0)
                add(mem, "semantic", score, "Semantic match for current prompt")

        return candidates

    def _deterministic_score(self, mem: Memory, query: MemoryRecallQuery) -> tuple[float, str]:
        haystack = " ".join([
            mem.name,
            mem.description,
            mem.content[:1000],
            " ".join(a.path or "" for a in mem.anchors),
        ]).lower()
        needles = [
            query.user_message,
            query.task_description,
            query.session_title,
            " ".join(query.active_files),
            " ".join(query.recent_tools),
        ]
        tokens: set[str] = set()
        for raw in needles:
            for token in raw.lower().replace("/", " ").replace("\\", " ").split():
                token = token.strip(".,:;()[]{}\"'")
                if len(token) >= 4:
                    tokens.add(token)
        matches = [t for t in tokens if t in haystack]
        confidence = float(getattr(mem.metadata, "confidence", 0.5))
        importance = float(getattr(mem.metadata, "importance", 0.5))
        relevance = min(1.0, len(matches) / 5.0)
        freshness = self._freshness(mem.updated_at)
        score = (
            relevance * 0.45
            + importance * 0.25
            + confidence * 0.20
            + freshness * 0.10
        )
        relation_boost = self._relation_boost(mem.name, tokens)
        score = min(1.0, score + relation_boost)
        if matches:
            return score, f"Matched terms: {', '.join(matches[:5])}"
        return score, "Importance, confidence, and freshness score"

    @staticmethod
    def _freshness(updated_at: str) -> float:
        if not updated_at:
            return 0.5
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds() / 86400)
            return max(0.0, 1.0 - min(age_days, 365.0) / 365.0)
        except (TypeError, ValueError):
            return 0.5

    def _relation_boost(self, name: str, query_tokens: set[str]) -> float:
        try:
            edges = self._store.list_edges(name)
        except Exception:
            return 0.0
        boost = 0.0
        for edge in edges:
            related = (
                edge.get("target_name")
                if edge.get("source_name") == name
                else edge.get("source_name")
            )
            if related and any(token in str(related).lower() for token in query_tokens):
                boost += 0.03 * float(edge.get("confidence", 0.5))
        return min(0.10, boost)

    def _select_for_injection(
        self,
        query: MemoryRecallQuery,
        candidates: dict[str, tuple[Memory, str, float, str]],
        overrides: dict[str, str],
    ) -> tuple[list[MemoryRecallRecord], list[tuple[Memory, MemoryRecallRecord]]]:
        rows = sorted(
            candidates.values(),
            key=lambda item: (
                1 if overrides.get(item[0].name) == "pin" else 0,
                item[2],
                float(getattr(item[0].metadata, "confidence", 0.0)),
            ),
            reverse=True,
        )
        records: list[MemoryRecallRecord] = []
        injected: list[tuple[Memory, MemoryRecallRecord]] = []
        used_tokens = 0
        max_items = min(query.top_k or self._max_injected, self._max_injected)
        for mem, source, score, reason in rows:
            action = overrides.get(mem.name, "")
            omitted = ""
            should_inject = True
            if action == "disable":
                should_inject = False
                omitted = "disabled_for_session"
            elif len(injected) >= max_items:
                should_inject = False
                omitted = "item_budget"
            else:
                content_tokens = self._estimator.count(mem.content)
                overhead = self._estimator.header_overhead(len(injected) + 1)
                if used_tokens + content_tokens + overhead > self._max_tokens:
                    should_inject = False
                    omitted = "token_budget"

            if should_inject:
                used_tokens += self._estimator.count(mem.content)

            meta = mem.metadata
            rec = MemoryRecallRecord(
                session_id=query.session_id,
                turn_id=query.turn_id,
                memory_name=mem.name,
                source=source,
                score=round(float(score), 3),
                reason=reason,
                confidence=round(float(getattr(meta, "confidence", 0.0)), 3),
                scope=getattr(meta.scope, "value", str(meta.scope)),
                injected=should_inject,
                omitted_reason=omitted,
                created_at=_now(),
                description=mem.description,
                type=getattr(meta.type, "value", str(meta.type)),
                override=action,
            )
            records.append(rec)
            if should_inject:
                injected.append((mem, rec))
        return records, injected

    def _format_injection(self, injected: list[tuple[Memory, MemoryRecallRecord]]) -> str:
        if not injected:
            return ""
        lines = ["## Active Memory Recall", "The following memories were selected for this session/turn:", ""]
        for mem, rec in injected:
            lines.append(f"### {mem.name} ({rec.source}, score {rec.score:.2f})")
            lines.append(f"> {rec.reason}")
            lines.append(mem.content.strip())
            lines.append("")
            try:
                self._store.record_access(mem.name)
            except Exception:
                pass
        return "\n".join(lines).strip()

    def record_result(self, result: MemoryRecallResult) -> None:
        conn = self._conn()
        if conn is None:
            self._fallback_recalls.extend(result.records)
            self._fallback_recalls = self._fallback_recalls[-500:]
            return
        try:
            with conn:
                for rec in result.records:
                    conn.execute(
                        """INSERT INTO memory_recalls
                           (id, session_id, turn_id, memory_name, source, score, reason,
                            confidence, scope, injected, omitted_reason, created_at,
                            description, type, override)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(uuid4()), rec.session_id, rec.turn_id, rec.memory_name,
                         rec.source, rec.score, rec.reason, rec.confidence, rec.scope,
                         1 if rec.injected else 0, rec.omitted_reason, rec.created_at,
                         rec.description, rec.type, rec.override),
                    )
        except Exception:
            logger.exception("Failed to record memory recalls")
        self._prune_counter += 1
        if self._prune_counter % 20 == 0:
            self.prune_old_recalls()

    def prune_old_recalls(self, retention_days: int | None = None) -> dict[str, Any]:
        """Delete recall records older than retention_days. Runs periodically."""
        days = retention_days or self._recall_retention_days
        conn = self._conn()
        if conn is None:
            return {"pruned": 0, "retention_days": days}
        try:
            with conn:
                cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
                result = conn.execute(
                    "DELETE FROM memory_recalls WHERE created_at < ?",
                    (cutoff_date,),
                )
                # Do NOT prune session overrides here — their lifecycle is
                # tied to the session, not to a fixed retention window.
                # Overrides are tiny (per-session pin/disable) and pruning
                # them would silently break long-lived sessions.
                pruned = result.rowcount
                if pruned:
                    logger.debug("Pruned %d old recall records (retention=%dd)", pruned, days)
                return {"pruned": pruned, "retention_days": days}
        except Exception:
            logger.exception("Failed to prune old recall records")
            return {"pruned": 0, "retention_days": days}

    def list_recalls(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._conn()
        if conn is None:
            return [r.to_dict() for r in self._fallback_recalls if r.session_id == session_id][-limit:]
        try:
            with conn:
                rows = conn.execute(
                    """SELECT * FROM memory_recalls WHERE session_id=?
                       ORDER BY created_at DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()
                return [dict(r) | {"injected": bool(r["injected"])} for r in rows]
        except Exception:
            return []

    def list_generated(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._conn()
        if conn is None:
            return []
        try:
            with conn:
                rows = conn.execute(
                    """SELECT name, description, type, status, scope, confidence,
                              access_count, source, source_session_id, source_run_id, created_at, updated_at
                       FROM memory_entries WHERE source_session_id=?
                       ORDER BY created_at DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def set_override(self, session_id: str, memory_name: str, action: str) -> dict[str, Any]:
        normalized = {"pin": "pin", "disable": "disable", "unpin": "", "enable": ""}.get(action, "")
        conn = self._conn()
        if conn is None:
            key = (session_id, memory_name)
            if normalized:
                self._fallback_overrides[key] = normalized
            else:
                self._fallback_overrides.pop(key, None)
            return {"session_id": session_id, "memory_name": memory_name, "action": normalized}
        try:
            with conn:
                if normalized:
                    conn.execute(
                        """INSERT OR REPLACE INTO memory_session_overrides
                           (session_id, memory_name, action, created_at) VALUES (?, ?, ?, ?)""",
                        (session_id, memory_name, normalized, _now()),
                    )
                else:
                    conn.execute(
                        "DELETE FROM memory_session_overrides WHERE session_id=? AND memory_name=?",
                        (session_id, memory_name),
                    )
        except Exception:
            logger.exception("Failed to set memory override")
        return {"session_id": session_id, "memory_name": memory_name, "action": normalized}

    def get_overrides(self, session_id: str) -> dict[str, str]:
        conn = self._conn()
        if conn is None:
            return {name: action for (sid, name), action in self._fallback_overrides.items() if sid == session_id}
        try:
            with conn:
                rows = conn.execute(
                    "SELECT memory_name, action FROM memory_session_overrides WHERE session_id=?",
                    (session_id,),
                ).fetchall()
                return {r["memory_name"]: r["action"] for r in rows}
        except Exception:
            return {}
