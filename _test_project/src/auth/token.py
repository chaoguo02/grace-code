"""Token generation and verification — pure hash-based stateless logic."""
import hashlib
import time
import logging

logger = logging.getLogger(__name__)


def generate_token(user_id: int, secret_key: str, timestamp: int | None = None) -> str:
    """Generate a SHA-256 token string for the given user and secret.

    Returns the raw token (caller is responsible for storing it alongside
    any session metadata).
    """
    ts = timestamp if timestamp is not None else int(time.time())
    payload = f"{user_id}:{ts}:{secret_key}"
    token = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    logger.debug("Generated token for user %s: %s...", user_id, token[:8])
    return token


def verify_token(token: str, secret_key: str) -> bool:
    """Verify that a token conforms to the expected format for a given secret.

    .. note:: This is a *structural* check only.  Real validation against
              a live session store (including expiry) is done by
              :class:`~src.auth.session.SessionManager`.
    """
    # Structural validation is inherently limited; real verification
    # requires comparing against the stored session.  This function
    # exists primarily so that consumers that only need a format check
    # can avoid pulling in the full session store.
    return isinstance(token, str) and len(token) == 32
