"""User API routes."""
import logging
import time
from flask import Blueprint, jsonify, request
from src.user_service import UserService
from src.middleware import handle_errors, require_auth
from src.auth import login, revoke_token
from src.utils.validators import validate_email, validate_username, validate_password

logger = logging.getLogger(__name__)

user_bp = Blueprint("users", __name__)
user_service = UserService()

_start_time = time.time()
_request_counter = 0


@user_bp.before_request
def _count_request():
    global _request_counter
    _request_counter += 1


@user_bp.route("/", methods=["GET"])
@handle_errors
@require_auth
def list_users():
    logger.debug("Fetching all users")
    users = user_service.get_all()
    return jsonify(users)


@user_bp.route("/<int:user_id>", methods=["GET"])
@handle_errors
@require_auth
def get_user(user_id):
    logger.debug("Fetching user %s", user_id)
    user = user_service.get_by_id(user_id)
    if not user:
        return jsonify({"error": "not found"}), 404
    return jsonify(user)


@user_bp.route("/<int:user_id>", methods=["DELETE"])
@handle_errors
@require_auth
def delete_user(user_id):
    logger.debug("Deleting user %s", user_id)
    deleted = user_service.delete_user(user_id)
    if not deleted:
        return jsonify({"error": "not found"}), 404
    return jsonify(deleted)


@user_bp.route("/", methods=["POST"])
@handle_errors
def create_user():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no data"}), 400
    name = (data.get("name") or "").strip()
    email = data.get("email", "")
    password = data.get("password", "")
    if not name:
        return jsonify({"error": "name required"}), 400
    if not validate_username(name):
        return jsonify({"error": "username must be 3-20 characters"}), 400
    if not validate_email(email):
        return jsonify({"error": "invalid email format"}), 400
    if not validate_password(password):
        return jsonify({"error": "password must be at least 6 characters"}), 400
    logger.debug("Creating user: %s", name)
    user = user_service.create(name, email, password)
    return jsonify(user), 201


@user_bp.route("/login", methods=["POST"])
@handle_errors
def do_login():
    data = request.get_json(silent=True)
    if not data or "username" not in data or "password" not in data:
        return jsonify({"error": "missing credentials"}), 400
    if not data.get("username", "").strip():
        return jsonify({"error": "username required"}), 400
    if not data.get("password") or len(data["password"]) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400
    token = login(data["username"], data["password"])
    if not token:
        return jsonify({"error": "invalid credentials"}), 401
    return jsonify({"token": token})


@user_bp.route("/logout", methods=["POST"])
@handle_errors
@require_auth
def do_logout():
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "", 1)
    revoke_token(token)
    return jsonify({"message": "logged out"}), 200


@user_bp.route("/health", methods=["GET"])
@handle_errors
def health():
    """Return server uptime and total request count served by this blueprint."""
    uptime = time.time() - _start_time
    return jsonify({"uptime": uptime, "request_count": _request_counter})


@user_bp.route("/stats", methods=["GET"])
@handle_errors
def stats():
    """Return total users and server uptime."""
    users = user_service.get_all()
    total_users = len(users)
    uptime = time.time() - _start_time
    return jsonify({"total_users": total_users, "uptime_seconds": uptime})
