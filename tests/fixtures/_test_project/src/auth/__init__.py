"""Authentication and token verification module.

This package is a drop-in replacement for the former ``src/auth.py`` module.
All public symbols are exposed here so that existing import statements
(``from src.auth import ...``) continue to work unchanged.
"""
import logging

from src.auth.service import AuthService
from src.auth.session import SessionManager

logger = logging.getLogger(__name__)

# ── Module-level singleton (preserved for backward compatibility) ──────────

auth_service = AuthService()


# ── Module-level convenience functions (delegate to the singleton) ─────────

def generate_token(user_id: int) -> str:
    """Generate a simple auth token for a user."""
    return auth_service.generate_token(user_id)


def verify_token(token: str) -> dict | None:
    """Verify token validity and expiry."""
    return auth_service.verify_token(token)


def login(username: str, password: str) -> str | None:
    """Authenticate user credentials and return a token."""
    return auth_service.login(username, password)


def revoke_token(token: str) -> None:
    """Remove a token from active sessions (logout support)."""
    return auth_service.revoke_token(token)


def cleanup_expired() -> int:
    """Remove all expired tokens and return the count removed."""
    return auth_service.cleanup_expired()


def get_active_sessions() -> dict:
    """Return all active sessions (for introspection/admin use)."""
    return auth_service.get_active_sessions()


__all__ = [
    "AuthService",
    "SessionManager",
    "auth_service",
    "generate_token",
    "verify_token",
    "login",
    "revoke_token",
    "cleanup_expired",
    "get_active_sessions",
]
