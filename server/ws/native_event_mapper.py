"""
G27: Native Event Mapper — trace snapshot → WS replay, high-watermark gap avoidance.

- On WS connect: read snapshot from TraceProjection, then subscribe to live events.
- High-watermark prevents missed events between snapshot read and live subscription.
- Terminal facts shown only once (tracked in session).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class WatermarkToken:
    """Tracks last-seen event position for a session."""
    session_id: str
    last_seq: int = 0
    last_event_id: str = ""


class NativeEventMapper:
    """Bridge: TraceProjection snapshot → WS Gateway live subscription.

    Connect flow:
      1. Read snapshot from trace (events up to last_seq)
      2. Send snapshot to client
      3. Start live subscription at next seq
      4. High-watermark prevents gap between snapshot and live
    """

    def __init__(self, db_path: str, gateway) -> None:
        self._db_path = db_path
        self._gateway = gateway
        self._terminal_sent: set[str] = set()  # track sent terminal events

    def connect(self, session_id: str) -> tuple[list[dict], WatermarkToken]:
        """Read snapshot and return (events, watermark).

        The caller should then subscribe to live events starting from
        watermark.last_seq + 1.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT * FROM session_trace_events
                   WHERE session_id=? ORDER BY seq""",
                (session_id,),
            ).fetchall()

            events = []
            last_seq = 0
            last_event_id = ""
            for row in rows:
                events.append({
                    "seq": row["seq"],
                    "event_type": row["event_type"],
                    "timestamp": row["timestamp"],
                    "event_json": row["event_json"],
                })
                last_seq = max(last_seq, row["seq"] or 0)
                last_event_id = row["event_json"][:50]  # simplified

            # Mark terminal events as sent
            for evt in events:
                if evt["event_type"] in ("run.completed.v1", "run.failed.v1",
                                          "run.cancelled.v1"):
                    self._terminal_sent.add(evt["event_type"] + ":" + session_id)

            return events, WatermarkToken(
                session_id=session_id,
                last_seq=last_seq,
                last_event_id=last_event_id,
            )
        finally:
            conn.close()

    def is_terminal_already_sent(self, event_type: str, session_id: str) -> bool:
        return (event_type + ":" + session_id) in self._terminal_sent

    def mark_terminal_sent(self, event_type: str, session_id: str) -> None:
        self._terminal_sent.add(event_type + ":" + session_id)
