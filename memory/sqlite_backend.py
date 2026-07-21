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

    # Indexer error state for observability (P0-6).
    # When not None, the last indexer operation failed with this message.
    _last_index_error: str | None = None
    _index_error_count: int = 0

    def __init__(self, db_path: str, indexer: Any | None = None) -> None:
        self._db_path = db_path
        self._indexer = indexer
        self._last_index_error: str | None = None
        self._index_error_count: int = 0
        self._init_tables()

    @staticmethod
    def _rows_to_memories(rows: list) -> list[Memory]:
        """Convert JOIN query rows to Memory objects (P2-43)."""
        from memory.models import Anchor, Memory, MemoryMetadata, MemoryScope, MemoryStatus, MemoryType
        mem_map: dict[str, Memory] = {}
        for r in rows:
            name = r["name"]
            if name not in mem_map:
                mem_map[name] = Memory(
                    name=name,
                    description=r["description"],
                    content=r["content"],
                    metadata=MemoryMetadata(
                        type=MemoryType(r["type"]),
                        status=MemoryStatus(r["status"]),
                        scope=MemoryScope(r["scope"]),
                        confidence=float(r["confidence"]),
                        access_count=int(r["access_count"]),
                    ),
                )
            if r.get("a_kind"):
                mem_map[name].anchors.append(Anchor(
                    kind=r["a_kind"], path=r.get("a_path") or "",
                    name=r.get("a_name"), value=r.get("a_value"),
                    content_hash=r.get("a_hash") or "",
                ))
        return list(mem_map.values())

    def _init_tables(self) -> None:
        """Ensure memory tables exist (idempotent). Called once at init."""
        try:
            with self._conn() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS memory_entries (
                        name TEXT PRIMARY KEY, description TEXT NOT NULL,
                        content TEXT NOT NULL DEFAULT '', type TEXT NOT NULL DEFAULT 'project',
                        status TEXT NOT NULL DEFAULT 'active', scope TEXT NOT NULL DEFAULT 'project',
                        confidence REAL NOT NULL DEFAULT 0.7, access_count INTEGER NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '', source_session_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS memory_anchors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, memory_name TEXT NOT NULL,
                        kind TEXT NOT NULL, path TEXT, symbol_name TEXT, task_value TEXT, content_hash TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_mem_type ON memory_entries(type);
                    CREATE INDEX IF NOT EXISTS idx_mem_scope ON memory_entries(scope);
                """)
        except Exception:
            logger.exception("Failed to create memory tables")

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
                    created_at=row["created_at"], updated_at=row["updated_at"], anchors=anchors,
                )
        except Exception as exc:
            logger.warning("SQLite read_memory %s failed: %s", name, exc)
            return None

    def write_memory(self, memory: Memory, source: str = "", source_session_id: str = "") -> bool:
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
                     source, source_session_id, memory.name, now, now),
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
            try:
                self._indexer.index_memory(memory)
                self._last_index_error = None
            except Exception as exc:
                self._last_index_error = str(exc)[:200]
                self._index_error_count += 1
                logger.warning(
                    "Semantic indexer failed to index memory '%s' (error #%d): %s",
                    memory.name, self._index_error_count, exc,
                )
        return True

    def delete_memory(self, name: str) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM memory_anchors WHERE memory_name=?", (name,))
                conn.execute("DELETE FROM memory_entries WHERE name=?", (name,))
                conn.execute("COMMIT")
            if self._indexer is not None:
                try:
                    self._indexer.remove_memory(name)
                except Exception as exc:
                    self._last_index_error = str(exc)[:200]
                    self._index_error_count += 1
                    logger.warning(
                        "Semantic indexer failed to remove memory '%s' (error #%d): %s",
                        name, self._index_error_count, exc,
                    )
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
        """List memories by scope in a single connection (P2-43)."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT m.*, a.kind AS a_kind, a.path AS a_path,
                              a.symbol_name AS a_name, a.task_value AS a_value,
                              a.content_hash AS a_hash
                       FROM memory_entries m
                       LEFT JOIN memory_anchors a ON a.memory_name = m.name
                       WHERE m.scope=? AND m.confidence>=?
                       ORDER BY m.confidence DESC""",
                    (scope, min_confidence),
                ).fetchall()
                return self._rows_to_memories(rows)
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

    def decay_confidences(self) -> int:
        """Decay confidence for low-access memories. Returns number updated."""
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    """UPDATE memory_entries SET confidence = MAX(0.1, confidence * 0.9)
                       WHERE access_count < 3
                       AND updated_at < datetime('now', '-90 days')
                       AND status='active'"""
                )
                decayed = cur.rowcount
                cur2 = conn.execute(
                    "UPDATE memory_entries SET status='deprecated' WHERE confidence < 0.2 AND status='active'"
                )
                deprecated = cur2.rowcount
                if decayed or deprecated:
                    logger.info("Decayed %d, auto-deprecated %d memories", decayed, deprecated)
                return decayed + deprecated
        except Exception:
            logger.exception("Failed to decay confidences")
            return 0

    def get_stats(self) -> dict:
        """Return aggregate stats using SQL COUNT queries with real TTL tracking."""
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            seven_days = timedelta(days=7)

            with self._conn() as conn:
                total = conn.execute("SELECT COUNT(*) AS c FROM memory_entries").fetchone()["c"]
                active = conn.execute("SELECT COUNT(*) AS c FROM memory_entries WHERE status='active'").fetchone()["c"]
                deprecated = conn.execute("SELECT COUNT(*) AS c FROM memory_entries WHERE status='deprecated'").fetchone()["c"]
                archived = deprecated  # deprecated memories are effectively archived

                # Real TTL: count active memories expiring within 7 days
                expiring = 0
                try:
                    ttl_rows = conn.execute(
                        "SELECT expires_at FROM memory_entries WHERE status='active' AND expires_at IS NOT NULL AND expires_at != ''"
                    ).fetchall()
                    for row in ttl_rows:
                        try:
                            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                            if now < expires < now + seven_days:
                                expiring += 1
                        except (ValueError, TypeError):
                            pass
                except Exception:
                    pass

                by_type = {r["type"]: r["cnt"] for r in conn.execute(
                    "SELECT type, COUNT(*) AS cnt FROM memory_entries GROUP BY type"
                ).fetchall()}
                by_scope = {r["scope"]: r["cnt"] for r in conn.execute(
                    "SELECT scope, COUNT(*) AS cnt FROM memory_entries GROUP BY scope"
                ).fetchall()}

                # Layer: active global-scope = global layer, active project = project layer, deprecated = archive
                global_active = conn.execute(
                    "SELECT COUNT(*) AS c FROM memory_entries WHERE status='active' AND scope='global'"
                ).fetchone()["c"]
                project_active = conn.execute(
                    "SELECT COUNT(*) AS c FROM memory_entries WHERE status='active' AND scope IN ('project','session')"
                ).fetchone()["c"]

                return {
                    "total": total, "active": active, "deprecated": deprecated,
                    "archived": archived, "expiring": expiring,
                    "by_type": by_type, "by_scope": by_scope,
                    "by_layer": {"project": project_active, "global": global_active, "archive": deprecated},
                }
        except Exception:
            return {"total": 0, "active": 0, "deprecated": 0, "archived": 0, "expiring": 0,
                    "by_type": {}, "by_scope": {}, "by_layer": {}}

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
