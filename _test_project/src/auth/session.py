"""In-memory session storage with expiry support."""
import threading
import time
import logging

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages in-memory token → session mappings with expiry cleanup."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def store(self, token: str, user_id: int, created_at: int | None = None) -> None:
        """Record a new session for the given token."""
        with self._lock:
            self._sessions[token] = {
                "user_id": user_id,
                "created_at": created_at if created_at is not None else int(time.time()),
            }

    def get(self, token: str, token_expiry_hours: int) -> dict | None:
        """Look up a session by token, returning None if missing or expired."""
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return None
            elapsed_hours = (time.time() - session["created_at"]) / 3600
            if elapsed_hours > token_expiry_hours:
                del self._sessions[token]
                return None
            return session

    def revoke(self, token: str) -> None:
        """Remove a single token from the store."""
        self._sessions.pop(token, None)
        logger.debug("Revoked token %s...", token[:8])

    def cleanup_expired(self, token_expiry_hours: int) -> int:
        """Remove all expired tokens and return the count removed."""
        now = time.time()
        expired = [
            token
            for token, session in self._sessions.items()
            if (now - session["created_at"]) / 3600 > token_expiry_hours
        ]
        for token in expired:
            del self._sessions[token]
        if expired:
            logger.debug("Cleaned up %d expired session(s)", len(expired))
        return len(expired)

    def get_active_sessions(self) -> dict:
        """Return a shallow copy of all active sessions."""
        return dict(self._sessions)
