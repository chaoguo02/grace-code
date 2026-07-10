"""Cross-cutting concern: require_auth decorator for protecting routes."""
import functools
from flask import request, jsonify
from src.auth import verify_token


def require_auth(f):
    """Decorator that checks Bearer token in Authorization header."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "unauthorized"}), 401
        token = auth_header[7:]
        session = verify_token(token)
        if not session:
            return jsonify({"error": "forbidden"}), 403
        request.current_user_id = session["user_id"]
        return f(*args, **kwargs)
    return decorated
