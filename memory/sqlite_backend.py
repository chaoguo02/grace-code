"""
SqliteMemoryBackend — SQLite-backed memory storage.

Stores memories in the same sessions.db as session data.
Tables: memory_entries, memory_anchors (created by SqliteStorageBackend._init_memory_tables).
"""

from __future__ import annotations

import logging
import hashlib
import json
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
        self._last_write_result: dict[str, Any] = {}
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
                        importance=float(r["importance"]),
                        access_count=int(r["access_count"]),
                    ),
                )
            if r["a_kind"]:
                mem_map[name].anchors.append(Anchor(
                    kind=r["a_kind"], path=r["a_path"] or "",
                    name=r["a_name"], value=r["a_value"],
                    content_hash=r["a_hash"] or "",
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
                        importance REAL NOT NULL DEFAULT 0.5,
                        current_revision INTEGER NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '', source_session_id TEXT NOT NULL DEFAULT '',
                        source_run_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        expires_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS memory_anchors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, memory_name TEXT NOT NULL,
                        kind TEXT NOT NULL, path TEXT, symbol_name TEXT, task_value TEXT, content_hash TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_mem_type ON memory_entries(type);
                    CREATE INDEX IF NOT EXISTS idx_mem_scope ON memory_entries(scope);
                    CREATE TABLE IF NOT EXISTS memory_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_name TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT '',
                        source_session_id TEXT NOT NULL DEFAULT '',
                        source_run_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        UNIQUE(memory_name, revision),
                        UNIQUE(memory_name, content_hash)
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_revisions_name
                        ON memory_revisions(memory_name, revision DESC);
                    CREATE TABLE IF NOT EXISTS memory_edges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_name TEXT NOT NULL,
                        target_name TEXT NOT NULL,
                        relation_type TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        evidence TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(source_name, target_name, relation_type, evidence)
                    );
                    CREATE INDEX IF NOT EXISTS idx_memory_edges_source
                        ON memory_edges(source_name);
                """)
                # Migration P1-34a: add expires_at to existing databases
                try:
                    conn.execute(
                        "ALTER TABLE memory_entries ADD COLUMN expires_at TEXT"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
                for declaration in (
                    "importance REAL NOT NULL DEFAULT 0.5",
                    "current_revision INTEGER NOT NULL DEFAULT 0",
                ):
                    try:
                        conn.execute(f"ALTER TABLE memory_entries ADD COLUMN {declaration}")
                    except sqlite3.OperationalError:
                        pass
                self._backfill_revisions(conn)
                # Migration: add source_run_id for turn-level traceability
                try:
                    conn.execute(
                        "ALTER TABLE memory_entries ADD COLUMN source_run_id TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
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

    @property
    def last_write_result(self) -> dict[str, Any]:
        return dict(self._last_write_result)

    @staticmethod
    def _revision_payload(memory: Memory) -> dict[str, Any]:
        meta = memory.metadata
        return {
            "name": memory.name,
            "description": memory.description,
            "content": memory.content,
            "metadata": {
                "type": SqliteMemoryBackend._val(meta.type),
                "status": SqliteMemoryBackend._val(meta.status),
                "scope": SqliteMemoryBackend._val(meta.scope),
                "confidence": meta.confidence,
                "importance": meta.importance,
                "ttl_seconds": meta.ttl_seconds,
                "expires_at": meta.expires_at,
                "access_count": meta.access_count,
                "validated_at": meta.validated_at,
            },
            "anchors": [
                {
                    "kind": a.kind, "path": a.path, "name": a.name,
                    "value": a.value, "content_hash": a.content_hash,
                }
                for a in memory.anchors
            ],
        }

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _backfill_revisions(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT * FROM memory_entries WHERE current_revision=0"
        ).fetchall()
        for row in rows:
            payload = {
                "name": row["name"], "description": row["description"],
                "content": row["content"],
                "metadata": {
                    "type": row["type"], "status": row["status"], "scope": row["scope"],
                    "confidence": row["confidence"], "importance": row["importance"],
                    "access_count": row["access_count"],
                },
                "anchors": [],
            }
            digest = self._payload_hash(payload)
            conn.execute(
                """INSERT OR IGNORE INTO memory_revisions
                   (memory_name, revision, content_hash, payload_json, source,
                    source_session_id, source_run_id, created_at)
                   VALUES (?, 1, ?, ?, ?, ?, ?, ?)""",
                (row["name"], digest, json.dumps(payload, ensure_ascii=False),
                 row["source"], row["source_session_id"], row["source_run_id"],
                 row["created_at"]),
            )
            conn.execute(
                "UPDATE memory_entries SET current_revision=1 WHERE name=?",
                (row["name"],),
            )

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
                        confidence=row["confidence"], importance=row["importance"],
                        access_count=row["access_count"],
                    ),
                    created_at=row["created_at"], updated_at=row["updated_at"], anchors=anchors,
                )
        except Exception as exc:
            logger.warning("SQLite read_memory %s failed: %s", name, exc)
            return None

    def write_memory(self, memory: Memory, source: str = "", source_session_id: str = "", source_run_id: str = "") -> bool:
        now = datetime.now(timezone.utc).isoformat()
        _t = self._val(memory.metadata.type)
        _s = self._val(memory.metadata.status)
        _sc = self._val(memory.metadata.scope)
        payload = self._revision_payload(memory)
        content_hash = self._payload_hash(payload)
        try:
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                previous = conn.execute(
                    """SELECT id, revision FROM memory_revisions
                       WHERE memory_name=? AND content_hash=?""",
                    (memory.name, content_hash),
                ).fetchone()
                if previous is not None:
                    conn.execute("COMMIT")
                    self._last_write_result = {
                        "action": "NOOP", "revision_id": previous["id"],
                        "revision": previous["revision"], "content_hash": content_hash,
                    }
                    return True
                current = conn.execute(
                    "SELECT current_revision FROM memory_entries WHERE name=?",
                    (memory.name,),
                ).fetchone()
                revision = int(current["current_revision"] if current else 0) + 1
                cur = conn.execute(
                    """INSERT INTO memory_revisions
                       (memory_name, revision, content_hash, payload_json, source,
                        source_session_id, source_run_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (memory.name, revision, content_hash,
                     json.dumps(payload, ensure_ascii=False), source,
                     source_session_id, source_run_id, now),
                )
                conn.execute(
                    """INSERT INTO memory_entries
                       (name, description, content, type, status, scope, confidence,
                        importance, access_count, source, source_session_id, source_run_id,
                        created_at, updated_at, current_revision)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                         description=excluded.description, content=excluded.content,
                         type=excluded.type, status=excluded.status, scope=excluded.scope,
                         confidence=excluded.confidence, importance=excluded.importance,
                         access_count=excluded.access_count, source=excluded.source,
                         source_session_id=excluded.source_session_id,
                         source_run_id=excluded.source_run_id, updated_at=excluded.updated_at,
                         current_revision=excluded.current_revision""",
                    (memory.name, memory.description, memory.content,
                     _t, _s, _sc, memory.metadata.confidence, memory.metadata.importance,
                     memory.metadata.access_count, source, source_session_id,
                     source_run_id, now, now, revision),
                )
                conn.execute("DELETE FROM memory_anchors WHERE memory_name=?", (memory.name,))
                for a in memory.anchors:
                    conn.execute(
                        "INSERT INTO memory_anchors (memory_name, kind, path, symbol_name, task_value, content_hash) VALUES (?,?,?,?,?,?)",
                        (memory.name, a.kind, a.path, a.name, a.value, a.content_hash),
                    )
                conn.execute("COMMIT")
                self._last_write_result = {
                    "action": "NEW" if current is None else "REVISION",
                    "revision_id": cur.lastrowid, "revision": revision,
                    "content_hash": content_hash,
                }
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

    def list_revisions(self, name: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, memory_name, revision, content_hash, payload_json,
                          source, source_session_id, source_run_id, created_at
                   FROM memory_revisions WHERE memory_name=?
                   ORDER BY revision DESC""",
                (name,),
            ).fetchall()
        return [
            dict(row) | {"payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    def list_edges(self, name: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM memory_edges
                   WHERE source_name=? OR target_name=? ORDER BY id""",
                (name, name),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_edge(
        self, source_name: str, target_name: str, relation_type: str,
        confidence: float, evidence: str,
    ) -> dict[str, Any]:
        allowed = {"related_to", "depends_on", "contradicts", "supersedes", "mentions"}
        if relation_type not in allowed:
            raise ValueError(f"Unsupported memory relation: {relation_type}")
        if not evidence.strip():
            raise ValueError("Memory relation evidence is required")
        if source_name == target_name:
            raise ValueError("A memory cannot relate to itself")
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            missing = [
                item for item in (source_name, target_name)
                if conn.execute("SELECT 1 FROM memory_entries WHERE name=?", (item,)).fetchone() is None
            ]
            if missing:
                raise ValueError(f"Unknown memory: {', '.join(missing)}")
            conn.execute(
                """INSERT INTO memory_edges
                   (source_name, target_name, relation_type, confidence, evidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_name, target_name, relation_type, evidence)
                   DO UPDATE SET confidence=excluded.confidence""",
                (source_name, target_name, relation_type,
                 max(0.0, min(1.0, float(confidence))), evidence.strip(), now),
            )
        return {
            "source": source_name, "target": target_name,
            "relation_type": relation_type,
            "confidence": max(0.0, min(1.0, float(confidence))),
            "evidence": evidence.strip(),
        }

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

    def prune_expired_ttl(self) -> int:
        """Deprecate memories whose TTL has expired.

        Checks ``expires_at`` against current UTC time. Expired memories
        are set to ``status='deprecated'``. Returns number of rows changed.
        """
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    """UPDATE memory_entries SET status='deprecated'
                       WHERE status='active'
                       AND expires_at IS NOT NULL
                       AND expires_at != ''
                       AND expires_at < datetime('now')"""
                )
                count = cur.rowcount
                if count:
                    logger.info("TTL-expired %d memories → deprecated", count)
                return count
        except sqlite3.OperationalError:
            logger.debug("prune_expired_ttl skipped — expires_at column not available")
            return 0
        except Exception:
            logger.exception("Failed to prune expired TTL")
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
