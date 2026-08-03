"""
PlanRevisionService — independent storage for plan revisions.

Revisions are stored in a dedicated table outside session metadata
to avoid field bloat, concurrent overwrites, and size concerns.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PlanRevision:
    id: str = ""                # auto-generated: {session_id}_{revision}
    session_id: str = ""
    revision: int = 0
    content: str = ""           # full plan text
    content_hash: str = ""      # SHA256 for dedup
    parent_revision: int = 0    # 0 = original
    change_request: str = ""    # rejection reason / feedback
    created_at: str = ""
    status: str = "pending"     # pending | approved | rejected | superseded

    @classmethod
    def create(cls, session_id: str, revision: int, content: str,
               parent_revision: int = 0, change_request: str = "") -> "PlanRevision":
        return cls(
            id=f"{session_id}_{revision}",
            session_id=session_id,
            revision=revision,
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest()[:16],
            parent_revision=parent_revision,
            change_request=change_request,
            created_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "revision": self.revision,
            "content": self.content,
            "content_hash": self.content_hash,
            "parent_revision": self.parent_revision,
            "change_request": self.change_request,
            "created_at": self.created_at,
            "status": self.status,
        }


class PlanRevisionService:
    """Manage plan revisions with SQLite-backed storage.

    Uses the existing SqliteStorageBackend for transactional safety.
    Legacy JSON files under .forge-agent/plan-revisions/ are imported
    on construction and then no longer written.
    """

    def __init__(self, storage: Any, repo_path: str = "") -> None:
        """*storage* is the SqliteStorageBackend instance.
        *repo_path* is needed only for legacy JSON migration.
        """
        self._storage = storage
        self._repo_path = repo_path
        if repo_path:
            self._migrate_json_if_needed()

    def _migrate_json_if_needed(self) -> None:
        """One-time import of legacy JSON plan revisions into SQLite."""
        import json as _json
        from pathlib import Path as _Path
        legacy_dir = _Path(self._repo_path) / ".forge-agent" / "plan-revisions"
        if not legacy_dir.is_dir():
            return
        migrated_file = legacy_dir / ".migrated"
        migrated: set[str] = set()
        if migrated_file.is_file():
            migrated = set(migrated_file.read_text(encoding="utf-8").splitlines())

        imported = 0
        for json_file in sorted(legacy_dir.glob("*.json")):
            session_id = json_file.stem
            if session_id in migrated or session_id.startswith("."):
                continue
            try:
                revisions = _json.loads(json_file.read_text(encoding="utf-8"))
                if not isinstance(revisions, list):
                    continue
                for rev in revisions:
                    if not isinstance(rev, dict) or "id" not in rev:
                        continue
                    try:
                        self._storage.insert_plan_revision(rev)
                        imported += 1
                    except Exception:
                        pass
                migrated.add(session_id)
            except (_json.JSONDecodeError, OSError):
                logger.debug("Failed to migrate plan revisions from %s", json_file)

        if imported:
            migrated_file.write_text("\n".join(sorted(migrated)), encoding="utf-8")
            logger.info("Migrated %d plan revisions from JSON to SQLite", imported)

    def append_revision(self, session_id: str, content: str,
                        parent_revision: int = 0,
                        change_request: str = "") -> PlanRevision:
        """Create a new plan revision and persist it to SQLite."""
        existing = self._storage.list_plan_revisions(session_id)
        rev_num = len(existing) + 1
        rev = PlanRevision.create(
            session_id, rev_num, content,
            parent_revision=parent_revision,
            change_request=change_request,
        )
        self._storage.insert_plan_revision(rev.to_dict())
        logger.info("Plan revision %d saved for session %s", rev_num, session_id[:8])
        return rev

    def mark_status(self, session_id: str, revision: int, status: str) -> bool:
        """Update a revision's status."""
        return self._storage.update_plan_revision_status(session_id, revision, status)

    def list_revisions(self, session_id: str) -> list[dict]:
        """Return all revisions for a session, oldest first."""
        return self._storage.list_plan_revisions(session_id)

    def get_revision(self, session_id: str, revision: int) -> dict | None:
        """Get a specific revision by number."""
        return self._storage.get_plan_revision(session_id, revision)

    def get_latest(self, session_id: str) -> dict | None:
        revisions = self._storage.list_plan_revisions(session_id)
        return revisions[-1] if revisions else None

    def compute_diff(self, session_id: str, from_rev: int, to_rev: int) -> dict:
        """Compute a simple line-level diff between two revisions."""
        from_r = self.get_revision(session_id, from_rev)
        to_r = self.get_revision(session_id, to_rev)
        if not from_r or not to_r:
            return {"error": "Revision not found"}

        import difflib
        from_lines = from_r["content"].splitlines(keepends=True)
        to_lines = to_r["content"].splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            from_lines, to_lines,
            fromfile=f"revision {from_rev}",
            tofile=f"revision {to_rev}",
        ))
        return {
            "from_revision": from_rev,
            "to_revision": to_rev,
            "diff": "".join(diff),
            "added_lines": sum(1 for l in diff if l.startswith("+") and not l.startswith("+++")),
            "removed_lines": sum(1 for l in diff if l.startswith("-") and not l.startswith("---")),
        }
