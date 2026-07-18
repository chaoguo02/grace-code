"""Middleware package: error handling and other cross-cutting concerns."""
import functools
import logging
import time
from collections import defaultdict

from flask import jsonify, request

from .require_auth import require_auth

logger = logging.getLogger(__name__)

# In-memory store: IP -> list of request timestamps (seconds since epoch)
_request_timestamps: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX = 5  # requests per window


def rate_limit(f):
    """Decorator that enforces a sliding-window rate limit of 5 requests per minute per IP.

    Returns a JSON 429 response when the limit is exceeded.
    """

    @functools.wraps(f)
    def decorated(*args, **kwargs):
        client_ip = request.remote_addr
        now = time.time()
        cutoff = now - _RATE_LIMIT_WINDOW

        # Prune timestamps outside the current sliding window
        timestamps = _request_timestamps[client_ip]
        _request_timestamps[client_ip] = [t for t in timestamps if t > cutoff]

        if len(_request_timestamps[client_ip]) >= _RATE_LIMIT_MAX:
            return jsonify({"error": "rate limit exceeded"}), 429

        _request_timestamps[client_ip].append(now)
        return f(*args, **kwargs)

    return decorated


def handle_errors(f):
    """Decorator that catches unhandled exceptions and returns a JSON error response."""

    @functools.wraps(f)
    def decorated(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.exception("Unhandled error in %s: %s", f.__name__, e)
            return jsonify({"error": "internal server error"}), 500

    return decorated


def log_request_timing(f):
    """Decorator that logs the HTTP method, path, response status code, and duration of each request."""

    @functools.wraps(f)
    def decorated(*args, **kwargs):
        start = time.time()
        response = f(*args, **kwargs)
        elapsed = time.time() - start
        logger.info(
            "%s %s -> %s (%.3fs)",
            request.method,
            request.path,
            response[1] if isinstance(response, tuple) else response.status_code,
            elapsed,
        )
        return response

    return decorated
