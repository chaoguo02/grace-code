"""
G33: Conversation Service — message state, serialization, dedup.

Runtime does NOT write to DB for conversation history.
This service owns message persistence and retrieval.
"""

from __future__ import annotations


class ConversationService:
    """Manages conversation message state.  Runtime never writes to DB."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def get_messages(self, session_id: str,
                     limit: int = 100) -> list[dict]:
        """Retrieve recent messages for a session."""
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT role, content, turn_id, created_at
                   FROM session_messages WHERE session_id=?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            result = []
            for r in reversed(rows):
                result.append({
                    "role": r["role"], "content": r["content"],
                    "turn_id": r["turn_id"],
                })
            return result
        finally:
            conn.close()

    def append_message(self, session_id: str, role: str,
                       content: str, turn_id: str) -> None:
        """Append a message (called within UoW transaction)."""
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT INTO session_messages
                   (session_id, role, content, turn_id, created_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (session_id, role, content, turn_id),
            )
            conn.commit()
        finally:
            conn.close()

    def deduplicate(self, session_id: str, content: str) -> bool:
        """True if identical content already exists for this session."""
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                """SELECT 1 FROM session_messages
                   WHERE session_id=? AND content=? LIMIT 1""",
                (session_id, content),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
