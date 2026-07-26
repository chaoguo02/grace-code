"""
Stats service — CRUD for execution statistics, diffs, and daily rollups.

Thin wrapper over ``StorageBackend`` stats methods. All data is stored
in the same SQLite database as sessions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.storage.protocol import StorageBackend

logger = logging.getLogger(__name__)


class StatsService:
    """Query/update execution stats and diffs."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    # ── Session stats ────────────────────────────────────────────────────

    def record_session_complete(
        self,
        session_id: str,
        agent_name: str,
        total_steps: int,
        total_tokens: int,
        total_duration_ms: int,
        status: str,
        tool_summary: dict[str, int],
    ) -> None:
        """Write aggregate stats after a session finishes."""
        self._storage.upsert_session_stats(
            session_id,
            agent_name=agent_name,
            total_steps=total_steps,
            total_tokens=total_tokens,
            total_duration_ms=total_duration_ms,
            status=status,
            tool_summary=json.dumps(tool_summary, ensure_ascii=False),
        )
        # Update daily rollup
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._update_daily_rollup(today, status, total_tokens, total_duration_ms, tool_summary)

    def record_step(
        self,
        session_id: str,
        step_number: int,
        tool_name: str,
        tool_params: dict[str, Any],
        status: str,
        duration_ms: int,
        tokens: int,
        timestamp: str,
    ) -> None:
        """Write one step log entry."""
        self._storage.insert_step_log(
            session_id,
            step_number=step_number,
            tool_name=tool_name,
            tool_params=json.dumps(tool_params, ensure_ascii=False),
            status=status,
            duration_ms=duration_ms,
            tokens=tokens,
            timestamp=timestamp,
        )

    def record_diff(
        self,
        session_id: str,
        step_number: int,
        file_path: str,
        diff_content: str,
    ) -> int:
        """Persist a file diff from an Edit/Write operation."""
        return self._storage.insert_session_diff(
            session_id, step_number=step_number, file_path=file_path,
            diff_content=diff_content,
        )

    def get_session_stats(self, session_id: str) -> dict | None:
        """Get aggregate stats for one session.

        JSON fields (tool_summary) are parsed into native Python objects
        so the API contract matches the frontend TypeScript types.
        """
        raw = self._storage.get_session_stats(session_id)
        if raw is None:
            return None
        # Parse JSON fields that are stored as TEXT in SQLite
        for field in ("tool_summary", "status_summary"):
            if field in raw and isinstance(raw[field], str):
                try:
                    raw[field] = json.loads(raw[field])
                except (json.JSONDecodeError, TypeError):
                    raw[field] = {}
        return raw

    def get_session_steps(self, session_id: str) -> list[dict]:
        """Get per-step log for one session."""
        return self._storage.get_session_steps(session_id)

    def record_context_snapshot(
        self,
        session_id: str,
        *,
        run_id: str = "",
        turn_id: str = "",
        step_number: int,
        request_kind: str,
        stats: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> int:
        """Persist the measured context of one provider request."""
        writer = getattr(self._storage, "insert_context_snapshot", None)
        if not callable(writer):
            return 0
        return writer(
            session_id,
            run_id=run_id,
            turn_id=turn_id,
            step_number=step_number,
            request_kind=request_kind,
            stats_json=json.dumps(stats, ensure_ascii=False),
            capabilities_json=json.dumps(capabilities, ensure_ascii=False),
        )

    def get_context_snapshots(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return parsed, ordered context snapshots for the inspector."""
        reader = getattr(self._storage, "get_context_snapshots", None)
        if not callable(reader):
            return []
        snapshots: list[dict[str, Any]] = []
        for raw in reader(session_id, limit=limit):
            item = dict(raw)
            for source, target in (
                ("stats_json", "stats"),
                ("capabilities_json", "capabilities"),
            ):
                value = item.pop(source, "{}")
                try:
                    item[target] = (
                        json.loads(value) if isinstance(value, str) else value
                    )
                except (json.JSONDecodeError, TypeError):
                    item[target] = {}
            snapshots.append(item)
        return snapshots

    # ── Diffs ────────────────────────────────────────────────────────────

    def get_session_diffs(
        self, session_id: str, status: str | None = None,
    ) -> list[dict]:
        return self._storage.get_session_diffs(session_id, status=status)

    def update_diff_status(self, diff_id: int, status: str, comment: str = "") -> bool:
        return self._storage.update_diff_status(diff_id, status, comment)

    # ── Daily rollup ─────────────────────────────────────────────────────

    def get_daily_rollups(self, days: int = 30) -> list[dict]:
        return self._storage.get_daily_rollups(days=days)

    def _update_daily_rollup(
        self, date: str, status: str, tokens: int, duration_ms: int,
        tool_summary: dict[str, int],
    ) -> None:
        """Read-modify-write daily aggregate."""
        try:
            existing = None
            # Try to read existing rollup via raw query
            from app.storage.sqlite import SqliteStorageBackend
            if isinstance(self._storage, SqliteStorageBackend):
                with self._storage._store._connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM daily_rollup WHERE date=?", (date,),
                    ).fetchone()
                    if row:
                        existing = dict(row)

            if existing:
                old_tools = json.loads(existing["tool_summary"] or "{}")
                old_status = json.loads(existing["status_summary"] or "{}")
                session_count = existing["session_count"] + 1
                total_tokens = existing["total_tokens"] + tokens
                total_duration = existing["total_duration_ms"] + duration_ms
                # Merge tool summaries
                for tool, count in tool_summary.items():
                    old_tools[tool] = old_tools.get(tool, 0) + count
                old_status[status] = old_status.get(status, 0) + 1
                self._storage.upsert_daily_rollup(
                    date, session_count=session_count,
                    total_tokens=total_tokens,
                    total_duration_ms=total_duration,
                    tool_summary=json.dumps(old_tools, ensure_ascii=False),
                    status_summary=json.dumps(old_status, ensure_ascii=False),
                )
            else:
                status_summary = json.dumps({status: 1}, ensure_ascii=False)
                self._storage.upsert_daily_rollup(
                    date, session_count=1, total_tokens=tokens,
                    total_duration_ms=duration_ms,
                    tool_summary=json.dumps(tool_summary, ensure_ascii=False),
                    status_summary=status_summary,
                )
        except Exception:
            logger.exception("Failed to update daily rollup for %s", date)
