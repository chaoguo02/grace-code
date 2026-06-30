"""Middleware package."""
import functools
import traceback
from flask import jsonify


def handle_errors(f):
    """Decorator that catches exceptions and returns a JSON error response."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"Unhandled error in {f.__name__}: {e}")
            traceback.print_exc()
            return jsonify({"error": "internal server error"}), 500
    return decorated
