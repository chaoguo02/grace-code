"""
Stats service — CRUD for execution statistics, diffs, and daily rollups.

Thin wrapper over ``StorageBackend`` stats methods. All data is stored
in the same SQLite database as sessions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.storage.protocol import StorageBackend

logger = logging.getLogger(__name__)


class StatsService:
    """Query/update execution stats and diffs."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    def record_llm_turn(self, payload: dict[str, Any]) -> int:
        store = getattr(self._storage, "_store", None)
        connect = getattr(store, "_connect", None)
        if not callable(connect):
            return 0
        with connect() as conn:
            cur = conn.execute(
                """INSERT INTO llm_turn_metrics
                   (session_id, run_id, turn_id, step_number, input_tokens,
                    output_tokens, billable_tokens, cache_read_tokens,
                    cache_create_tokens, non_cached_input_tokens, token_source,
                    attempts, retries, backoff_ms, timed_out)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload.get("session_id", ""), payload.get("run_id", ""),
                    payload.get("turn_id", ""), int(payload.get("step_number", 0)),
                    int(payload.get("input_tokens", 0)), int(payload.get("output_tokens", 0)),
                    int(payload.get("billable_tokens", 0)),
                    int(payload.get("cache_read_tokens", 0)),
                    int(payload.get("cache_create_tokens", 0)),
                    int(payload.get("non_cached_input_tokens", 0)),
                    payload.get("token_source", "estimate"),
                    int(payload.get("attempts", 1)), int(payload.get("retries", 0)),
                    float(payload.get("backoff_ms", 0)), int(bool(payload.get("timed_out"))),
                ),
            )
            return int(cur.lastrowid)

    def get_llm_turns(self, session_id: str = "", limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        store = getattr(self._storage, "_store", None)
        connect = getattr(store, "_connect", None)
        if not callable(connect):
            return []
        query = "SELECT * FROM llm_turn_metrics"
        params: list[Any] = []
        if session_id:
            query += " WHERE session_id=?"
            params.append(session_id)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        with connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    @staticmethod
    def llm_overview(rows: list[dict[str, Any]]) -> dict[str, Any]:
        cache_read = sum(int(row.get("cache_read_tokens", 0)) for row in rows)
        uncached = sum(int(row.get("non_cached_input_tokens", 0)) for row in rows)
        denominator = cache_read + uncached
        return {
            "turns": len(rows),
            "input_tokens": sum(int(row.get("input_tokens", 0)) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens", 0)) for row in rows),
            "billable_tokens": sum(int(row.get("billable_tokens", 0)) for row in rows),
            "cache_read_tokens": cache_read,
            "cache_create_tokens": sum(int(row.get("cache_create_tokens", 0)) for row in rows),
            "non_cached_input_tokens": uncached,
            "cache_hit_rate": cache_read / denominator if denominator else None,
            "attempts": sum(int(row.get("attempts", 1)) for row in rows),
            "retries": sum(int(row.get("retries", 0)) for row in rows),
            "token_sources": sorted({str(row.get("token_source", "estimate")) for row in rows}),
        }

    def prune_raw_telemetry(self, retention_days: int = 90) -> dict[str, int]:
        """Delete expired fine-grained telemetry while preserving daily rollups."""
        store = getattr(self._storage, "_store", None)
        connect = getattr(store, "_connect", None)
        if not callable(connect):
            return {}
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=max(1, int(retention_days)))
        ).isoformat()
        deleted: dict[str, int] = {}
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table, timestamp_column in (
                    ("step_log", "timestamp"),
                    ("context_snapshot", "created_at"),
                    ("llm_turn_metrics", "created_at"),
                ):
                    cursor = conn.execute(
                        f"DELETE FROM {table} "
                        f"WHERE datetime({timestamp_column}) < datetime(?)",
                        (cutoff,),
                    )
                    deleted[table] = max(0, int(cursor.rowcount))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if any(deleted.values()):
            logger.info("Telemetry retention deleted expired rows: %s", deleted)
        return deleted

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
