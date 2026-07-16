[CACHED] Here is the execution plan:

## Goal

Add a `POST /logout` endpoint to `_test_project/src/user_router.py` that revokes the caller's Bearer token from the session store, effectively logging them out.

## Constraints

- Must use the existing `revoke_token` function from `src.auth` (already exposed in `src/auth/__init__.py`)
- Extract the token from the `Authorization: Bearer <token>` header (same pattern as `require_auth` middleware)
- The endpoint should require authentication (`@require_auth`) so a valid token is present to revoke
- Do NOT modify any other files — only `_test_project/src/user_router.py`

## Steps

1. **Read `user_router.py`** to confirm the current import block and end of file.
2. **Add the new endpoint** after the existing `/login` route (around line 81):
   - Route: `@user_bp.route("/logout", methods=["POST"])`
   - Decorators: `@handle_errors`, `@require_auth`
   - Extract the Bearer token from `request.headers.get("Authorization", "")` (strip `"Bearer "` prefix)
   - Call `from src.auth import revoke_token` (add to existing import line)
   - Call `revoke_token(token)`
   - Return `jsonify({"message": "logged out"})`, 200

## Verification

- Run any existing tests in `_test_project/tests/` related to auth or user routes
- Visually confirm the new route is properly decorated and follows the same patterns as `do_login`