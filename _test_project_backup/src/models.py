"""Data models and schemas."""

USER_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string", "minLength": 3, "maxLength": 20},
        "email": {"type": "string", "format": "email"},
        "password": {"type": "string", "minLength": 6},
    },
    "required": ["name", "email", "password"],
}

SESSION_SCHEMA = {
    "type": "object",
    "properties": {
        "user_id": {"type": "integer"},
        "token": {"type": "string"},
        "created_at": {"type": "integer"},
    },
    "required": ["user_id", "token"],
}
