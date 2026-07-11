## Plan

### Goal
Add a `log_request_timing` decorator function to `_test_project/src/middleware/__init__.py` that measures and logs the duration of each request.

### Constraints
- Follow the existing decorator patterns in the file (`rate_limit`, `handle_errors`).
- Use only existing imports (`functools`, `logging`, `time`, `flask` modules already imported).
- Do NOT modify any other files (no routes, no app.py, no user_router.py).
- Do NOT create new files.
- The decorator should be reusable with `@log_request_timing` annotation.

### Steps
1. Read the current state of `_test_project/src/middleware/__init__.py` (already done above).
2. Add a `log_request_timing` decorator function after the existing `handle_errors` function.
3. The decorator will:
   - Record `time.time()` before calling the wrapped function.
   - Call the wrapped function and capture its return value.
   - Log the HTTP method, path, response status code, and elapsed time (in seconds) using `logger.info`.
   - Return the response unchanged.

### Verification
- Run `pytest _test_project/tests/` if tests exist (none found — no tests will be run).
- Verify the syntax is valid by checking the file can be imported without error: `python -c "from src.middleware import log_request_timing"` from within `_test_project/`.
- Check that `log_request_timing` follows the same pattern as `rate_limit` and `handle_errors`.

```json
{
  "objective": "Add a request timing middleware decorator to the middleware package that logs request duration per endpoint call.",
  "target_files": ["_test_project/src/middleware/__init__.py"],
  "expected_behavior": "The middleware file will export a new `log_request_timing` decorator that wraps a Flask route handler, measures its execution time, and logs method, path, status code, and duration via `logger.info`.",
  "verification_strategy": "python -c \"from src.middleware import log_request_timing; print('OK')\" from _test_project/ directory",
  "potential_conflicts": []
}
```