-- Migration: runs table + turn_id column
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
);
CREATE INDEX IF NOT EXISTS idx_runs_session_created ON runs(session_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency ON runs(session_id, idempotency_key) WHERE idempotency_key != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active ON runs(session_id) WHERE status IN ('queued', 'running');

ALTER TABLE session_messages ADD COLUMN turn_id TEXT NOT NULL DEFAULT '';
