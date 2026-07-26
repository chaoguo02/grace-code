from __future__ import annotations

import sqlite3

from scripts.migrate_clean_runtime_prefixes import migrate


PREFIX = (
    "[UNVERIFIED — no test environment available. "
    "Code changes were made but NOT independently verified.]\n\n"
)


def _create_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                summary TEXT,
                agent_result_json TEXT,
                fork_result_json TEXT
            );
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY,
                role TEXT,
                content TEXT
            );
            CREATE TABLE runs (id TEXT PRIMARY KEY, summary TEXT);
            CREATE TABLE plan_revisions (id TEXT PRIMARY KEY, content TEXT);
            CREATE TABLE session_trace_events (
                id INTEGER PRIMARY KEY,
                event_type TEXT,
                event_json TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            ("s1", PREFIX + "Session answer", None, None),
        )
        conn.execute(
            "INSERT INTO session_messages VALUES (?, ?, ?)",
            (1, "assistant", PREFIX + "Message answer"),
        )
        conn.execute(
            "INSERT INTO session_messages VALUES (?, ?, ?)",
            (2, "assistant", "Legitimate [UNVERIFIED] text"),
        )


def test_runtime_prefix_migration_defaults_to_dry_run(tmp_path):
    db = tmp_path / "sessions.db"
    _create_db(db)

    counts = migrate(db, repo=None, apply=False)

    assert counts["sessions"] == 1
    assert counts["session_messages"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT summary FROM sessions WHERE id='s1'"
        ).fetchone()[0].startswith("[UNVERIFIED")


def test_runtime_prefix_migration_apply_is_exact_and_backed_up(tmp_path):
    db = tmp_path / "sessions.db"
    _create_db(db)

    counts = migrate(db, repo=None, apply=True)

    assert counts["sessions"] == 1
    assert len(list(tmp_path.glob("sessions.db.*.bak"))) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT summary FROM sessions WHERE id='s1'"
        ).fetchone()[0] == "Session answer"
        rows = conn.execute(
            "SELECT content FROM session_messages ORDER BY id"
        ).fetchall()
        assert rows == [("Message answer",), ("Legitimate [UNVERIFIED] text",)]
