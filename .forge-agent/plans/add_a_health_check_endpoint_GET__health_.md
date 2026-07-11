Here's my plan.

### Goal
Add a `GET /health` endpoint to `_test_project/src/user_router.py` that returns server uptime (in seconds) and total request count served via that blueprint.

### Constraints
- The existing `/health` route in `app.py` (line 19-22, returns `{"status": "ok"}`) must remain untouched — no conflicts.
- Since `user_bp` is registered with `url_prefix="/api/users"` in `app.py`, the new endpoint will actually live at `/api/users/health`. The user's "GET /health" refers to the route *definition* in the file, not the final URL path.
- Do not touch any other files unless a test or import break is discovered.

### Steps
1. **Edit `_test_project/src/user_router.py`:**
   - Add `import time` (or merge into the existing imports).
   - Add module-level `_start_time = time.time()` for uptime tracking.
   - Add module-level `_request_counter = 0` and a `@user_bp.before_request` handler that increments it (so every request to the blueprint is counted).
   - Add a new route `@user_bp.route("/health", methods=["GET"])` with public access (no `@require_auth`) that returns:
     ```json
     {"uptime": <seconds_since_start>, "request_count": <counter_value>}
     ```

2. **Verify no regressions:** Run the project's existing tests (e.g., `pytest tests/` or any test file covering user_router) to ensure existing functionality is not broken.

### Verification
- Run the test suite. Exit code 0 confirms no regressions.
- Optionally, if a quick manual check is safe, start the Flask dev server and hit `GET /api/users/health` to confirm it returns both `uptime` (float) and `request_count` (int).