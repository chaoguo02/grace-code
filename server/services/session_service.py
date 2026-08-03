"""
Session service — query operations over SessionStore.

Service boundary that provides structured session queries and session-owned
metadata transitions for the web API. It does not run agents.

Usage:
    store = SessionStore(db_path)
    service = SessionService(store)
    sessions = service.list_sessions(limit=20)
    messages = service.get_messages("abc123")
    events = service.get_events("abc123")
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from agent.event_log import EventLog
from agent.session.models import SessionRecord
from agent.task import Event, RunResult
from core.state_paths import ProjectStatePaths
from llm.base import LLMMessage

from app.storage.protocol import StorageBackend

logger = logging.getLogger(__name__)

_LEGACY_UNVERIFIED_PREFIX = re.compile(
    r"^\[UNVERIFIED\s+[-—–―〞]\s+(?:no test environment available|"
    r"project has no Git fact source|tests ran but failed|"
    r"test/validation did not run or was unavailable)\. "
    r"Code changes were made but NOT independently verified\.\]\r?\n\r?\n",
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _serialize_event(event: Event) -> dict[str, Any]:
    """Convert an Event domain object to a plain JSON-safe dict."""
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value if hasattr(event.event_type, "value") else event.event_type,
        "task_id": event.task_id,
        "timestamp": event.timestamp,
        "payload": event.payload,
    }


def _serialize_message(msg: LLMMessage) -> dict[str, Any]:
    """Convert an LLMMessage to a plain JSON-safe dict."""
    tool_calls = None
    if msg.tool_calls:
        tool_calls = [
            {
                "name": tc.name,
                "params": tc.params,
                "id": tc.id,
            }
            for tc in msg.tool_calls
        ]
    from agent.session.message_serializer import collapse_plain_text_content

    content = collapse_plain_text_content(msg.content)
    if msg.role == "assistant" and isinstance(content, str):
        # Compatibility for answers persisted before verification metadata
        # was separated from assistant prose.
        content = _LEGACY_UNVERIFIED_PREFIX.sub("", content, count=1)
    return {
        "role": msg.role,
        "content": content,
        "tool_calls": tool_calls,
        "tool_call_id": msg.tool_call_id,
        "created_at": getattr(msg, "created_at", ""),
        "turn_id": getattr(msg, "turn_id", ""),
    }


# ── SessionService ──────────────────────────────────────────────────────────


class SessionService:
    """Session queries and session-owned metadata backed by StorageBackend."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    @property
    def store(self) -> StorageBackend:
        """The underlying storage backend."""
        return self._storage

    # ── Session CRUD ──────────────────────────────────────────────────────

    def list_sessions(
        self, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        """List sessions with enriched summary data.

        Returns:
            list[dict]: Each session with ``id``, ``agent_name``, ``title``,
            ``status``, ``mode``, ``summary``, ``error``, ``parent_id``,
            ``created_at``, ``updated_at``, ``completed_at``, ``message_count``,
            ``total_tokens_estimate``.
        """
        records = self._storage.list_sessions(limit=limit, offset=offset)
        if not records:
            return []

        # One batch query for all session message counts (P2-48 fix:
        # replaces N per-session COUNT queries with a single GROUP BY).
        session_ids = [rec.id for rec in records]
        msg_counts: dict[str, tuple[int, int]] = {}
        try:
            store = getattr(self._storage, "store", None)
            if store is not None:
                placeholders = ",".join(["?"] * len(session_ids))
                with store._connect() as conn:
                    rows = conn.execute(
                        f"SELECT session_id, COUNT(*), COALESCE(SUM(LENGTH(content)), 0) "
                        f"FROM session_messages WHERE session_id IN ({placeholders}) "
                        f"GROUP BY session_id",
                        session_ids,
                    ).fetchall()
                    for row in rows:
                        msg_counts[row[0]] = (row[1], max(1, row[2] // 3) if row[2] else 0)
        except Exception:
            pass

        results: list[dict] = []
        for rec in records:
            mc = msg_counts.get(rec.id, (0, 0))
            results.append({
                "id": rec.id,
                "agent_name": rec.agent_name,
                "title": rec.title,
                "status": rec.status.value if hasattr(rec.status, "value") else rec.status,
                "mode": rec.mode.value if hasattr(rec.mode, "value") else rec.mode,
                "summary": rec.summary,
                "error": rec.error,
                "parent_id": rec.parent_id,
                "created_at": rec.created_at,
                "updated_at": rec.updated_at,
                "completed_at": rec.completed_at,
                "message_count": mc[0],
                "total_tokens_estimate": mc[1],
            })
        return results

    def get_session(self, session_id: str) -> SessionRecord | None:
        """Get a single session by ID.

        Returns:
            SessionRecord or None if not found.
        """
        return self._storage.get_session(session_id)

    def get_session_tree(self, session_id: str) -> dict | None:
        """Build a recursive session tree starting from *session_id*.

        Returns a nested dict with ``session`` summary and ``children`` list.
        Each child is recursively expanded up to 5 levels deep (CC-aligned).
        """
        rec = self._storage.get_session(session_id)
        if rec is None:
            return None

        def _build_node(sid: str, depth: int = 0) -> dict:
            if depth >= 5:
                return None
            node_rec = self._storage.get_session(sid)
            if node_rec is None:
                return None
            children = []
            for child in self._storage.list_child_sessions(sid):
                child_node = _build_node(child.id, depth + 1)
                if child_node:
                    children.append(child_node)
            return {
                "id": node_rec.id,
                "agent_name": node_rec.agent_name,
                "title": node_rec.title,
                "status": node_rec.status.value if hasattr(node_rec.status, "value") else str(node_rec.status),
                "depth": depth,
                "parent_id": node_rec.parent_id,
                "created_at": node_rec.created_at,
                "children": children,
                "child_count": len(children),
            }

        return _build_node(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages.

        Returns:
            bool: True if deleted, False if not found.
        """
        return self._storage.delete_session(session_id)

    def delete_sessions_batch(self, session_ids: list[str]) -> int:
        """Delete multiple sessions in one transaction.

        Returns:
            int: Number of sessions actually deleted.
        """
        return self._storage.delete_sessions_batch(session_ids)

    def update_title(self, session_id: str, title: str) -> bool:
        """Update a session's title.

        Returns:
            bool: True if updated, False if not found.
        """
        return self._storage.update_title(session_id, title)

    def update_agent_name(self, session_id: str, agent_name: str) -> bool:
        """Update a session's agent_name (mode).

        Returns:
            bool: True if updated, False if not found.
        """
        return self._storage.update_agent_name(session_id, agent_name)

    def claim_session_context(
        self, session_id: str, repo_path: str,
    ) -> str | None:
        """Return a changed session summary once and persist its content hash."""
        rec = self._storage.get_session(session_id)
        if rec is None:
            return None

        try:
            from context.compaction import load_session_summary

            summary_dir = Path(repo_path) / ".grace"
            summary = load_session_summary(str(summary_dir))
            if not summary:
                return None

            new_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
            metadata = dict(rec.metadata or {})
            if metadata.get("session_context_hash") == new_hash:
                return None

            metadata["session_context_hash"] = new_hash
            if not self._storage.update_metadata(session_id, metadata):
                logger.warning(
                    "Could not claim session context for session %s", session_id,
                )
                return None
            logger.debug("Session context claimed (hash=%s)", new_hash)
            return f"[PREVIOUS SESSION CONTEXT]\n{summary}"
        except Exception:
            logger.debug("Session summary claim skipped", exc_info=True)
            return None

    def get_session_detail(self, session_id: str) -> dict | None:
        """Get session detail with computed stats.

        Extends the base SessionRecord with:
        - ``message_count`` (int): Total messages in the session.
        - ``total_tokens_estimate`` (int): Rough token estimate from summary length.

        Returns:
            dict or None if not found.
        """
        rec = self._storage.get_session(session_id)
        if rec is None:
            return None

        # Compute stats
        try:
            msgs = self._storage.list_messages(session_id)
            message_count = len(msgs)
            total_tokens_estimate = sum(
                max(1, len(str(m.content or "")) // 3) for m in msgs
            )
        except Exception:
            message_count = 0
            total_tokens_estimate = 0

        return {
            "id": rec.id,
            "parent_id": rec.parent_id,
            "root_id": rec.root_id,
            "agent_name": rec.agent_name,
            "title": rec.title,
            "status": rec.status.value if hasattr(rec.status, "value") else rec.status,
            "mode": rec.mode.value if hasattr(rec.mode, "value") else rec.mode,
            "summary": rec.summary,
            "error": rec.error,
            "agent_kind": rec.agent_kind.value if hasattr(rec.agent_kind, "value") else rec.agent_kind,
            "context_origin": rec.context_origin.value if hasattr(rec.context_origin, "value") else rec.context_origin,
            "execution_placement": rec.execution_placement.value if hasattr(rec.execution_placement, "value") else rec.execution_placement,
            "workspace_mode": rec.workspace_mode.value if hasattr(rec.workspace_mode, "value") else rec.workspace_mode,
            "agent_depth": rec.agent_depth.value if hasattr(rec.agent_depth, "value") else int(rec.agent_depth),
            "generation": rec.generation,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "completed_at": rec.completed_at,
            "metadata": rec.metadata,
            "message_count": message_count,
            "total_tokens_estimate": total_tokens_estimate,
        }

    def get_child_sessions(self, parent_id: str) -> list[SessionRecord]:
        """Get all child sessions of a parent.

        Returns:
            list[SessionRecord]: Children ordered by creation time.
        """
        return self._storage.list_child_sessions(parent_id)

    def merge_metadata(self, session_id: str, extra: dict[str, Any]) -> None:
        """Merge session metadata without discarding unrelated runtime facts."""
        rec = self._storage.get_session(session_id)
        if rec is None:
            raise ValueError(f"Unknown session: {session_id}")
        metadata = dict(rec.metadata or {})
        metadata.update(extra)
        if not self._storage.update_metadata(session_id, metadata):
            raise ValueError(f"Unknown session: {session_id}")

    # ── Messages ──────────────────────────────────────────────────────────

    def append_message(self, session_id: str, message: Any) -> None:
        """Persist one LLMMessage (Phase 5: native 跨轮持久化)。

        复用 storage.append_message → message_serializer 序列化
        content_json/tool_calls_json/tool_call_id。
        """
        if self._storage.get_session(session_id) is None:
            raise ValueError(f"Unknown session: {session_id}")
        self._storage.append_message(session_id, message)

    def get_messages(self, session_id: str,
                     limit: int | None = None) -> list[dict[str, Any]]:
        """Get messages for a session as JSON-safe dicts (rich, Phase 3).

        Args:
            session_id: The session to query.
            limit: If set, return the most recent *limit* messages (for
                native conversation rebuild).

        Returns:
            list[dict]: Each message has ``role``, ``content``,
                ``tool_calls`` (optional), ``tool_call_id`` (optional),
                ``tool_name`` (optional).

        Raises:
            ValueError: If session not found.
        """
        if self._storage.get_session(session_id) is None:
            raise ValueError(f"Unknown session: {session_id}")
        msgs = self._storage.list_messages(session_id)
        result = [_serialize_message(m) for m in msgs]
        if limit is not None and limit > 0:
            result = result[-limit:]
        return result

    # ── Events ────────────────────────────────────────────────────────────

    def build_turn_timeline(
        self, session_id: str, *, after_seq: int = 0, limit: int = 200,
    ) -> dict[str, Any]:
        """Build a turn-grouped timeline for frontend consumption.

        Returns a dict with ``turns`` (primary, turn-grouped) and ``items``
        (legacy, flat message+event list).  The frontend uses ``turns`` for
        direct StreamingTurn construction — no more flat-list reconstruction.

        Each turn contains:
        - turn_id, run_id, turn_index
        - user_message (from session_messages)
        - assistant_message (from session_messages)
        - trace_events (WS-format events, sorted by seq)
        - meta (steps, tokens, status from runs table)
        """
        if self._storage.get_session(session_id) is None:
            raise ValueError(f"Unknown session: {session_id}")

        import json as _json

        # ── 1. Query messages (always — they're static after persist) ──
        raw_messages: list[dict[str, Any]] = self.get_messages(session_id)

        # ── 2. Query trace events (already parsed from event_json) ──
        raw_events = self.list_trace_events(
            session_id, after_seq=after_seq, limit=limit,
        )

        # ── 3. Query runs for turn metadata ──
        runs_by_turn: dict[str, dict[str, Any]] = {}
        try:
            store = getattr(self._storage, "store", None)
            if store is not None:
                with store._connect() as conn:
                    rows = conn.execute(
                        """SELECT turn_id, id AS run_id, turn_index, status,
                                  steps_taken, total_tokens, started_at, completed_at,
                                  error, termination_reason, verification_status,
                                  verification_reason, verification_checks_json,
                                  workspace_delta_json
                           FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY turn_id ORDER BY created_at DESC) AS rn
                                 FROM runs WHERE session_id = ?)
                           WHERE rn = 1""",
                        (session_id,),
                    ).fetchall()
                    for row in rows:
                        runs_by_turn[row["turn_id"]] = {
                            "run_id": row["run_id"] or "",
                            "turn_index": row["turn_index"] or 0,
                            "status": row["status"] or "",
                            "steps_taken": row["steps_taken"] or 0,
                            "total_tokens": row["total_tokens"] or 0,
                            "started_at": row["started_at"] or "",
                            "completed_at": row["completed_at"] or "",
                            "error": row["error"] or "",
                            "termination_reason": row["termination_reason"] or "none",
                            "verification": {
                                "status": row["verification_status"] or "not_applicable",
                                "reason": row["verification_reason"] or "none",
                                "checks": _json.loads(
                                    row["verification_checks_json"] or "[]"
                                ),
                            },
                            "workspace_delta": _json.loads(
                                row["workspace_delta_json"] or "{}"
                            ),
                        }
        except Exception:
            logger.debug("Failed to query runs for turn timeline", exc_info=True)

        # ── 4. Group messages by turn_id ──
        user_msgs_by_turn: dict[str, dict[str, Any]] = {}
        asst_msgs_by_turn: dict[str, dict[str, Any]] = {}
        _turn_order: list[str] = []

        for msg in raw_messages:
            tid = (msg.get("turn_id") or "").strip()
            role = (msg.get("role") or "").strip()
            if not tid:
                continue
            if tid not in _turn_order:
                _turn_order.append(tid)
            if role == "user":
                if tid not in user_msgs_by_turn:
                    user_msgs_by_turn[tid] = msg
            elif role == "assistant":
                # Runtime can persist intermediate assistant/tool-call history
                # before appending the canonical RunResult summary.  The final
                # assistant message in a turn is therefore authoritative.
                asst_msgs_by_turn[tid] = msg

        # ── 5. Build run_id → turn_id reverse mapping ──
        # Events are injected with turn_id (Batch 5 fix) but legacy events
        # may only have run_id.  Resolve via the runs table.
        turn_id_by_run: dict[str, str] = {}
        for tid, meta in runs_by_turn.items():
            _rid = meta.get("run_id", "")
            if _rid:
                turn_id_by_run[_rid] = tid

        # ── 6. Group trace events by turn_id ──
        events_by_turn: dict[str, list[dict[str, Any]]] = {}
        # Older persisted typed events can have blank run_id/turn_id because
        # their dataclass supplied empty envelope fields which EventBus did
        # not overwrite.  run_started/run_terminal still carry the real
        # identifiers, so use the ordered lifecycle span to recover those
        # otherwise orphaned events on replay.
        active_event_turn_id = ""
        for ev in raw_events:
            tid = (ev.get("turn_id") or "").strip()
            if not tid:
                # Legacy: events only have run_id — resolve via runs table
                _rid = (ev.get("run_id") or "").strip()
                if _rid and _rid in turn_id_by_run:
                    tid = turn_id_by_run[_rid]
                else:
                    tid = _rid
            if not tid:
                tid = active_event_turn_id
            if not tid:
                continue
            if ev.get("type") == "run_started":
                active_event_turn_id = tid
            if tid not in events_by_turn:
                events_by_turn[tid] = []
            events_by_turn[tid].append(ev)
            if ev.get("type") == "run_terminal" and tid == active_event_turn_id:
                active_event_turn_id = ""

        # ── 6. Build turn list ──
        turns: list[dict[str, Any]] = []
        seen_tids: set[str] = set()

        for tid in _turn_order:
            if tid in seen_tids:
                continue
            seen_tids.add(tid)

            run_meta = runs_by_turn.get(tid, {})
            turn_events = sorted(
                events_by_turn.pop(tid, []),
                key=lambda e: e.get("seq", 0),
            )

            turns.append({
                "turn_id": tid,
                "run_id": run_meta.get("run_id", ""),
                "turn_index": run_meta.get("turn_index", 0),
                "user_message": user_msgs_by_turn.get(tid),
                "assistant_message": asst_msgs_by_turn.get(tid),
                "trace_events": turn_events,
                "meta": {
                    "steps": run_meta.get("steps_taken", 0),
                    "tokens": run_meta.get("total_tokens", 0),
                    "status": run_meta.get("status", ""),
                    "started_at": run_meta.get("started_at", ""),
                    "completed_at": run_meta.get("completed_at", ""),
                    "error": run_meta.get("error", ""),
                    "termination_reason": run_meta.get("termination_reason", "none"),
                    "verification": run_meta.get("verification", {
                        "status": "not_applicable", "reason": "none", "checks": [],
                    }),
                    "workspace_delta": run_meta.get("workspace_delta", {}),
                },
            })

        # Remaining events (turn_id not in messages, e.g. plan_mode runs)
        for tid, evs in events_by_turn.items():
            if tid in seen_tids:
                continue
            seen_tids.add(tid)
            run_meta = runs_by_turn.get(tid, {})
            turn_events = sorted(evs, key=lambda e: e.get("seq", 0))
            turns.append({
                "turn_id": tid,
                "run_id": run_meta.get("run_id", ""),
                "turn_index": run_meta.get("turn_index", 0),
                "user_message": None,
                "assistant_message": None,
                "trace_events": turn_events,
                "meta": {
                    "steps": run_meta.get("steps_taken", 0),
                    "tokens": run_meta.get("total_tokens", 0),
                    "status": run_meta.get("status", ""),
                    "started_at": run_meta.get("started_at", ""),
                    "completed_at": run_meta.get("completed_at", ""),
                    "error": run_meta.get("error", ""),
                    "termination_reason": run_meta.get("termination_reason", "none"),
                    "verification": run_meta.get("verification", {
                        "status": "not_applicable", "reason": "none", "checks": [],
                    }),
                    "workspace_delta": run_meta.get("workspace_delta", {}),
                },
            })

        # ── 7. Active run ──
        active_run: dict[str, Any] | None = None
        try:
            store_ref = getattr(self._storage, "store", None)
            if store_ref is not None:
                with store_ref._connect() as conn:
                    row = conn.execute(
                        """SELECT id, turn_id, turn_index, prompt, status
                           FROM runs
                           WHERE session_id=? AND status IN ('queued','running')
                           LIMIT 1""",
                        (session_id,),
                    ).fetchone()
                    if row:
                        active_run = {
                            "run_id": row["id"],
                            "turn_id": row["turn_id"],
                            "turn_index": row["turn_index"],
                            "prompt": row["prompt"],
                            "status": row["status"],
                        }
        except Exception:
            pass

        max_seq = max(
            (int(e.get("seq") or 0) for e in raw_events),
            default=after_seq,
        )

        # ── 8. Build legacy items (for backward compat) ──
        items: list[dict[str, Any]] = []
        if after_seq <= 0:
            for msg in raw_messages:
                items.append({
                    "source": "message",
                    "timestamp": msg.get("created_at", ""),
                    "message": msg,
                })
        for ev in raw_events:
            items.append({
                "source": "ws",
                "timestamp": ev.get("timestamp", ""),
                "event": ev,
                "seq": ev.get("seq", 0),
            })
        items.sort(key=lambda item: item.get("timestamp") or "")

        return {
            "turns": turns,
            "items": items,
            "last_seq": max_seq,
            "has_more": len(raw_events) >= limit,
            "active_run": active_run,
        }

    def list_trace_events(
        self, session_id: str, *, after_seq: int = 0, limit: int = 200,
    ) -> list[dict[str, Any]]:
        if self._storage.get_session(session_id) is None:
            raise ValueError(f"Unknown session: {session_id}")
        return self._storage.list_trace_events(
            session_id, after_seq=after_seq, limit=limit,
        )

    def list_runs(self, session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return structured run outcomes for debug, stats, and audit views."""
        if self._storage.get_session(session_id) is None:
            raise ValueError(f"Unknown session: {session_id}")
        rows = self._storage.list_runs(session_id, limit=limit)
        for row in rows:
            row["verification_checks"] = json.loads(
                row.pop("verification_checks_json", "[]") or "[]"
            )
            row["workspace_delta"] = json.loads(
                row.pop("workspace_delta_json", "{}") or "{}"
            )
        return rows

    def get_events(
        self, session_id: str, *, after: int = 0, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read EventLog JSONL events for a session.

        Events are stored in per-run JSONL files under the project's state
        log directory.  This method reads all JSONL files whose ``task_id``
        matches the session, deduplicates by ``event_id``, and returns them
        ordered by timestamp.

        Args:
            session_id: The session whose events to fetch.
            after: 0-based index — skip this many events before applying
                ``limit`` (default 0).
            limit: Max events to return (default 1000).

        Returns:
            list[dict]: Each contains ``event_id``, ``event_type``,
                ``task_id``, ``timestamp``, ``payload``.

        Raises:
            ValueError: If session not found.
        """
        session = self._storage.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")

        log_dir = self._resolve_log_dir(session.repo_path)
        events: list[dict[str, Any]] = []

        # Scan only this session's JSONL files.
        # EventLog filenames are isolated by task_id: {task_id}_{timestamp}.jsonl
        log_path = Path(log_dir)
        if log_path.is_dir():
            patterns = [f"{session_id}_*.jsonl"]
            # Backward compatibility: keep a defensive fallback for legacy filenames,
            # but still require raw.task_id to match the requested session.
            candidate_files = []
            for pattern in patterns:
                candidate_files.extend(sorted(log_path.glob(pattern)))
            if not candidate_files:
                candidate_files = sorted(log_path.glob("*.jsonl"))

            for jsonl_file in candidate_files:
                try:
                    for line in jsonl_file.read_text("utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        raw_task_id = str(raw.get("task_id", "") or "")
                        raw_session_id = str(raw.get("session_id", "") or "")
                        if raw_task_id != session_id and raw_session_id != session_id:
                            continue

                        events.append({
                            "event_id": raw.get("event_id", ""),
                            "event_type": raw.get("event_type", ""),
                            "task_id": raw_task_id,
                            "timestamp": raw.get("timestamp", ""),
                            "payload": raw.get("payload", {}),
                        })
                except OSError:
                    continue

        # Apply pagination
        if after > 0:
            events = events[after:]
        return events[:limit]

    # ── Cancel ────────────────────────────────────────────────────────────

    def cancel_session(
        self, session_id: str, detail: str = "",
    ) -> bool:
        """Cancel a running session.

        Delegates to SessionRuntime's cancellation token mechanism. Returns
        False if the session has no active token (e.g. already finished).

        Args:
            session_id: The session to cancel.
            detail: Human-readable cancellation reason.

        Returns:
            bool: True if the session had an active token and was cancelled.
        """
        # NOTE: Cancel is delegated to AgentService which holds the
        # SessionRuntime reference. This method is a placeholder for the
        # web API contract — the actual cancellation path is:
        #
        #     router → AgentService.cancel_session(session_id, detail)
        #
        # See agent_service.py for the real implementation.
        raise NotImplementedError(
            "Use AgentService.cancel_session() — it holds the SessionRuntime reference"
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _resolve_log_dir(self, repo_path: str) -> str:
        """Resolve the EventLog directory for a repo path.

        Uses the same logic as EventLog.create() to find the log directory.
        """
        try:
            state_paths = ProjectStatePaths.for_project(repo_path)
            return str(state_paths.logs)
        except Exception:
            return "logs"
