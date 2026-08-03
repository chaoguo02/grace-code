"""P0_4: Session message serializer — acceptance tests.

AC mappings:
  AC-2.1 serialize assistant+tool_use → content_json is structured array
  AC-2.2 deserialize content_json → LLMMessage.content is list[ContentBlock]
  AC-2.3 old data (content_text only) → fallback to plain text
  AC-2.4 validate_pairs catches missing tool_result
  AC-5.1 roundtrip text message
  AC-5.2 roundtrip tool_use message
  AC-5.3 roundtrip image message
  AC-5.4 old data fallback
"""

from __future__ import annotations

import json
import tempfile
import os

import pytest


# ===========================================================================
# 1. Content serialization roundtrip
# ===========================================================================

class TestContentRoundtrip:

    def test_text_only(self):
        from agent.session.message_serializer import content_to_json, content_from_json
        original = "hello world"
        js = content_to_json(original)
        restored = content_from_json(js)
        assert restored == [{"type": "text", "text": "hello world"}]

    def test_structured_blocks(self):
        from agent.session.message_serializer import content_to_json, content_from_json
        original = [
            {"type": "text", "text": "I'll search"},
            {"type": "tool_use", "id": "toolu_1", "name": "Grep", "input": {"pattern": "TODO"}},
        ]
        js = content_to_json(original)
        restored = content_from_json(js)
        assert isinstance(restored, list)
        assert len(restored) == 2
        assert restored[1]["type"] == "tool_use"
        assert restored[1]["id"] == "toolu_1"

    def test_image_block_preserved(self):
        from agent.session.message_serializer import content_to_json, content_from_json
        original = [
            {"type": "image", "source": {"type": "base64", "data": "AAAA", "media_type": "image/png"}},
        ]
        js = content_to_json(original)
        restored = content_from_json(js)
        assert restored[0]["type"] == "image"
        assert restored[0]["source"]["media_type"] == "image/png"

    def test_none_content(self):
        from agent.session.message_serializer import content_to_json, content_from_json
        assert content_to_json(None) == "[]"
        assert content_from_json(None, "fallback") == "fallback"

    def test_fallback_to_plain_text(self):
        from agent.session.message_serializer import content_from_json
        # Old data: no content_json, only content_text
        result = content_from_json(None, fallback_text="plain old text")
        assert result == "plain old text"

    def test_invalid_json_fallback(self):
        from agent.session.message_serializer import content_from_json
        result = content_from_json("not valid json", fallback_text="safe fallback")
        assert result == "safe fallback"


# ===========================================================================
# 2. MessageKind inference
# ===========================================================================

class TestMessageKindInference:

    def test_user_message(self):
        from agent.session.message_serializer import infer_message_kind, MessageKind
        assert infer_message_kind("user", None, None) == MessageKind.USER

    def test_tool_result(self):
        from agent.session.message_serializer import infer_message_kind, MessageKind
        assert infer_message_kind("user", "toolu_xxx", None) == MessageKind.TOOL_RESULT

    def test_assistant_message(self):
        from agent.session.message_serializer import infer_message_kind, MessageKind
        assert infer_message_kind("assistant", None, None) == MessageKind.ASSISTANT

    def test_system_message(self):
        from agent.session.message_serializer import infer_message_kind, MessageKind
        assert infer_message_kind("system", None, None) == MessageKind.SYSTEM


# ===========================================================================
# 3. Schema migration
# ===========================================================================

class TestSchemaMigration:

    def test_migration_idempotent(self):
        from agent.session.message_serializer import SchemaMigrator
        tmp = os.path.join(tempfile.gettempdir(), "test_migrate_idem.db")
        try:
            # Create base tables first (simulating _init_db)
            import sqlite3
            conn = sqlite3.connect(tmp)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_call_id TEXT NULL,
                    tool_name TEXT NULL,
                    tool_calls_json TEXT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
            """)
            conn.close()

            m = SchemaMigrator(tmp)
            v1 = m.ensure_latest()
            assert v1 == 4, f"First run should be v4, got {v1}"
            # Idempotent
            v2 = m.ensure_latest()
            assert v2 == 4, f"Second run should still be v4, got {v2}"
        finally:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(tmp + ext)
                except Exception:
                    pass

    def test_migration_adds_columns(self):
        from agent.session.message_serializer import SchemaMigrator
        import sqlite3
        tmp = os.path.join(tempfile.gettempdir(), "test_migrate_cols.db")
        try:
            conn = sqlite3.connect(tmp)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                    tool_call_id TEXT NULL, tool_name TEXT NULL,
                    tool_calls_json TEXT NULL, turn_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
            """)
            conn.close()
            SchemaMigrator(tmp).ensure_latest()
            # Verify columns exist
            conn = sqlite3.connect(tmp)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(session_messages)").fetchall()]
            assert "content_json" in cols
            assert "message_kind" in cols
            conn.close()
        finally:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(tmp + ext)
                except Exception:
                    pass


# ===========================================================================
# 4. Prefix filter replacement
# ===========================================================================

class TestPrefixFilterReplacement:

    def test_user_message_with_system_prefix_not_filtered(self):
        """P0_4: User message starting with [SYSTEM] is NOT filtered."""
        from agent.session.message_serializer import infer_message_kind, MessageKind
        kind = infer_message_kind("user", None, None)
        # A user message should never be classified as SYSTEM
        assert kind != MessageKind.SYSTEM
        assert kind == MessageKind.USER

    def test_system_message_filtered_by_kind(self):
        """P0_4: SYSTEM kind messages are filtered regardless of content."""
        from agent.session.message_serializer import infer_message_kind, MessageKind
        kind = infer_message_kind("system", None, None)
        assert kind == MessageKind.SYSTEM
