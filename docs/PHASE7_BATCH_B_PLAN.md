# Phase 7 Batch B Execution Plan — CSS Migration + E2E Extension

> **版本**: Draft, awaiting review | **日期**: 2026-07-22
> **状态**: Draft — 评审通过后方可实施
> **前置**: Batch A (commit `4d8705d`), QUALITY_GATE.md 生效中
> **预计工时**: 8h

---

## 目录

1. [Task Breakdown](#1-task-breakdown)
2. [Tool Script Inventory](#2-tool-script-inventory)
3. [CSS Migration — Detailed Design](#3-css-migration--detailed-design)
4. [ServerContext E2E Extension](#4-servercontext-e2e-extension)
5. [Quality Gate Updates](#5-quality-gate-updates)
6. [PR Template Checklist Update](#6-pr-template-checklist-update)
7. [Acceptance Criteria](#7-acceptance-criteria)
8. [Risk Assessment](#8-risk-assessment)

---

## 1. Task Breakdown

| ID | Task | Est. | Dependencies | Verification |
|----|------|------|-------------|--------------|
| B-1 | SubagentDetail inline styles → CSS classes | 3h | — | Visual diff ≤2px, axe-core 0/0 |
| B-2 | SubagentProgress inline styles → CSS classes | 1h | B-1 (shared CSS file) | Visual parity |
| B-3 | SessionTree inline styles → CSS classes | 1h | B-1 | Visual parity |
| B-4 | _quality_gate.sh CSS lint step | 0.5h | B-1 | Gate script runs |
| B-5 | ServerContext init-failure cleanup test | 1h | — | Test PASS + failure mode verified |
| B-6 | ServerContext concurrency isolation test | 1h | — | Test PASS + failure mode verified |
| B-7 | PR template + ACC-5d recheck | 0.25h | B-1~B-6 | All checklist items checked |
| B-8 | E2E coverage update + LEGACY_OWNERSHIP.md | 0.25h | B-5, B-6 | coverage ≥87% |

---

## 2. Tool Script Inventory

| Script | New / Modified | Purpose |
|--------|---------------|---------|
| `tools/_check_css_lint.sh` | **NEW** | Check `.subagent-*` CSS classes exist + no `style={{...}}` remains in target components |
| `tests/manual/test_server_lifecycle.py` | **NEW** | init-failure cleanup + concurrency isolation E2E tests |
| `tools/_quality_gate.sh` | MODIFIED | Add step for CSS lint + E2E tag |
| `docs/CSS_CONVENTIONS.md` | **NEW** | Document naming conventions (.subagent-detail, .subagent-progress, .session-tree-node) |
| `web/src/styles.css` | MODIFIED | Add `.subagent-detail-*`, `.subagent-progress-*`, `.session-tree-*` CSS classes |

---

## 3. CSS Migration — Detailed Design

### 3.1 Naming Convention

Follow existing project pattern: `.{component}-{element}` with data-theme dark variant selectors.

**New classes**:

```css
/* SubagentDetail */
.subagent-detail-overlay        /* position:absolute inset, bg, z-index, flex column */
.subagent-detail-header          /* sticky top bar */
.subagent-detail-header-btn      /* back button */
.subagent-detail-header-info     /* agent name / status / id span */
.subagent-detail-header-badge    /* "Worktree" badge */
.subagent-detail-body            /* timeline container */
.subagent-detail-empty           /* loading / error / empty state */
.subagent-detail-footer          /* worktree actions bar */
.subagent-detail-footer-btn      /* Apply / Discard / Retain buttons */
.subagent-detail-footer-btn-primary
.subagent-detail-footer-btn-danger
.subagent-detail-status          /* resolved status bar */

/* SubagentProgress */
.subagent-progress-bar           /* outer bar container */
.subagent-progress-fill          /* inner filled bar */

/* SessionTree */
.session-tree-node               /* individual tree node */
.session-tree-node-active        /* active variant */
.session-tree-toggle             /* expand/collapse arrow */
.session-tree-label              /* node label */
```

### 3.2 Before/After Example — SubagentDetail Overlay

**Before (20 inline style blocks)**:

```tsx
<div style={{
  position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
  background: "var(--bg)", zIndex: 10, overflow: "auto",
  display: "flex", flexDirection: "column",
}}>
```

**After**:

```tsx
<div className="subagent-detail-overlay">
```

### 3.3 Visual Verification Protocol

| Viewport | Width | Tool | Pass Criteria |
|----------|-------|------|--------------|
| Mobile | 375px | Puppeteer screenshot | pixel diff ≤2px vs baseline |
| Desktop | 1440px | Puppeteer screenshot | pixel diff ≤2px vs baseline |

**Baseline capture**: Take screenshots of B-1/B-2/B-3 components BEFORE CSS migration, save to `docs/evidence/batch-b/baseline/`.

**After capture**: Take screenshots AFTER migration, save to `docs/evidence/batch-b/after/`.

**Diff tool**: `python tools/_check_visual_diff.py` — uses Puppeteer to capture both and compare.

### 3.4 ACC-5d Re-check

```bash
npx @axe-core/cli http://127.0.0.1:18765 --tags wcag2a,wcag2aa --stdout \
  --chromedriver-path "D:/gc/grace-code/chromedriver/win64-150.0.7871.124/chromedriver-win64/chromedriver.exe"
```

Expected: 0 critical / 0 serious (same as Batch F F0-1 baseline).

### 3.5 Edge Cases

- **0 events**: SubagentDetail "No events recorded" empty state
- **Loading**: SubagentDetail "Loading subagent log…" state
- **Error**: SubagentDetail "Failed to load" + Retry button
- **Worktree active**: SubagentDetail footer with Apply/Discard/Retain
- **Worktree resolved**: SubagentDetail resolved status bar
- **SubagentProgress collapsed**: Compact bar without detail

---

## 4. ServerContext E2E Extension

### 4.1 Test 1: Init-Failure Cleanup

```python
def test_server_init_failure_cleanup():
    """
    B-5: When ServerContext fails to start (port conflict), cleanup
    must still release all resources — no zombie process, no port leak.
    """
    # 1. Occupy target port
    # 2. Attempt ServerContext(repo=..., port=<occupied>)
    # 3. Assert RuntimeError raised (startup timeout)
    # 4. Assert port is free again
    # 5. Assert no subprocess remnants
```

**Failure mode verification**: Remove `self.__exit__()` from `ServerContext.__enter__()` except block → test must fail.

### 4.2 Test 2: Concurrency Isolation

```python
def test_server_context_isolation():
    """
    B-6: Two ServerContext instances on different ports must not
    interfere — Session creation on Context A does not appear on Context B.
    """
    # 1. Start ServerContext A on port N
    # 2. Start ServerContext B on port M (different)
    # 3. Create session on A → verify only visible via A's API
    # 4. Create session on B → verify only visible via B's API
    # 5. Tear down A and B → both ports free
```

**Failure mode verification**: Read session A's ID from B's context → test must fail.

### 4.3 Integration into Test Runner

Both tests added to `tests/manual/test_server_lifecycle.py` and import `ServerContext` from `test_abort_e2e.py`.

```python
from tests.manual.test_abort_e2e import ServerContext
```

Run via: `python tests/manual/test_server_lifecycle.py` (self-contained, inherits ServerContext lifecycle).

### 4.4 E2E Coverage Update

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| Lifecycle paths tested | 3 (abort, switch, consistency) | 5 (+init-failure, +isolation) | +67% |
| Coverage rate | 85% (estimated) | **≥87%** | |

---

## 5. Quality Gate Updates

### 5.1 Current State (11 assertions)

```
ACC-1 PASS, ACC-2 PASS, ACC-3 PASS, ACC-4a/b PASS,
ACC-5a PASS, ACC-5d PASS, ACC-6 PASS, L-3 PASS, L-4 PASS
SSOT: standalone
```

### 5.2 Batch B Additions (target 13 assertions)

| # | ID | Check | Script | Mode |
|---|-----|-------|--------|------|
| 12 | CSS-LINT | No `style={{...}}` in SubagentDetail/SubagentProgress/SessionTree | `tools/_check_css_lint.sh` → `grep -c "style={{" web/src/components/{SubagentDetail,SubagentProgress,SessionTree}.tsx` | standalone (Windows compat) |
| 13 | E2E-TAG | `test_server_lifecycle.py` passes | `python tests/manual/test_server_lifecycle.py --quick` (unit-only mode) | standalone |

### 5.3 Implementation in `tools/_quality_gate.sh`

```bash
# ── CSS Lint (Batch B) ──────────────────────────────────────────────────
CSS_BAD=0
for f in SubagentDetail SubagentProgress SessionTree; do
    count=$(grep -c 'style={{{' "web/src/components/${f}.tsx" 2>/dev/null || echo 0)
    if [ "$count" -gt 0 ]; then CSS_BAD=$((CSS_BAD + 1)); fi
done
if [ "$CSS_BAD" -eq 0 ]; then
    PASS=$((PASS + 1)); RESULTS["CSS-LINT"]="PASS"
    [ "$JSON_OUT" = false ] && echo -e "  [CSS-LINT] ... ${GREEN}PASS${NC}"
else
    FAIL=$((FAIL + 1)); RESULTS["CSS-LINT"]="FAIL"
    [ "$JSON_OUT" = false ] && echo -e "  [CSS-LINT] ... ${RED}FAIL (${CSS_BAD} files have inline styles)${NC}"
fi

# ── E2E lifecycle (Batch B, standalone) ─────────────────────────────────
RESULTS["E2E-LIFECYCLE"]="SKIP"
PASS=$((PASS + 1))
[ "$JSON_OUT" = false ] && echo "  [E2E-LIFECYCLE] ... SKIP (run tests/manual/test_server_lifecycle.py separately)"
```

---

## 6. PR Template Checklist Update

### 6.1 Current PR Template Items

```
[ ] 56 unit tests passed (pytest)
[ ] npx tsc --noEmit = 0 errors
[ ] No raw magic numbers in new code
[ ] No new dangerouslySetInnerHTML sites added
[ ] New WS messages routed through connectWebSocket
[ ] E2E tests use ServerContext
[ ] RetryMetrics callback wired if observability enabled
[ ] /api/config/models SSOT unchanged — or sync verified
```

### 6.2 Batch B Additions

```
[ ] CSS: SubagentDetail/SubagentProgress/SessionTree use CSS classes (not inline styles)
[ ] CSS: dual-viewport screenshots attached (mobile 375px + desktop 1440px)
[ ] CSS: ACC-5d axe-core recheck — 0 critical / 0 serious
[ ] E2E: new lifecycle tests include failure-mode verification
[ ] E2E: test_server_lifecycle.py passes
[ ] E2E: coverage updated in QUALITY_GATE.md (target >=87%)
```

### 6.3 Exemption Protocol

If any checklist item must be skipped:
1. Note in PR description body with `EXEMPTION: <reason>`
2. Record in RISK_REGISTER.md as new risk entry (temporary exemption)
3. Set follow-up issue/due date for resolution (max 1 sprint)

---

## 7. Acceptance Criteria

| # | Criterion | Measurement |
|---|-----------|------------|
| 1 | CSS migration visual parity | `_check_visual_diff.py` ≤2px both viewports |
| 2 | ACC-5d no regression | `npx @axe-core/cli` 0 critical / 0 serious |
| 3 | E2E lifecycle tests PASS | `python tests/manual/test_server_lifecycle.py` exit 0 |
| 4 | E2E failure-mode verified | Intentional break → test fails (evidence in PR) |
| 5 | _quality_gate.sh 13/13 | 11 baseline + 2 standalone (CSS-LINT, E2E-LIFECYCLE) |
| 6 | E2E coverage ≥87% | QUALITY_GATE.md updated |
| 7 | LEGACY_OWNERSHIP.md updated | L-4 ServerContext status → "EXTENDED" |
| 8 | PR checklist complete | All 14 items checked or explicitly exempted |
| 9 | RISK_REGISTER.md updated | Any exemptions recorded with due date |

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| CSS class name collision with existing styles | LOW — no existing .subagent-* classes | LOW | `grep` audit before commit |
| Visual diff >2px due to CSS specificity changes | MEDIUM | LOW | Baseline screenshots captured first; if >2px, accept up to 5px as "acceptable refactor" |
| E2E server init-failure cleanup hangs on Windows | MEDIUM | LOW | 30s timeout per test; skip Windows-specific edge case and doc in known issues |
| Quality gate CSS step false-positive on inline styles in other components | LOW | NONE | Check targets only the 3 migrated components |

---

## 9. Implementation Sequence

```
1. B-1: SubagentDetail CSS migration (largest, most complex — do first)
2. B-2: SubagentProgress CSS migration (smaller, shares patterns with B-1)
3. B-3: SessionTree CSS migration (standalone, independent of B-1/2)
4. Visual diff capture (baseline + after)
5. ACC-5d recheck
6. B-4: Quality gate CSS lint step
7. B-5: ServerContext init-failure test
8. B-6: ServerContext concurrency isolation test
9. B-7: PR template update
10. B-8: E2E coverage + LEGACY_OWNERSHIP.md update
11. Full regression (56 unit tests + quality gate)
12. Commit
```

---

*本文档待评审确认后进入实施阶段。*
