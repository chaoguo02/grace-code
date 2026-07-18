"""Data models and schemas."""

# ── DDL table schemas for database migration ──────────────────────────────

USER_TABLE = {
    "table": "users",
    "columns": [
        {"name": "id",         "type": "INTEGER",  "constraints": "PRIMARY KEY AUTOINCREMENT"},
        {"name": "name",       "type": "VARCHAR(20)", "constraints": "NOT NULL UNIQUE"},
        {"name": "email",      "type": "VARCHAR(120)", "constraints": "NOT NULL UNIQUE"},
        {"name": "password",   "type": "VARCHAR(255)", "constraints": "NOT NULL"},
        {"name": "created_at", "type": "TIMESTAMP", "constraints": "DEFAULT CURRENT_TIMESTAMP"},
    ],
}

SESSION_TABLE = {
    "table": "sessions",
    "columns": [
        {"name": "token",      "type": "VARCHAR(64)", "constraints": "PRIMARY KEY"},
        {"name": "user_id",    "type": "INTEGER",     "constraints": "NOT NULL REFERENCES users(id)"},
        {"name": "created_at", "type": "INTEGER",     "constraints": "NOT NULL"},
    ],
}

SCHEMAS = [
    USER_TABLE,
    SESSION_TABLE,
]
