# Grace-Code PR Checklist (Phase 7 Batch C — 15 items)

Verify each item before merging.  Every item has an automated verification path
— do NOT check items manually without running the listed command.

---

## Quality Gate

- [ ] 56 unit tests passed
  ```bash
  python -m pytest tests/test_cli_web_alignment.py tests/test_e2e_core.py tests/test_memory_api.py -q -m "not e2e"
  ```

- [ ] tsc --noEmit = 0 errors
  ```bash
  cd web && npx tsc --noEmit
  ```

- [ ] Code quality gate passed
  ```bash
  bash tools/_quality_gate.sh
  ```

## Security & Architecture

- [ ] No raw magic numbers in new code
  ```bash
  grep -nE '[^a-zA-Z]3000[^0-9_]|[^a-zA-Z]8000[^0-9_]' agent/core.py
  ```

- [ ] No new dangerouslySetInnerHTML sites added
  ```bash
  python tools/_check_xss.py
  ```

- [ ] New WS messages routed through connectWebSocket (not raw WebSocket)
  ```bash
  grep -rn 'new WebSocket()' web/src/
  ```

- [ ] /api/config/models SSOT unchanged — or sync verified
  ```bash
  python tools/_check_ssot_all.py
  ```

## CSS & Visual

- [ ] CSS: SubagentDetail/SubagentProgress/SessionTree use CSS classes (not inline styles)
  ```bash
  bash tools/_check_css_lint.sh
  ```

- [ ] CSS: VISUAL-DIFF passes
  ```bash
  # Standard run; if design changed intentionally:
  #   UPDATE_BASELINE=1 bash tools/_quality_gate.sh
  ```

- [ ] CSS: ACC-5d axe-core 0 critical / 0 serious
  ```bash
  npx @axe-core/cli http://127.0.0.1:18765 --tags wcag2a,wcag2aa --stdout
  ```

- [ ] If VISUAL-DIFF skipped: reason documented + R-6 tracked
  ```bash
  VISUAL_DIFF_SKIP=1 bash tools/_quality_gate.sh  # intentional skip only
  ```

## E2E & Observability

- [ ] E2E: new lifecycle tests include failure-mode verification
  ```bash
  python tests/manual/test_server_lifecycle.py --quick
  ```

- [ ] E2E: test_server_lifecycle.py passes
  ```bash
  python tests/manual/test_server_lifecycle.py
  ```

- [ ] LANGFUSE: endpoint verified (if FORGE_OBSERVE_RETRIES=1)
  ```bash
  FORGE_OBSERVE_RETRIES=1 bash tools/_verify_langfuse_endpoint.sh
  ```

- [ ] COVERAGE: E2E >=87%
  ```bash
  # Verify at least 5 lifecycle test functions exist across test scripts
  grep -c 'def test_' tests/manual/test_*.py
  ```
