## Plan

### Goal
Add input validation to `_test_project/src/user_router.py` to reject empty usernames and short passwords, closing the current gap in the `do_login()` endpoint (and tightening the `create_user()` endpoint for empty/whitespace-only usernames).

### Constraints
- Only modify `_test_project/src/user_router.py` — **no other files**.
- Do **not** modify the existing validator functions in `_test_project/src/utils/validators.py`.
- Reuse the existing `validate_username` and `validate_password` imports already present.
- Follow the existing error-response pattern: `return jsonify({"error": "..."}), 4xx`.

### Analysis (already performed — read only)
- **`create_user()` (line 48):** Already validates username (3-20 chars) and password (≥6 chars). However, `name = data.get("name", "")` does not strip whitespace, so `"   "` passes the `if not name:` check; it's caught later by `validate_username` regex, but the error message is misleading ("must be 3-20 characters" instead of "name required"). A `.strip()` on the name field makes the empty-check consistent.
- **`do_login()` (line 68):** Only checks that the JSON keys `username` and `password` exist. An empty string `""` or `"   "` for username, or a too-short password, would be passed directly to `login()` without any client-side validation. **This is the main gap.**

### Steps (execution phase)

1. **In `do_login()`** — after the existing keys-exist check (line 72), add two validation guards:
   - Reject empty/blank username: `if not data.get("username", "").strip():` → `400, "username required"`.
   - Reject short password: `if not data.get("password", "") or len(data["password"]) < 6:` → `400, "password must be at least 6 characters"`.
   *(The 6-char minimum matches the existing `validate_password` used in `create_user`.)*

2. **In `create_user()`** — change `name = data.get("name", "")` to `name = (data.get("name") or "").strip()` so that whitespace-only usernames are caught by the early `if not name:` guard and return the correct `"name required"` message instead of falling through to `validate_username`.

### Verification
- Read `_test_project/src/user_router.py` to confirm the edits are present.
- Run the existing project test suite to ensure no regressions.
- No new tests are needed unless the user requests them (no existing test file was found for this router).