[CACHED] ## Summary — Budget Exhausted Mid-Plan

I explored the codebase sufficiently to understand the fix needed. Here's what I found and what the execution plan would be:

### Current State

| File | Issue |
|---|---|
| `_test_project/src/auth/service.py` (line 47) | `if user.get("password") != password:` — plaintext comparison against stored plaintext |
| `_test_project/src/user_service.py` (lines 6-10) | `FAKE_DB` stores passwords in plaintext (`"pass123"`, `"secret456"`, `"qwerty"`) |
| `_test_project/config.py` | Contains `SECRET_KEY` (already imported by `service.py` on line 3) |
| `_test_project/src/auth/token.py` | Already uses `hashlib.sha256` — same module can be reused for password hashing |

### What Was Not Done (budget limit)

I was unable to read `_test_project/config.py` to confirm the exact content of `SECRET_KEY`, and could not execute the fix.

### Execution Plan (if resumed)

1. **Read `_test_project/config.py`** — confirm `SECRET_KEY` value/type.
2. **Modify `_test_project/src/user_service.py`** — hash the passwords in `FAKE_DB` using `hashlib.sha256(password.encode() + SECRET_KEY.encode()).hexdigest()` so stored values match the new scheme. Also update the `create()` method to hash on user creation.
3. **Modify `_test_project/src/auth/service.py`** — in `login()`, hash the provided `password` with the same scheme before comparing:
   ```python
   hashed = hashlib.sha256((password + self._secret_key).encode()).hexdigest()
   if user.get("password") != hashed:
       return None
   ```
4. **Verify** — run any existing tests under `_test_project/` (if they exist) or manually confirm login flow works by checking the logic.

### Constraints

- Use only stdlib `hashlib` (no external bcrypt dependency) since only stdlib is clearly available.
- Use `SECRET_KEY` from config as the salt/pepper to match the suggestion.
- No changes to `verify_token()` in `token.py` — that's a separate issue.