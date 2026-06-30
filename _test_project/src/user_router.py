"""User API routes."""
from flask import Blueprint, jsonify, request
from src.user_service import UserService
from src.auth import require_auth, login
from src.utils.validators import validate_email, validate_username

user_bp = Blueprint("users", __name__)
user_service = UserService()


@user_bp.route("/", methods=["GET"])
@require_auth
def list_users():
    print("Fetching all users")
    users = user_service.get_all()
    return jsonify(users)


@user_bp.route("/<int:user_id>", methods=["GET"])
@require_auth
def get_user(user_id):
    print(f"Fetching user {user_id}")
    user = user_service.get_by_id(user_id)
    if not user:
        return jsonify({"error": "not found"}), 404
    return jsonify(user)


@user_bp.route("/", methods=["POST"])
def create_user():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no data"}), 400
    name = data.get("name", "")
    email = data.get("email", "")
    password = data.get("password", "")
    if not name:
        return jsonify({"error": "name required"}), 400
    if not validate_username(name):
        return jsonify({"error": "username must be 3-20 characters"}), 400
    if not validate_email(email):
        return jsonify({"error": "invalid email format"}), 400
    print(f"Creating user: {name}")
    user = user_service.create(name, email, password)
    return jsonify(user), 201


@user_bp.route("/login", methods=["POST"])
def do_login():
    data = request.get_json(silent=True)
    if not data or "username" not in data or "password" not in data:
        return jsonify({"error": "missing credentials"}), 400
    token = login(data["username"], data["password"])
    if not token:
        return jsonify({"error": "invalid credentials"}), 401
    return jsonify({"token": token})
