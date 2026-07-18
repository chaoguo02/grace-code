"""Auth orchestration — wires token creation, session storage, and login."""
import logging
from config import SECRET_KEY, TOKEN_EXPIRY_HOURS
from src.user_service import UserService, _verify_password
from src.auth.token import generate_token as _generate_token, verify_token as _verify_token
from src.auth.session import SessionManager

logger = logging.getLogger(__name__)


class AuthService:
    """Encapsulates authentication logic with injectable configuration."""

    def __init__(
        self,
        secret_key: str = SECRET_KEY,
        token_expiry_hours: int = TOKEN_EXPIRY_HOURS,
    ):
        self._secret_key = secret_key
        self._token_expiry_hours = token_expiry_hours
        self._session_manager = SessionManager()

    # ── public API ──────────────────────────────────────────────────────────

    def generate_token(self, user_id: int) -> str:
        """Generate a simple auth token for a user and store it."""
        import time

        timestamp = int(time.time())
        token = _generate_token(user_id, self._secret_key, timestamp)
        self._session_manager.store(token, user_id, timestamp)
        return token

    def verify_token(self, token: str) -> dict | None:
        """Verify token validity and expiry, returning the session or None."""
        return self._session_manager.get(token, self._token_expiry_hours)

    def login(
        self, username: str, password: str, user_service: UserService | None = None
    ) -> str | None:
        """Authenticate user credentials and return a token."""
        svc = user_service or UserService()
        user = svc.get_by_name(username)
        if not user:
            logger.debug("Login failed: user '%s' not found", username)
            return None
        if not _verify_password(password, user["password"]):
            logger.debug("Login failed: wrong password for '%s'", username)
            return None
        logger.debug("Login successful for '%s'", username)
        token = self.generate_token(user["id"])
        # Sanity-check that the generated token passes HMAC verification.
        if not _verify_token(token, self._secret_key):
            logger.error("Generated token failed HMAC self-check for user '%s'", username)
            return None
        return token

    def revoke_token(self, token: str) -> None:
        """Remove a token from active sessions (logout support)."""
        self._session_manager.revoke(token)

    def cleanup_expired(self) -> int:
        """Remove all expired tokens and return the count removed."""
        return self._session_manager.cleanup_expired(self._token_expiry_hours)

    def get_active_sessions(self) -> dict:
        """Return all active sessions (for introspection/admin use)."""
        return self._session_manager.get_active_sessions()
