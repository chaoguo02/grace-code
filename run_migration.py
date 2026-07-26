"""One-shot migration: create runs table + turn_id column."""
import sqlite3
from agent.session import default_session_db_path

db_path = default_session_db_path(".")
print(f"Migrating: {db_path}")
conn = sqlite3.connect(db_path)

# Create runs table
conn.execute("""
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        summary TEXT NOT NULL DEFAULT '',
        steps_taken INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
""")
print("runs table OK")

# Indexes
conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_runs_session_created "
    "ON runs(session_id, created_at)"
)
conn.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency "
    "ON runs(session_id, idempotency_key) WHERE idempotency_key != ''"
)
conn.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active "
    "ON runs(session_id) WHERE status IN ('queued', 'running')"
)
print("indexes OK")

# turn_id column
try:
    conn.execute(
        "ALTER TABLE session_messages ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''"
    )
    print("turn_id column added")
except sqlite3.OperationalError as e:
    if "duplicate" in str(e).lower():
        print("turn_id already exists")
    else:
        print(f"turn_id: {e}")

conn.commit()
conn.close()
print("Migration done")
