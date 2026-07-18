"""Storage abstraction layer — abstract interface + SQLite implementation.

Usage::

    from app.storage.protocol import StorageBackend, StorageStats
    from app.storage.sqlite import SqliteStorageBackend

    backend: StorageBackend = SqliteStorageBackend(db_path)
    sessions = backend.list_sessions()
"""

from app.storage.protocol import StorageBackend, StorageStats
from app.storage.sqlite import SqliteStorageBackend

__all__ = ["StorageBackend", "StorageStats", "SqliteStorageBackend"]
