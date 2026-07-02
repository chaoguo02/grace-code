Now I have a clear picture of the codebase. Let me produce the plan.

---

### Current Architecture Summary

| File | Role |
|---|---|
| `src/auth.py` | Monolithic module: `AuthService` class (token gen/verify, login, revoke, session mgmt) + module-level singleton + convenience functions |
| `src/middleware/require_auth.py` | Cross-cutting decorator, already moved to middleware. Imports `verify_token` from `src.auth` |
| `src/user_router.py` | Flask routes. Imports `login` from `src.auth` |
| `src/models.py` | Already has `SESSION_TABLE` DDL schema (not yet used by auth) |
| `src/user_service.py` | Service layer for user CRUD |

### Goal

Refactor the monolithic `src/auth.py` module into a **package** (`src/auth/`) with separated concerns:
- Token generation/verification → `token.py`
- Session management → `session.py`
- Auth orchestration (login) → `service.py`
- Backward-compatible public API → `__init__.py`

All existing import paths (`from src.auth import verify_token`, `from src.auth import login`) must continue to work.

### Constraints

1. **Backward compatibility** — `from src.auth import verify_token, login, AuthService, generate_token, etc.` must all still work.
2. **No external API changes** — The `AuthService` class interface, module-level singleton `auth_service`, and module-level convenience functions must remain accessible from `src.auth`.
3. **Middleware stays in place** — `require_auth` remains in `src/middleware/require_auth.py` (import from `src.auth` as-is).
4. **Minimal diff** — Only refactor the auth module; do not change other files unless imports break.

### Steps

1. **Create `_test_project/src/auth/` directory** with:
   - `__init__.py` — import and re-export all public symbols (`AuthService`, `auth_service`, `generate_token`, `verify_token`, `login`, `revoke_token`, `cleanup_expired`, `get_active_sessions`) so `from src.auth import X` continues to work unchanged.
   - `token.py` — extract `generate_token()` and `verify_token()` as pure functions (or a `TokenService` class). These handle the hash-based token creation and validation.
   - `session.py` — extract the `SessionManager` (in-memory `self._sessions` dict + expiry logic + `revoke_token`, `cleanup_expired`, `get_active_sessions`).
   - `service.py` — extract the `AuthService` class that wires `TokenService`, `SessionManager`, and the `login()` orchestration together.

2. **Remove old `_test_project/src/auth.py`**.

3. **Update `__init__.py`** to preserve the module-level singleton `auth_service` and the convenience functions that delegate to it.

### Verification (to be done in execution phase)

1. Confirm `from src.auth import verify_token` works (used by `require_auth.py`).
2. Confirm `from src.auth import login` works (used by `user_router.py`).
3. Confirm `from src.auth import AuthService, auth_service, generate_token, revoke_token, cleanup_expired, get_active_sessions` all resolve.
4. Run the project's test suite if any exists (none found — but check for `pyproject.toml` test config).

### Deliverable

A `_test_project/src/auth/` package that is a drop-in replacement for the old `_test_project/src/auth.py` — zero changes needed in any consumer file.