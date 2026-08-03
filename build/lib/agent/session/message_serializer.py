"""
CC-Native session message serialization (P0_4).

Design:
  - content_json: JSON array of ContentBlock objects (the real data)
  - content_text: plain-text fallback for old data + full-text search
  - message_kind: precise message type enum (replaces role/prefix inference)
  - SchemaMigrator: idempotent incremental migrations with version tracking

Decoupled from: LLM Backend, MCP Transport, Tool Registry, HITL Pipeline.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ── MessageKind ─────────────────────────────────────────────────────────────

class MessageKind(StrEnum):
    """Precise message type — replaces role/prefix-based inference."""
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    ATTACHMENT = "attachment"
    # ── Runtime-only (not persisted in session_messages) ──
    RUNTIME_NOTICE = "runtime_notice"
    PLAN_CONTEXT = "plan_context"


# ── Content helpers ─────────────────────────────────────────────────────────

def content_to_json(content) -> str:
    """Serialize message.content (str | list[dict] | None) to JSON string.

    Structured arrays are preserved as-is.
    Plain strings are wrapped in [{"type":"text","text":"..."}].
    """
    if content is None:
        return "[]"
    if isinstance(content, str):
        return json.dumps([{"type": "text", "text": content}], ensure_ascii=False)
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, default=str)
    return json.dumps([{"type": "text", "text": str(content)}], ensure_ascii=False)


def content_from_json(content_json: str | None, fallback_text: str = "") -> str | list[dict]:
    """Deserialize content_json back to message.content.

    Returns the structured list if valid JSON; falls back to plain text.
    """
    if not content_json:
        return fallback_text
    try:
        parsed = json.loads(content_json)
        if isinstance(parsed, list):
            return parsed
        return str(parsed)
    except (json.JSONDecodeError, TypeError):
        return fallback_text


def collapse_plain_text_content(content):
    """Return a string for the canonical one-text-block representation.

    Storage remains lossless for genuinely structured/multimodal content,
    while legacy text-only API and context consumers keep their historical
    string contract.
    """
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
    ):
        return content[0]["text"]
    return content


def content_to_text(content) -> str:
    """Extract a plain-text summary from content (for FTS and old clients)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool_use: {block.get('name', '')}]")
                elif block.get("type") == "tool_result":
                    text = str(block.get("content", ""))
                    parts.append(text[:500] if len(text) > 500 else text)
                elif block.get("type") == "image":
                    parts.append("[image]")
        return "\n".join(parts)
    return str(content)


def infer_message_kind(message_role: str, tool_call_id: str | None, tool_calls: Any) -> MessageKind:
    """Infer MessageKind from existing message fields.

    Used during migration and for old code paths.
    """
    if message_role == "system":
        return MessageKind.SYSTEM
    if message_role == "user":
        if tool_call_id:
            return MessageKind.TOOL_RESULT
        return MessageKind.USER
    if message_role == "assistant":
        return MessageKind.ASSISTANT
    return MessageKind.USER


# ── Schema Migrator ─────────────────────────────────────────────────────────

@dataclass
class SchemaMigration:
    version: int
    name: str
    sql: str  # idempotent: uses IF NOT EXISTS / WHERE ... IS NULL


SCHEMA_MIGRATIONS: list[SchemaMigration] = [
    SchemaMigration(1, "add_content_json", """
        ALTER TABLE session_messages ADD COLUMN content_json TEXT DEFAULT NULL;
    """),
    SchemaMigration(2, "add_message_kind", """
        ALTER TABLE session_messages ADD COLUMN message_kind TEXT DEFAULT NULL;
    """),
    SchemaMigration(3, "populate_message_kind", """
        UPDATE session_messages SET message_kind =
            CASE
                WHEN role = 'system' THEN 'system'
                WHEN role = 'user' AND tool_call_id IS NOT NULL AND tool_call_id != '' THEN 'tool_result'
                WHEN role = 'user' THEN 'user'
                WHEN role = 'assistant' THEN 'assistant'
                ELSE 'user'
            END
        WHERE message_kind IS NULL;
    """),
    SchemaMigration(4, "populate_content_json", """
        UPDATE session_messages SET content_json =
            json_array(json_object('type','text','text', content))
        WHERE content_json IS NULL AND content IS NOT NULL AND content != '';
    """),
]


class SchemaMigrator:
    """Idempotent schema migrator for session_store database."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def ensure_latest(self) -> int:
        """Apply all unapplied migrations. Returns current version."""
        with self._connect() as conn:
            self._init_version_table(conn)
            current = self._current_version(conn)
            for m in SCHEMA_MIGRATIONS:
                if m.version > current:
                    try:
                        conn.execute(m.sql)
                        conn.execute(
                            "INSERT OR REPLACE INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                            (m.version, m.name, _utc_now()),
                        )
                        logger.info("Migration v%d (%s) applied.", m.version, m.name)
                    except Exception as exc:
                        logger.warning(
                            "Migration v%d (%s) failed (may be idempotent): %s",
                            m.version, m.name, exc,
                        )
            return self._current_version(conn)

    def current_version(self) -> int:
        with self._connect() as conn:
            return self._current_version(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _init_version_table(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
        """)

    @staticmethod
    def _current_version(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
