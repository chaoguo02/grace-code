"""
SqliteMemoryBackend — SQLite-backed memory storage.

Stores memories in the same sessions.db as session data.
Tables: memory_entries, memory_anchors (created by SqliteStorageBackend._init_memory_tables).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from memory.models import Anchor, Memory, MemoryMetadata, MemoryScope, MemoryStatus, MemorySummary, MemoryType

logger = logging.getLogger(__name__)


class SqliteMemoryBackend:
    """SQLite-backed memory backend. Memories in memory_entries + memory_anchors tables."""

    def __init__(self, db_path: str, indexer: Any | None = None) -> None:
        self._db_path = db_path
        self._indexer = indexer

    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _val(val):
        """Extract string value from enum or plain string."""
        return val.value if hasattr(val, 'value') else str(val) if val else ""

    # ── CRUD ────────────────────────────────────────────────────────────

    def read_memory(self, name: str) -> Memory | None:
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM memory_entries WHERE name=?", (name,)).fetchone()
                if row is None:
                    return None
                anchors = []
                for a in conn.execute("SELECT * FROM memory_anchors WHERE memory_name=?", (name,)).fetchall():
                    anchor = Anchor(kind=a["kind"])
                    if a["path"]: anchor.path = a["path"]
                    if a["symbol_name"]: anchor.name = a["symbol_name"]
                    if a["task_value"]: anchor.value = a["task_value"]
                    if a["content_hash"]: anchor.content_hash = a["content_hash"]
                    anchors.append(anchor)
                return Memory(
                    name=row["name"], description=row["description"], content=row["content"],
                    metadata=MemoryMetadata(
                        type=MemoryType(row["type"]) if row["type"] in ("user","feedback","project","reference") else MemoryType.PROJECT,
                        status=MemoryStatus(row["status"]) if row["status"] in ("active","deprecated") else MemoryStatus.ACTIVE,
                        scope=MemoryScope(row["scope"]) if row["scope"] in ("session","project","global") else MemoryScope.PROJECT,
                        confidence=row["confidence"], access_count=row["access_count"],
                    ),
                    updated_at=row["updated_at"], anchors=anchors,
                )
        except Exception as exc:
            logger.warning("SQLite read_memory %s failed: %s", name, exc)
            return None

    def write_memory(self, memory: Memory, source: str = "") -> bool:
        now = datetime.now(timezone.utc).isoformat()
        _t = self._val(memory.metadata.type)
        _s = self._val(memory.metadata.status)
        _sc = self._val(memory.metadata.scope)
        try:
            with self._conn() as conn:
                conn.execute("BEGIN")
                conn.execute(
                    """INSERT OR REPLACE INTO memory_entries
                       (name, description, content, type, status, scope, confidence,
                        access_count, source, source_session_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               COALESCE((SELECT created_at FROM memory_entries WHERE name=?), ?), ?)""",
                    (memory.name, memory.description, memory.content,
                     _t, _s, _sc, memory.metadata.confidence, memory.metadata.access_count,
                     source, "", memory.name, now, now),
                )
                conn.execute("DELETE FROM memory_anchors WHERE memory_name=?", (memory.name,))
                for a in memory.anchors:
                    conn.execute(
                        "INSERT INTO memory_anchors (memory_name, kind, path, symbol_name, task_value, content_hash) VALUES (?,?,?,?,?,?)",
                        (memory.name, a.kind, a.path, a.name, a.value, a.content_hash),
                    )
                conn.execute("COMMIT")
        except Exception as exc:
            logger.error("SQLite write_memory %s failed: %s", memory.name, exc)
            return False
        if self._indexer is not None:
            try: self._indexer.index_memory(memory)
            except Exception: pass
        return True

    def delete_memory(self, name: str) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM memory_anchors WHERE memory_name=?", (name,))
                conn.execute("DELETE FROM memory_entries WHERE name=?", (name,))
                conn.execute("COMMIT")
            if self._indexer is not None:
                try: self._indexer.remove_memory(name)
                except Exception: pass
            return True
        except Exception as exc:
            logger.error("SQLite delete_memory %s failed: %s", name, exc)
            return False

    def list_memories(self) -> list[MemorySummary]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT name, description, type, updated_at FROM memory_entries ORDER BY updated_at DESC"
                ).fetchall()
                return [MemorySummary(name=r["name"], description=r["description"], type=r["type"], updated_at=r["updated_at"]) for r in rows]
        except Exception as exc:
            logger.warning("SQLite list_memories failed: %s", exc)
            return []

    def count_by_type(self) -> dict[str, int]:
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT type, COUNT(*) AS cnt FROM memory_entries GROUP BY type").fetchall()
                return {r["type"]: r["cnt"] for r in rows}
        except Exception:
            return {}

    def list_by_scope(self, scope: str = "project", min_confidence: float = 0.0) -> list[Memory]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT name FROM memory_entries WHERE scope=? AND confidence>=? ORDER BY confidence DESC",
                    (scope, min_confidence),
                ).fetchall()
                result = []
                for r in rows:
                    mem = self.read_memory(r["name"])
                    if mem:
                        result.append(mem)
                return result
        except Exception:
            return []

    def record_access(self, name: str) -> bool:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE memory_entries SET access_count = access_count + 1 WHERE name=?", (name,)
                )
                return cur.rowcount > 0
        except Exception:
            return False

    def get_index_content(self, max_lines: int | None = None) -> str:
        try:
            with self._conn() as conn:
                limit = max_lines or 200
                rows = conn.execute(
                    "SELECT name, description, type, updated_at FROM memory_entries ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                lines = ["# Memory Index\n"]
                for r in rows:
                    lines.append(f"- [{r['name']}]({r['name']}.md) -- {r['description']} ({r['type']})\n")
                return "".join(lines)
        except Exception:
            return ""
