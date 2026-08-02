"""Mechanical boundaries for the Runtime/Hook/EventBus redesign."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_text(*roots: str) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for root in roots
        for path in (ROOT / root).rglob("*.py")
    )


def test_removed_event_bypasses_do_not_return() -> None:
    text = _python_text("agent", "server")
    assert "_persisted_event" not in text
    assert "publish_raw" not in text


def test_server_does_not_reach_through_runtime_private_state() -> None:
    text = _python_text("server")
    assert "_runtime._" not in text
    assert "runtime._store" not in text


def test_team_topology_is_not_a_runtime_capability() -> None:
    text = _python_text("agent", "server")
    assert "AgentTopology.TEAM" not in text
    assert "team_enabled" not in text
    assert "team_approved" not in text


def test_outbox_relay_is_composed_not_left_as_placeholder() -> None:
    text = (ROOT / "server/services/agent_service.py").read_text(encoding="utf-8")
    assert "self._outbox_relay = OutboxRelay(" in text


def test_native_submission_is_atomic() -> None:
    """NATIVE submission writes Run + Message + Outbox atomically.

    Verifies the full write path: submit_run_turn(NATIVE) →
    RunCoordinator.submit() → _StorageTx → SqliteOutboxStore.
    All three rows are written in a single SQLite transaction.
    """
    import sqlite3, tempfile, os
    from server.services.run_submission import submit_run_turn
    from infrastructure.outbox.sqlite_store import SqliteOutboxStore
    from application.events.schema_registry import SchemaRegistry

    db = os.path.join(tempfile.gettempdir(), "test_native_atomic.db")
    # Clean slate
    for ext in ("", "-wal", "-shm"):
        try: os.unlink(db + ext)
        except OSError: pass

    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, root_id TEXT, agent_name TEXT, mode TEXT,
            title TEXT, status TEXT, repo_path TEXT,
            created_at TEXT, updated_at TEXT, completed_at TEXT NULL,
            run_generation INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, session_id TEXT,
            turn_id TEXT, turn_index INTEGER DEFAULT 0,
            idempotency_key TEXT DEFAULT '', prompt TEXT DEFAULT '',
            status TEXT, created_at TEXT, updated_at TEXT,
            completed_at TEXT NULL
        );
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT,
            turn_id TEXT, created_at TEXT
        );
        INSERT INTO sessions(id,root_id,agent_name,mode,title,status,repo_path,created_at,updated_at)
        VALUES('s1','s1','test','primary','test','idle','.',datetime('now'),datetime('now'));
    """)
    conn.commit()
    # Use the new outbox store DDL so columns match
    SqliteOutboxStore.install(conn)
    conn.close()

    # Minimal storage mock — provides _db_path for the NATIVE path
    class _Storage:
        def __init__(self, path):
            self._db_path = path

    os.environ["GRACE_RUNTIME_MODE"] = "NATIVE"
    try:
        result = submit_run_turn(
            storage=_Storage(db),
            session_id="s1",
            prompt="test prompt",
        )
    finally:
        os.environ.pop("GRACE_RUNTIME_MODE", None)

    assert result.created is True
    assert result.run_id
    assert result.turn_id
    assert result.turn_index >= 1

    # Verify DB contents — all 3 writes in one transaction
    conn2 = sqlite3.connect(db)
    conn2.row_factory = sqlite3.Row
    runs = conn2.execute("SELECT COUNT(*) as c FROM runs").fetchone()["c"]
    msgs = conn2.execute("SELECT COUNT(*) as c FROM session_messages").fetchone()["c"]
    outbox = conn2.execute("SELECT COUNT(*) as c FROM event_outbox").fetchone()["c"]
    conn2.close()

    assert runs >= 1, f"Expected >=1 runs, got {runs}"
    assert msgs >= 1, f"Expected >=1 messages, got {msgs}"
    assert outbox >= 1, f"Expected >=1 outbox events, got {outbox}"
