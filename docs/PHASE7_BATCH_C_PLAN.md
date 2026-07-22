# Phase 7 Batch C Execution Plan — Quality Gate Closure + Visual Baseline + Observability Endpoint Verification

> **Version**: Draft, awaiting review | **Date**: 2026-07-22
> **Status**: Draft — review gate before implementation
> **Predecessor**: Batch B (commit `4e9dcf1`)
> **Estimated**: 6h

---

## 1. Task Breakdown

| ID | Task | Est. | Dependencies | Verification |
|----|------|------|-------------|--------------|
| C-1 | CSS-LINT: migrate from standalone to main gate (assertion #12) | 0.5h | B-1/2/3 | `_quality_gate.sh` blocks on `style={{` in migrated components |
| C-2 | E2E-LIFECYCLE: migrate from standalone to main gate (assertion #13) | 0.5h | B-5/6 | `_quality_gate.sh` blocks on `test_server_lifecycle.py --quick` fail |
| C-3 | Visual baseline: `_check_visual_diff.py` + gate integration (assertion #14) | 2h | B-1/2/3 | Puppeteer screenshot diff ≤2px vs committed baseline |
| C-4 | Langfuse health check: `_verify_langfuse_endpoint.sh` + gate integration (assertion #15, conditional) | 1h | Batch A (retry_tracer.py) | Health API 200, `FORGE_OBSERVE_RETRIES=1` gated |
| C-5 | PR template update: 8→15 items | 0.25h | C-1→C-4 | All new items have tooling scripts |
| C-6 | Docs sync: QUALITY_GATE.md + LEGACY_OWNERSHIP.md final | 0.25h | C-5 | Both docs reflect C-final state |
| C-7 | R-5 future assessment | 0.25h | — | Recorded in RISK_REGISTER.md |
| C-8 | Full regression + quality gate 15/15 check | 0.25h | C-1→C-7 | All gates green |

---

## 2. Tool Script Inventory

| Script | New/Modified | Purpose | Gate # |
|--------|-------------|---------|--------|
| `tools/_check_css_lint.sh` | MODIFIED | Reject inline styles + validate .subagent-* / .session-tree-* naming convention | #12 |
| `tools/_check_visual_diff.py` | **NEW** | Puppeteer screenshot diff vs `tests/visual-baselines/` ≤2px per viewport | #14 |
| `tools/_verify_langfuse_endpoint.sh` | **NEW** | `curl` Langfuse Health API, exit 0 on 200 | #15 (conditional) |
| `tests/manual/test_server_lifecycle.py` | MODIFIED | Add `--quick` tag for gate CI (≤30s timeout) | #13 |
| `tools/_quality_gate.sh` | MODIFIED | +4 assertions (12-15), JSON schema update | — |

---

## 3. Gate Assertion Integration Order

```
Current (Batch B): 11 assertions (ACC-1~6, L-3, L-4) + SSOT standalone

Step C-1: integrate CSS-LINT (standalone -> inline)
  → assertion #12: CSS-LINT (blocks on failure)
  → test: intentional inline style added -> gate expected to block

Step C-2: integrate E2E-LIFECYCLE (standalone -> inline)
  → assertion #13: E2E-LIFECYCLE (blocks on failure, ≤30s)
  → test: break B-5 assertion -> gate expected to block

Step C-3: add visual diff gate
  → assertion #14: VISUAL-DIFF (Uses Puppeteer, compares against tests/visual-baselines/)
  → UPDATE_BASELINE=1 escape hatch with auto-issue creation

Step C-4: add Langfuse health check
  → assertion #15: LANGFUSE-HEALTH (conditional: only when FORGE_OBSERVE_RETRIES=1)
  → SKIP when env var absent — no false-positive on local dev
```

---

## 4. Visual Baseline Design (`tools/_check_visual_diff.py`)

### 4.1 Script Behavior

```python
"""
Check visual regression against committed baselines.

Captures screenshot at configurable viewport, compares pixel-by-pixel
against tests/visual-baselines/<viewport>.png.  Fails with >2px diff.

UPDATE_BASELINE=1: overwrite baseline and exit 0 (creates follow-up issue).
"""
```

### 4.2 Gate Entry in `_quality_gate.sh`

```bash
# ── Visual Diff (Batch C) ────────────────────────────────────────────────
VISUAL_OK=0
if [ "${UPDATE_BASELINE:-}" = "1" ]; then
    python tools/_check_visual_diff.py --update > /dev/null 2>&1
    echo "  [VISUAL-DIFF] ... SKIP (baseline updated)"
    PASS=$((PASS + 1))
    RESULTS["VISUAL-DIFF"]="UPDATED"
else
    set +e
    python tools/_check_visual_diff.py > /dev/null 2>&1; _vd=$?
    set -e
    if [ "$_vd" -eq 0 ]; then
        PASS=$((PASS + 1)); RESULTS["VISUAL-DIFF"]="PASS"
        [ "$JSON_OUT" = false ] && echo -e "  [VISUAL-DIFF] ... ${GREEN}PASS${NC}"
    else
        FAIL=$((FAIL + 1)); RESULTS["VISUAL-DIFF"]="FAIL"
        [ "$JSON_OUT" = false ] && echo -e "  [VISUAL-DIFF] ... ${RED}FAIL (run UPDATE_BASELINE=1 if changes are intentional)${NC}"
    fi
fi
```

### 4.3 Baseline Management

| File | Description |
|------|------------|
| `tests/visual-baselines/subagent-desktop-1440.png` | Desktop reference (1440x900) |
| `tests/visual-baselines/subagent-mobile-375.png` | Mobile reference (375x812) |
| `tests/visual-baselines/README.md` | Update protocol: `UPDATE_BASELINE=1 bash tools/_quality_gate.sh` + follow-up issue |

### 4.4 CI Compatibility

- Uses Puppeteer (already a devDependency from Batch F axe-core scan)
- Baseline images committed to repo (3 KB each, not requiring LFS)
- Windows compatibility: `--no-sandbox` flag passed to Puppeteer in CI

---

## 5. Langfuse Endpoint Script

```bash
#!/bin/bash
# _verify_langfuse_endpoint.sh — health-check Langfuse connectivity

if [ "${FORGE_OBSERVE_RETRIES:-0}" != "1" ]; then
    echo "Langfuse health: SKIP (FORGE_OBSERVE_RETRIES not set)"
    exit 0
fi

LANGFUSE_HOST="${LANGFUSE_BASE_URL:-https://cloud.langfuse.com}"

if curl -sf --max-time 5 "${LANGFUSE_HOST}/api/public/health" > /dev/null 2>&1; then
    echo "Langfuse health: PASS (${LANGFUSE_HOST})"
    exit 0
else
    echo "Langfuse health: FAIL (${LANGFUSE_HOST} unreachable)"
    exit 1
fi
```

Gate entry:

```bash
# ── Langfuse Health (Batch C) ──────────────────────────────────────────
if [ "${FORGE_OBSERVE_RETRIES:-0}" = "1" ]; then
    if bash tools/_verify_langfuse_endpoint.sh > /dev/null 2>&1; then
        PASS=$((PASS + 1)); RESULTS["LANGFUSE"]="PASS"
        [ "$JSON_OUT" = false ] && echo -e "  [LANGFUSE] ... ${GREEN}PASS${NC}"
    else
        FAIL=$((FAIL + 1)); RESULTS["LANGFUSE"]="FAIL"
        [ "$JSON_OUT" = false ] && echo -e "  [LANGFUSE] ... ${RED}FAIL${NC}"
    fi
else
    RESULTS["LANGFUSE"]="SKIP"
    PASS=$((PASS + 1))
    [ "$JSON_OUT" = false ] && echo "  [LANGFUSE] ... SKIP (FORGE_OBSERVE_RETRIES=0)"
fi
```

---

## 6. PR Template Checklist — Final State (15 items)

```
### Pre-merge Checklist
[ ] 56 unit tests passed (pytest)
[ ] npx tsc --noEmit = 0 errors
[ ] No raw magic numbers in new code (agent/core.py check)
[ ] No new dangerouslySetInnerHTML sites added
[ ] New WS messages routed through connectWebSocket (not raw WebSocket)
[ ] E2E tests use ServerContext (not standalone subprocess)
[ ] RetryMetrics callback wired if observability enabled
[ ] /api/config/models SSOT unchanged — or sync verified
[ ] CSS: SubagentDetail/SubagentProgress/SessionTree use CSS classes (not inline styles)
[ ] CSS: VISUAL-DIFF passes (run: UPDATE_BASELINE=1 if intentional design change)
[ ] CSS: ACC-5d axe-core 0 critical / 0 serious
[ ] E2E: new lifecycle tests include failure-mode verification
[ ] E2E: test_server_lifecycle.py passes
[ ] LANGFUSE: endpoint verified (FORGE_OBSERVE_RETRIES=1)
[ ] COVERAGE: E2E >=87%
```

---

## 7. RISK_REGISTER.md R-5 Update

### R-5: SessionTree Dynamic Inline Styles (Phase 7 Batch C Amendment)

> **Future static-ization assessment**: CSS-in-JS migration or CSS custom properties (`--session-depth: none`) could replace `marginLeft: depth * 12`. Estimated effort: 2h for CSS variables approach. Deferred to Phase 8 — low priority.

---

## 8. QUALITY_GATE.md Update — F-1 Gate Revision

| Old | New | Assertion Count |
|-----|-----|-----------------|
| ACC-1~6 + L-3 + L-4 + SSOT(standalone) | ACC-1~6 + L-3 + L-4 + CSS-LINT + E2E-LIFECYCLE + VISUAL-DIFF + LANGFUSE(conditional) + SSOT(standalone) | 11 → **15** |

---

## 9. Acceptance Criteria

| # | Criterion | Measurement |
|---|-----------|------------|
| 1 | `_quality_gate.sh` `--json` output structure includes 15 keys | `python -c "import json; j=json.load(open(...))"` passes |
| 2 | Gate blocks on CSS-LINT violation | Add `style={{` → gate FAIL |
| 3 | Gate blocks on E2E-LIFECYCLE break | Break B-5 assertion → gate FAIL |
| 4 | `UPDATE_BASELINE=1` overwrites baseline + exits 0 | File mtime updated, gate passes |
| 5 | Langfuse health conditional | `FORGE_OBSERVE_RETRIES=1` → gate checks endpoint; `FORGE_OBSERVE_RETRIES=0` → SKIP |
| 6 | PR template has all 15 items | Check PULL_REQUEST_TEMPLATE.md |
| 7 | R-5 amended with future assessment | CRLF documented in RISK_REGISTER.md |
| 8 | QUALITY_GATE.md + LEGACY_OWNERSHIP.md sync to C-final state | Diff shows all changes |

---

## 10. Implementation Sequence

```
1. C-1: CSS-LINT migration (modify _check_css_lint.sh + _quality_gate.sh)
2. C-2: E2E-LIFECYCLE migration (add --quick tag + gate entry)
3. C-3: Visual baseline (write _check_visual_diff.py + gate entry)
4. C-4: Langfuse endpoint (write _verify_langfuse_endpoint.sh + gate entry)
5. C-5: PR template update (15 items)
6. C-6: Docs sync (QUALITY_GATE.md, LEGACY_OWNERSHIP.md)
7. C-7: R-5 amendment
8. C-8: Full regression (56 tests + quality gate 15/15 run)
9. Commit
```

---

*This plan awaits review sign-off before implementation.*
