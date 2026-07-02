"""Middleware package: error handling and other cross-cutting concerns."""
import functools
import logging

from flask import jsonify

from .require_auth import require_auth

logger = logging.getLogger(__name__)


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
