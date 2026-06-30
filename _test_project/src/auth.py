"""Authentication and token verification module."""
import functools
import hashlib
import time
from flask import request, jsonify
from config import SECRET_KEY, TOKEN_EXPIRY_HOURS

ACTIVE_SESSIONS = {}


def generate_token(user_id: int) -> str:
    """Generate a simple auth token for a user."""
    timestamp = int(time.time())
    payload = f"{user_id}:{timestamp}:{SECRET_KEY}"
    token = hashlib.sha256(payload.encode()).hexdigest()[:32]
    ACTIVE_SESSIONS[token] = {"user_id": user_id, "created_at": timestamp}
    print(f"Generated token for user {user_id}: {token[:8]}...")
    return token


def verify_token(token: str) -> dict | None:
    """Verify token validity and expiry."""
    session = ACTIVE_SESSIONS.get(token)
    if not session:
        return None
    elapsed_hours = (time.time() - session["created_at"]) / 3600
    if elapsed_hours > TOKEN_EXPIRY_HOURS:
        del ACTIVE_SESSIONS[token]
        return None
    return session


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


def login(username: str, password: str) -> str | None:
    """Authenticate user credentials and return a token."""
    from src.user_service import UserService
    svc = UserService()
    user = svc.get_by_name(username)
    if not user:
        print(f"Login failed: user '{username}' not found")
        return None
    if user.get("password") != password:
        print(f"Login failed: wrong password for '{username}'")
        return None
    print(f"Login successful for '{username}'")
    return generate_token(user["id"])
