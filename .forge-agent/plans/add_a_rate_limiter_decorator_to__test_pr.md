[CACHED] ### Goal
Add a `rate_limit` decorator to `_test_project/src/middleware/__init__.py` that enforces a sliding-window limit of **5 requests per minute per IP address**, returning a JSON 429 response when exceeded.

---

### Constraints
- Must use `request.remote_addr` (Flask) to identify the client IP.
- Must use a **sliding window** (not fixed 60-second buckets) so bursts at the boundary are handled fairly.
- Must return `{"error": "rate limit exceeded"}` with HTTP 429 when over the limit.
- Must be a decorator (`@rate_limit`) usable on Flask route functions, like the existing `@handle_errors`.
- Must be exported (importable as `from middleware import rate_limit`).

---

### Steps

1. **Edit `_test_project/src/middleware/__init__.py`:**
   - Add `import time` and `from flask import request` to the imports section.
   - Add a `rate_limit` decorator function (after `handle_errors`):
     - Maintain an in-memory `defaultdict(list)` mapping IP → list of request timestamps.
     - On each invocation: get `request.remote_addr`, prune timestamps older than 60 s, check count < 5, either append and proceed or return 429.
     - Use `functools.wraps` to preserve the wrapped function's metadata.
   - Ensure `rate_limit` is available at the package level (no additional `__all__` needed since it's a module-level name).

2. **No other files need modification.** The existing `handle_errors` and `require_auth` exports are untouched.

---

### Verification
1. **Unit test (optional):** If `_test_project` has a test suite for middleware, run `pytest _test_project/tests/` to confirm nothing is broken.
2. **Import check:** Verify `from middleware import rate_limit` resolves.
3. **Syntax check:** Run `python -c "import ast; ast.parse(open('_test_project/src/middleware/__init__.py').read())"` to confirm valid syntax.