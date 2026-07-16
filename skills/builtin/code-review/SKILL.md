---
name: Code Review
description: 代码审查清单与最佳实践，帮助识别 bug、性能问题和可维护性问题
---

## Code Review Checklist

### Correctness
- [ ] Logic errors: off-by-one, null/None handling, edge cases
- [ ] Error handling: exceptions caught, resources cleaned up (files, connections)
- [ ] Boundary conditions: empty input, single element, max size
- [ ] Concurrency: race conditions, deadlocks, shared state mutations
- [ ] Type safety: implicit conversions, unchecked casts

### Security
- [ ] Input validation: user input sanitized before use
- [ ] Injection: SQL, command, path traversal, XSS
- [ ] Authentication/Authorization: proper checks on all paths
- [ ] Secrets: no hardcoded keys, passwords, tokens
- [ ] Dependencies: known vulnerabilities in imported packages

### Performance
- [ ] Algorithmic complexity: unnecessary O(n^2) when O(n) is possible
- [ ] Database: N+1 queries, missing indexes, unbounded result sets
- [ ] Memory: large allocations in loops, unbounded caches
- [ ] I/O: synchronous blocking calls that should be async
- [ ] Unnecessary work: redundant computations, repeated parsing

### Maintainability
- [ ] Naming: variables/functions clearly describe purpose
- [ ] Single responsibility: each function does one thing
- [ ] DRY: no copy-paste code that should be extracted
- [ ] Coupling: changes to one module don't ripple unnecessarily
- [ ] Tests: new logic has corresponding test coverage

### Style
- [ ] Consistent with surrounding code conventions
- [ ] No dead code, commented-out blocks, or TODO without context
- [ ] Error messages are actionable (say what went wrong AND what to do)

## Review Process

1. **Read the diff** — understand what changed and why
2. **Check the test plan** — are edge cases covered?
3. **Run the tests** — verify they pass
4. **Look for patterns** — apply checklist above
5. **Summarize findings** — group by severity (critical / suggestion / nit)

### For: $ARGUMENTS
Focus your review on the specific code/module described above. Prioritize critical issues over style nits.