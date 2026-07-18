"""Token generation and verification — HMAC-based stateless logic."""
import hashlib
import time
import logging

logger = logging.getLogger(__name__)

_TOKEN_SEPARATOR = "."


def generate_token(user_id: int, secret_key: str, timestamp: int | None = None) -> str:
    """Generate an HMAC-bearing token for the given user and secret.

    The returned token embeds *user_id*, *timestamp*, and an HMAC so that
    :func:`verify_token` can later confirm the token was genuinely issued
    with the matching *secret_key* — all without consulting a session store.

    The caller is still responsible for storing the token alongside any
    session metadata (expiry, user info, etc.).
    """
    ts = timestamp if timestamp is not None else int(time.time())
    payload = f"{user_id}:{ts}:{secret_key}"
    token_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    token = f"{user_id}{_TOKEN_SEPARATOR}{ts}{_TOKEN_SEPARATOR}{token_hash}"
    logger.debug("Generated token for user %s: %s...", user_id, token[-16:])
    return token


def verify_token(token: str, secret_key: str) -> bool:
    """Verify that a *token* was generated with *secret_key* (HMAC check).

    Returns ``True`` only when the HMAC embedded in the token matches a
    freshly-computed HMAC built from the token's own *user_id*, *timestamp*,
    and the supplied *secret_key*.

    .. note:: This is a **cryptographic** check.  It does **not** consult a
              session store, so it cannot enforce expiry or revocation.
              Pair it with :class:`~src.auth.session.SessionManager` when
              those guarantees are needed.
    """
    try:
        user_id_str, ts_str, hash_val = token.split(_TOKEN_SEPARATOR)
    except (ValueError, AttributeError):
        return False
    expected_hash = hashlib.sha256(
        f"{user_id_str}:{ts_str}:{secret_key}".encode("utf-8")
    ).hexdigest()[:32]
    return hash_val == expected_hash
