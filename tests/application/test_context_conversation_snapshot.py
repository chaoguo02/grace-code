"""G33: Context/Conversation — immutable snapshot, no Runtime DB access.

AC: ContextSnapshot is immutable frozen dataclass
AC: ContextAssembler truncates to budget
AC: Child context has no parent history
AC: ConversationService retrieves and deduplicates
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from application.context.context_assembler import ContextAssembler, ContextSnapshot
from application.conversation.conversation_service import ConversationService


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE session_messages
        (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
         role TEXT, content TEXT, turn_id TEXT, created_at TEXT)""")
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


class TestContextAssembler:
    """G33: ContextAssembler builds immutable snapshots."""

    def test_assemble_basic(self):
        ca = ContextAssembler(budget=10000)
        msgs = [{"role": "user", "content": "hello"}]
        snap = ca.assemble(msgs)
        assert isinstance(snap, ContextSnapshot)
        assert len(snap.messages) == 1

    def test_truncates_to_budget(self):
        ca = ContextAssembler(budget=100)
        msgs = [{"role": "user", "content": "x" * 5000}] * 50
        snap = ca.assemble(msgs)
        assert len(snap.messages) < 50  # truncated

    def test_child_context_fresh(self):
        ca = ContextAssembler()
        snap = ca.child_context("read file.txt", budget=10000)
        assert len(snap.messages) == 1
        assert snap.messages[0]["role"] == "system"

    def test_snapshot_is_frozen(self):
        snap = ContextSnapshot(messages=({"role": "user", "content": "hi"},))
        with pytest.raises(Exception):
            snap.messages = ()  # type: ignore


class TestConversationService:
    """G33: ConversationService manages message state."""

    def test_append_and_retrieve(self, temp_db):
        svc = ConversationService(temp_db)
        svc.append_message("s1", "user", "hello", "t1")
        svc.append_message("s1", "assistant", "hi there", "t1")

        msgs = svc.get_messages("s1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"

    def test_deduplicate(self, temp_db):
        svc = ConversationService(temp_db)
        svc.append_message("s1", "user", "unique", "t1")
        assert svc.deduplicate("s1", "unique")
        assert not svc.deduplicate("s1", "nonexistent")
