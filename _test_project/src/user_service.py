"""User business logic and data access."""
import logging

logger = logging.getLogger(__name__)

FAKE_DB = [
    {"id": 1, "name": "alice", "email": "alice@example.com", "password": "pass123"},
    {"id": 2, "name": "bob", "email": "bob@example.com", "password": "secret456"},
    {"id": 3, "name": "charlie", "email": "charlie@example.com", "password": "qwerty"},
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

    def delete_user(self, user_id: int):
        """Delete a user by ID. Returns True if deleted, False if not found."""
        logger.info("UserService.delete_user(%s)", user_id)
        for i, user in enumerate(FAKE_DB):
            if user["id"] == user_id:
                del FAKE_DB[i]
                return True
        return False

    def create(self, name: str, email: str, password: str):
        """Create a new user."""
        global _next_id
        logger.info("UserService.create(%s, %s)", name, email)
        user = {"id": _next_id, "name": name, "email": email, "password": password}
        FAKE_DB.append(user)
        _next_id += 1
        return {"id": user["id"], "name": user["name"], "email": user["email"]}
