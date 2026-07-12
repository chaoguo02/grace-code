"""User business logic and data access."""
import base64
import hashlib
import logging
import os

logger = logging.getLogger(__name__)


def _hash_password(password: str) -> str:
    """Hash a password with PBKDF2-SHA256 and a random 16-byte salt.

    Returns a ``salt$hash`` string where both components are base64-encoded.
    """
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def _verify_password(password: str, stored: str) -> bool:
    """Verify a plaintext password against a ``salt$hash`` string."""
    salt_b64, hash_b64 = stored.split("$")
    salt = base64.b64decode(salt_b64)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return base64.b64encode(key).decode() == hash_b64


FAKE_DB = [
    {"id": 1, "name": "alice", "email": "alice@example.com", "password": _hash_password("pass123")},
    {"id": 2, "name": "bob", "email": "bob@example.com", "password": _hash_password("secret456")},
    {"id": 3, "name": "charlie", "email": "charlie@example.com", "password": _hash_password("qwerty")},
]

_next_id = 4


class UserService:
    def get_all(self):
        """Return all users (without passwords)."""
        return [{"id": u["id"], "name": u["name"], "email": u["email"]} for u in FAKE_DB]

    def get_by_id(self, user_id: int):
        """Find user by ID."""
        for user in FAKE_DB:
            if user["id"] == user_id:
                return {"id": user["id"], "name": user["name"], "email": user["email"]}
        return None

    def get_by_name(self, name: str):
        """Find user by username (includes password for auth)."""
        for user in FAKE_DB:
            if user["name"] == name:
                return user
        return None

    def create(self, name: str, email: str, password: str):
        """Create a new user. Password is hashed before storage."""
        global _next_id
        logger.debug("UserService.create(%s, %s)", name, email)
        user = {"id": _next_id, "name": name, "email": email, "password": _hash_password(password)}
        FAKE_DB.append(user)
        _next_id += 1
        return {"id": user["id"], "name": user["name"], "email": user["email"]}

    def delete_user(self, user_id: int):
        """Delete a user by ID. Returns the deleted user info or None if not found."""
        for i, user in enumerate(FAKE_DB):
            if user["id"] == user_id:
                removed = FAKE_DB.pop(i)
                logger.debug("UserService.delete_user(%s) -> deleted", user_id)
                return {"id": removed["id"], "name": removed["name"], "email": removed["email"]}
        logger.debug("UserService.delete_user(%s) -> not found", user_id)
        return None

    @staticmethod
    def get_schemas():
        """Return DDL table schemas for database migration."""
        from src.models import SCHEMAS
        return SCHEMAS
