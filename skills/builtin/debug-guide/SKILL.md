---
name: Debug Guide
description: 系统化调试方法论，帮助定位 bug 根因（二分法、日志注入、最小复现）
---

## Systematic Debugging Methodology

### Phase 1: Reproduce
- [ ] Can you reproduce the bug reliably? (exact steps, inputs, environment)
- [ ] What is the expected behavior vs actual behavior?
- [ ] Is it environment-specific? (OS, Python version, dependencies)
- [ ] Minimize: strip away unrelated code until you have the smallest reproducer

### Phase 2: Isolate
Use **binary search** to narrow down the fault location:

1. **Time-based**: `git bisect` — find the commit that introduced the bug
2. **Space-based**: Add assertions at midpoints to narrow which half of the code path fails
3. **Input-based**: Simplify input until you find the minimal trigger

### Phase 3: Diagnose

**Observation tools** (prefer non-invasive first):
- `print()` / `logging.debug()` at suspected locations
- Debugger breakpoints (`import pdb; pdb.set_trace()` or IDE debugger)
- `traceback.print_exc()` for swallowed exceptions
- `repr()` for values that look correct but aren't (whitespace, types)

**Common root causes**:
| Symptom | Likely cause |
|---------|-------------|
| Works locally, fails in CI | Environment diff (env vars, paths, versions) |
| Intermittent failure | Race condition, timing, flaky external service |
| "Impossible" state | Mutation from unexpected caller, shared reference |
| Silent failure | Swallowed exception, empty except clause |
| Wrong output | Off-by-one, wrong variable, stale cache |

### Phase 4: Fix
- Fix the **root cause**, not the symptom
- If adding a workaround, document WHY with a comment
- Add a test that fails before the fix and passes after
- Check for similar bugs in nearby code (same pattern elsewhere?)

### Phase 5: Verify
- [ ] Original reproducer now passes
- [ ] No regressions (run full test suite)
- [ ] Edge cases covered by new tests
- [ ] Clean up any debug prints/logging you added

## Anti-Patterns to Avoid
- Changing code randomly hoping it fixes the issue
- Adding try/except to silence the error without understanding it
- Fixing the test instead of fixing the code
- Assuming the bug is in the library/framework (check your code first)

### For: $ARGUMENTS
Apply this methodology to debug the specific issue described above.