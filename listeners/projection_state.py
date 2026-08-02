"""
G25: Projection state — aggregate watermark tracking, gap detection.

Each projection tracks (aggregate_id, last_seen_version).
Expected next version = last_seen + 1.
Gap detected → Retryable with missing range recorded.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class GapInfo:
    aggregate_id: str
    expected: int
    actual: int
    missing_range: str = ""


class ProjectionStateStore:
    """Tracks per-aggregate watermark for a projection."""

    def __init__(self, db_path: str, projection_name: str) -> None:
        self._db_path = db_path
        self._name = projection_name

    @staticmethod
    def install(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projection_watermarks (
                projection_name TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                last_version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (projection_name, aggregate_id)
            )
        """)

    def get_watermark(self, aggregate_id: str) -> int:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT last_version FROM projection_watermarks "
                "WHERE projection_name=? AND aggregate_id=?",
                (self._name, aggregate_id),
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def check_gap(self, aggregate_id: str,
                  version: int) -> GapInfo | None:
        """Check if *version* is the next expected.  Returns GapInfo if gap."""
        last = self.get_watermark(aggregate_id)
        expected = last + 1
        if version == expected:
            return None  # OK
        if version <= last:
            return GapInfo(
                aggregate_id=aggregate_id, expected=expected,
                actual=version,
                missing_range=f"duplicate or old: {version} <= {last}",
            )
        return GapInfo(
            aggregate_id=aggregate_id, expected=expected, actual=version,
            missing_range=f"gap: {expected}..{version - 1}",
        )

    def advance(self, conn: sqlite3.Connection, aggregate_id: str,
                version: int) -> None:
        """Update watermark to *version* (within transaction)."""
        conn.execute(
            """INSERT OR REPLACE INTO projection_watermarks
               (projection_name, aggregate_id, last_version, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (self._name, aggregate_id, version),
        )
