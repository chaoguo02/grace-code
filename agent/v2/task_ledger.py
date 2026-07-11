"""Task Ledger — prevents duplicate execution of identical tasks.

Claude Code pattern: the Runtime tracks what has been done. If the same task
arrives again (same description, same repo, same intent), and it was previously
completed successfully, return the cached result instead of re-executing.

This is NOT a cache — it's an idempotency guard. It prevents wasted token
consumption when the user or an automated system re-submits the same task.

Design decisions:
- Fingerprint: SHA256(normalized_description + repo_path + intent)
- Storage: SQLite table `task_ledger` in the sessions.db
- TTL: 24 hours (cached results are valid for 1 day)
- Only successful completions are cached; failures are not
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Results older than this are considered stale
_DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 hours


# ---------------------------------------------------------------------------
# TaskFingerprint
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskFingerprint:
    """Unique identity of a task for deduplication."""

    task_description: str
    repo_path: str
    intent: str
    fingerprint_hash: str

    @classmethod
    def compute(
        cls,
        task_description: str,
        repo_path: str,
        intent: str = "edit",
    ) -> "TaskFingerprint":
        """Compute a fingerprint from task parameters.

        Normalizes the description (strip, lowercase first 200 chars) to
        catch trivial variations while still being specific enough.
        """
        normalized = task_description.strip()[:200].lower()
        key = f"{normalized}|{repo_path}|{intent}"
        fp_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return cls(
            task_description=task_description.strip(),
            repo_path=repo_path,
            intent=intent,
            fingerprint_hash=fp_hash,
        )


# ---------------------------------------------------------------------------
# TaskLedger
# ---------------------------------------------------------------------------

class TaskLedger:
    """SQLite-backed ledger of completed tasks.

    Usage:
        ledger = TaskLedger(db_path)

        fp = TaskFingerprint.compute(description, repo_path, intent)
        if ledger.is_completed(fp):
            result = ledger.get_cached_result(fp)
            return result  # skip execution

        result = agent.run(task, log)
        if result.is_success():
            ledger.mark_completed(fp, result.summary)
    """

    def __init__(self, db_path: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._db_path = db_path
        self._ttl_seconds = ttl_seconds
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create the task_ledger table if it doesn't exist."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_ledger (
                    fingerprint_hash TEXT PRIMARY KEY,
                    task_description TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    intent TEXT NOT NULL DEFAULT 'edit',
                    status TEXT NOT NULL DEFAULT 'completed',
                    summary TEXT NOT NULL DEFAULT '',
                    completed_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_task_ledger_expires
                ON task_ledger(expires_at)
            """)
            conn.commit()

    # ── Public API ──

    def is_completed(self, fingerprint: TaskFingerprint) -> bool:
        """Check if a task with this fingerprint was already completed.

        Automatically evicts expired entries before checking.
        """
        self._evict_expired()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM task_ledger WHERE fingerprint_hash = ? AND expires_at > ?",
                (fingerprint.fingerprint_hash, _time.time()),
            ).fetchone()
            return row is not None

    def get_cached_result(
        self, fingerprint: TaskFingerprint
    ) -> dict[str, Any] | None:
        """Return the cached result for a completed task, or None."""
        self._evict_expired()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """SELECT status, summary, completed_at
                   FROM task_ledger
                   WHERE fingerprint_hash = ? AND expires_at > ?""",
                (fingerprint.fingerprint_hash, _time.time()),
            ).fetchone()
            if row is None:
                return None
            return {
                "status": row[0],
                "summary": row[1],
                "completed_at": row[2],
            }

    def mark_completed(
        self,
        fingerprint: TaskFingerprint,
        summary: str = "",
        status: str = "completed",
    ) -> None:
        """Record a task as completed.

        Only successful completions should be recorded. Failed tasks
        should NOT be cached — they may succeed on retry.
        """
        now = _time.time()
        expires_at = now + self._ttl_seconds
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO task_ledger
                   (fingerprint_hash, task_description, repo_path, intent,
                    status, summary, completed_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fingerprint.fingerprint_hash,
                    fingerprint.task_description,
                    fingerprint.repo_path,
                    fingerprint.intent,
                    status,
                    summary,
                    now,
                    expires_at,
                ),
            )
            conn.commit()
        logger.debug(
            "TaskLedger: recorded %s (expires in %.0fs)",
            fingerprint.fingerprint_hash, self._ttl_seconds,
        )

    def invalidate(self, fingerprint: TaskFingerprint) -> None:
        """Remove a task from the ledger (e.g., after code changes)."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "DELETE FROM task_ledger WHERE fingerprint_hash = ?",
                (fingerprint.fingerprint_hash,),
            )
            conn.commit()

    def invalidate_for_repo(self, repo_path: str) -> None:
        """Invalidate all cached results for a repo (e.g., after major changes)."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "DELETE FROM task_ledger WHERE repo_path = ?",
                (repo_path,),
            )
            conn.commit()
        logger.info("TaskLedger: invalidated all entries for %s", repo_path)

    def _evict_expired(self) -> int:
        """Remove expired entries. Returns count of evicted rows."""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM task_ledger WHERE expires_at <= ?",
                (_time.time(),),
            )
            conn.commit()
            count = cursor.rowcount
            if count:
                logger.debug("TaskLedger: evicted %d expired entries", count)
            return count

    def prune(self) -> int:
        """Alias for _evict_expired. Returns count of pruned rows."""
        return self._evict_expired()

    def count(self) -> int:
        """Return the number of active (non-expired) entries."""
        self._evict_expired()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM task_ledger WHERE expires_at > ?",
                (_time.time(),),
            ).fetchone()
            return row[0] if row else 0
