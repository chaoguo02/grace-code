"""Bounded direct-message mailbox for approved team members."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import uuid


@dataclass(frozen=True)
class MailMessage:
    id: str
    sender_id: str
    recipient_id: str
    body: str
    created_at: str


class Mailbox:
    def __init__(self, members: set[str] | frozenset[str], *, max_pending: int = 256) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._members = set(members)
        self._pending: dict[str, list[MailMessage]] = {
            member: [] for member in members
        }
        self._max_pending = max_pending
        self._lock = threading.RLock()

    def add_member(self, member_id: str) -> None:
        if not member_id:
            raise ValueError("member_id is required")
        with self._lock:
            self._members.add(member_id)
            self._pending.setdefault(member_id, [])

    def send(self, sender_id: str, recipient_id: str, body: str) -> MailMessage:
        if sender_id not in self._members or recipient_id not in self._members:
            raise PermissionError("team messages are restricted to registered members")
        if not body.strip():
            raise ValueError("message body is required")
        with self._lock:
            queue = self._pending[recipient_id]
            if len(queue) >= self._max_pending:
                raise OverflowError("recipient mailbox is full")
            message = MailMessage(
                id=uuid.uuid4().hex,
                sender_id=sender_id,
                recipient_id=recipient_id,
                body=body.strip(),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            queue.append(message)
            return message

    def receive(self, recipient_id: str, *, limit: int | None = None) -> tuple[MailMessage, ...]:
        if recipient_id not in self._members:
            raise PermissionError("recipient is not a team member")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            queue = self._pending[recipient_id]
            count = len(queue) if limit is None else min(limit, len(queue))
            messages = tuple(queue[:count])
            del queue[:count]
            return messages

    def pending_count(self, recipient_id: str) -> int:
        if recipient_id not in self._members:
            raise PermissionError("recipient is not a team member")
        with self._lock:
            return len(self._pending[recipient_id])

