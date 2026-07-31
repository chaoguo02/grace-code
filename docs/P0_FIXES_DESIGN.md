# P0 Normalization Fixes — Design Decision Records

This document records the **three P0 philosophical fixes** identified in the
Claude Code gap analysis.  Each section is a self-contained Decision Record:
problem → CC reference → our approach → impact → verification.

## 1. P0-#1: Tool Result Compression Pipeline

### Decision Record 1

**Problem — what cognitive/behavioral issue does this solve?**

When a tool produces a long output (Bash build log 50k chars, Read of a
2000-line file, Grep with 500 matches), the model sees one of two bad
outcomes:

| Outcome | Current code path | Model effect |
|---------|------------------|-------------|
| Full output, no truncation | `artifact_store` absent, exempt tool (Read/Bash inline) | Context window explosion, model loses earlier history |
| Head+tail truncation | `truncate_output()` at 8000 chars (line 51, observation_rendering.py) | Middle of output silently lost — exit codes, error lines, file summaries in the middle disappear |

In neither case does the model receive **structured metadata** about what
was lost.  The truncation is character-level, not semantic-level.  The
model cannot distinguish "this file has more content" from "this is the
complete output."  Empty/nil outputs are not explicitly marked — the
model sees `(no output)` only as a fallback, not as a first-class signal.

**CC reference — how does Claude Code solve this?**

CC applies a 5-layer result compression pipeline:

1. Full content (always in storage, never lost)
2. Soft head cap: first 2000 chars preserved, rest in artifact store
3. Structured metadata always survives truncation: exit codes, file lists,
   error lines, command output summaries
4. Empty results get explicit semantic markers: `(No matches found)`,
   `(Empty file)`, `(Permission denied)`
5. The model always knows *what* happened even when it doesn't see *all*
   the output because structural facts are extracted before truncation

**Our approach**

We already have `truncate_output()` (head+tail, 8000 chars) and
`artifact_store.maybe_store()` for large outputs.  The gap is a

single function that extracts structured facts BEFORE truncation
happens, and injects those facts into the truncated output so the model
always receives them regardless of truncation tier.

**Implementation: `compress_tool_result(observation) → str`**

```python
def compress_tool_result(observation: Observation) → str:
    """Extract structured metadata, truncate body, render for model.
    
    Operates at the ``Observation`` level — after ``ToolResult.to_observation()``
    has already mapped raw execution facts to standardized fields.
    """
    facts = _extract_facts(observation)   # exit_code, file_paths, counts, errors
    body = _truncate_body(observation.output, max_chars=COMPRESS_MAX_CHARS)
    return _render_compressed(observation, facts, body)
```

Where:

- `_extract_facts(obs) → ToolResultFacts` reads ONLY structured keys from
  ``Observation.metadata``.  **No regex or string-pattern matching on output
  text is permitted in this function.**  Facts must be populated upstream:

  | Fact | Source | Who populates it |
  |------|--------|-----------------|
  | `exit_code` | `metadata["exit_code"]` | Bash/Shell executor writes on completion |
  | `file_paths` | `modified_files` | All write tools set `result.modified_files` |
  | `match_count` | `metadata["match_count"]` | Grep/Glob executor counts result lines |
  | `error_lines` | `metadata["error_lines"]` | Tool executor extracts before truncation |
  | `summary` | First 200 chars of `output` | Always from output (not metadata) |

  If a tool executor does not populate these metadata keys, that is a
  **defect in the executor** — the render layer must NOT compensate with
  regex heuristics.  Fix the executor, not the renderer.

- `_truncate_body(raw_output, max_chars)` applies tiered truncation:
  - Tier 0 (output ≤ COMPRESS_FULL_CHARS): return as-is
  - Tier 1 (output ≤ COMPRESS_MAX_CHARS): head+tail (current behavior)
  - Tier 2 (output > COMPRESS_MAX_CHARS): head (40%) + tail (40%) + metadata footer (20%).  **Tail is NEVER dropped — critical signals (exit codes, test summaries, stack traces) are almost always at the end.**  No free-text "summary line" is generated; structured facts are the summary.

- `_render_compressed(obs, facts, body)` assembles the final model text:
  ```
  [facts block: exit code, file paths, match count, error summary]
  [body: truncated content]
  [metadata footer: artifact store ID if offloaded]
  ```

**Empty result semantics**

Before: `(no output)` only as fallback.
After: explicit markers derived from `Observation.outcome`:

| Outcome | Model text |
|---------|-----------|
| EMPTY | `(no output — expected for {tool_name})` |
| NONE | empty string (no output, unexpected — model should react) |
| BLOCKED | `(blocked: {error})` |
| SKIPPED | `(skipped — {reason})` |
| PARTIAL | `(partial output above, {N} chars omitted)` |

This is implemented as a new top-level function in
`agent/observation_rendering.py` that replaces the current inline
`truncate_output()` call with a structured `compress_tool_result()` call.

**No changes to `ToolResult` or `Observation` types** — all facts are
extracted from existing fields and rendered as text.  This is a pure
presentation-layer change.

**Impact on existing dimensions:**

| Dimension | Impact |
|-----------|--------|
| 1. Registry/Permissions | None |
| 2. Parallel execution | None |
| 3. Validation/Dispatch/Parse | None |
| 5. Tool description generation | None |
| 7. Result contract | **Enhanced** — `Observation.outcome` drives rendering |
| 8. Cancellation | None |
| 9. Side effects | None |

**Verification — how does the model behavior improve?**

1. **Test: long Bash output (50k build log)** — model sees exit code +
   first 1000 chars + last 1000 chars + "15000 chars omitted" summary.
   Model correctly decides whether build passed/failed without seeing
   all output.

2. **Test: Read of 3000-line file** — model sees first 2000 chars +
   last 2000 chars.  Model can decide whether to re-read specific
   sections vs asking for more.

3. **Test: empty Grep result** — model sees `(no matches found)` not
   `(no output)`.  Model correctly interprets emptiness as "pattern
   absent" not "tool failure."

4. **Test: blocked tool call** — model sees `(blocked: Permission
   denied)` with reason.  Model does not retry the same call.

### Acceptance Checklist: P0-#1

- [ ] `ToolResultFacts` dataclass defined with exit_code, file_paths, match_count, error_lines, summary
- [ ] `_extract_facts()` reads ONLY from `Observation.metadata` explicit keys — **zero regex/pattern matching on output text**
- [ ] If a tool executor lacks exit_code/error_lines/match_count metadata, fix the EXECUTOR (not the renderer)
- [ ] Bash executor writes `exit_code` into `ToolResult.metadata` at execution time
- [ ] `_truncate_body(raw, max_chars)` implements 3-tier truncation: Tier 2 preserves BOTH head (40%) AND tail (40%)
- [ ] No free-text "summary line" — structured facts ARE the summary
- [ ] `compress_tool_result(observation) → str` replaces `truncate_output()` in rendering pipeline
- [ ] Empty result semantics: 5 Outcome markers → explicit model text
- [ ] `build_tool_result_content()` calls `compress_tool_result()` instead of `truncate_output()`
- [ ] `format_observations_for_history()` calls `compress_tool_result()` instead of `truncate_output()`
- [ ] Existing truncation tests pass unchanged (output format may change, semantics preserved)
- [ ] Long Bash output: exit code always visible regardless of truncation tier
- [ ] Long Read output: file path always visible
- [ ] Empty Grep: explicit "(no matches found)"
- [ ] Permission denied: explicit "(blocked: Permission denied)"
- [ ] No change to Observation/ToolResult types — pure rendering layer
- [ ] 114+ test suite passes with 0 regressions

---

## 2. P0-#2: MCP Effect Inference (VERIFIED COMPLETE)

**Decision**: Already implemented in commit `81a0d7d`.  This P0 item was
completed before the formal DDR was written.  The implementation follows
the principles recorded here for audit completeness.

**Actual behavior change**:

| Before | After |
|--------|-------|
| All MCP tools `is_read_only = False` | Inferred from metadata → heuristic → logged UNKNOWN |
| All MCP tools denied in plan mode | Read-only MCP tools auto-allowed |
| All MCP tools run SERIAL | Read-only MCP tools can run PARALLEL_SAFE with other safe tools |
| All MCP tools APPROVAL retry | Read-only MCP tools AUTOMATIC retry |

**Verification**: 9 new tests pass.  81 regression tests pass.  MCP
`list_resources` and `read_resource` explicitly READ_WORKSPACE.

---

## 3. P0-#3: Parallel Safety Decoupled from Read-Only

### Decision Record 3

**Problem — what cognitive/behavioral issue does this solve?**

Currently, tool concurrency (parallel-safe vs serial) is derived from
`isReadOnly()` for tools that don't override `concurrency_mode()`.  This
creates a false coupling:

- "Read-only" =/= "safe to run in parallel"
- Two read-only tools can race on the same temp file
- A read-only MCP tool calling a remote API might have server-side rate
  limits that make parallel execution dangerous
- Conversely, some write tools (e.g., writing to *different* files) are
  actually parallel-safe but get serialized because they're not read-only

The admission control in `StreamingToolExecutor._try_start()` already has
the infrastructure to enforce this — the gap is that the **declaration**
is tied to the wrong property.

**CC reference — how does Claude Code solve this?**

CC separates two orthogonal concerns:
- `isReadOnly`: affects permission mode (plan/dontAsk) and retry policy
- `parallelSafe`: independent declaration on the tool class, defaults to
  `false` (fail-closed).  Tools that operate on disjoint resources
  explicitly declare `parallelSafe = true`.

CC's admission control uses `parallelSafe` to serialize tools that share
resources, preventing the "two reads racing on the same temp file" problem.

**Our approach**

Add `parallel_safe` as an independent property on `BaseTool`, separate
from `isReadOnly()` and `concurrency_mode()`.  `concurrency_mode()` becomes
the adapter that maps `parallel_safe` + `isReadOnly()` into the
`ToolConcurrency` enum.

**Implementation**:

```python
# core/base.py
class BaseTool(ABC):
    @property
    def parallel_safe(self) -> bool:
        """Whether this tool can run concurrently with OTHER tools.

        Default: ``False`` (fail-closed — serial execution is always safe).
        Tools that operate on disjoint resources (different files, independent
        network calls) MUST override this to ``True``.

        This is SEPARATE from ``isReadOnly()``.  A tool can be:
        - read-only + parallel-safe   (Read: independent files)
        - read-only + NOT parallel-safe (remote API rate-limited)
        - write + parallel-safe        (Write to different files)
        - write + NOT parallel-safe    (Edit: most tools, default)
        """
        return False

    def concurrency_mode(self, params: dict[str, Any]) -> ToolConcurrency:
        """Declare whether this specific call may run beside sibling calls.

        Default derives from ``parallel_safe``:
        - ``parallel_safe=True`` → ``PARALLEL_SAFE``
        - ``parallel_safe=False`` → ``SERIAL``

        Input-aware tools (Bash: ``ls`` vs ``rm``) override this and inspect
        *params* directly without depending on the static property.
        """
        if self.parallel_safe:
            return ToolConcurrency.PARALLEL_SAFE
        return ToolConcurrency.SERIAL
```

**Per-tool declarations**:

| Tool | `parallel_safe` | `isReadOnly()` | Rationale |
|------|----------------|---------------|-----------|
| Read (file_read) | `True` | depends on input | Disjoint files → safe |
| Grep (search_text) | `True` | True | Stateless search → safe |
| Glob (find_files) | `True` | True | Stateless listing → safe |
| WebSearch | `True` | True | Stateless, no shared state |
| WebFetch | `True` | True | Stateless, no shared state — rate limiting is a retry concern |
| Edit (file_edit) | `False` | False | File lock needed |
| Write (file_write) | `False` | False | File creation |
| Bash (shell) | depends on cmd | depends on cmd | `concurrency_mode()` already overrides |
| GitStatus | `True` | True | Read-only VCS → safe |
| MemoryRead | `True` | True | Stateless → safe |

MCP tools: `parallel_safe` defaults to `False` (fail-closed).  If the
MCP server declares effects that indicate stateless read-only operation,
`parallel_safe` can be `True` — but this is opt-in, not inferred.

**No change to `ToolConcurrency` enum** — it already has the right values.
`concurrency_mode()` is the single call site for `StreamingToolExecutor`;
all it does is map `parallel_safe` into the existing enum, no behavioral
change to the executor.

**Impact on existing dimensions:**

| Dimension | Impact |
|-----------|--------|
| 1. Registry/Permissions | None |
| 2. Parallel execution | **Enhanced** — SAFE/SERIAL decoupled from read-only |
| 3. Validation/Dispatch/Parse | None |
| 6. Hook system | None |
| 7. Result contract | None |
| 9. Side effects | **Clarified** — `parallel_safe` independent of effects |

**Verification — how does the model behavior improve?**

1. **Test: two unrelated Read calls** — `Read(a.py)` and `Read(b.py)` run
   in parallel.  Previously they were parallel because `isReadOnly=True` →
   `PARALLEL_SAFE`.  Behavior **unchanged** (our defaults match).

2. **Test: two WebFetch calls** — `WebFetch(url1)` and `WebFetch(url2)`
   run in parallel.  They are stateless and share no resources.
   Rate limiting is handled at the retry layer (exponential backoff,
   429 retry), not the parallel-safety declaration.

3. **Test: parallel Read+WebFetch** — Read(a.py) runs parallel with
   WebFetch(url).  Both are parallel-safe, independent resources.
   Parallel execution is correct and efficient.

4. **Test: MCP tool default** — All MCP tools default to `parallel_safe=False`.
   Behavior **unchanged** from current (all MCP tools were SERIAL because
   `isReadOnly=False` → `PARALLEL_SAFE` was never triggered).

### Acceptance Checklist: P0-#3

- [ ] `BaseTool.parallel_safe` property defined (default `False`, fail-closed)
- [ ] `BaseTool.concurrency_mode()` updated to derive from `parallel_safe` (not only `isReadOnly`)
- [ ] `BuiltTool._parallel_safe` field added, `parallel_safe` property override
- [ ] `build_tool()` accepts `parallel_safe` parameter (default `False`)
- [ ] Read, Grep, Glob, GitStatus, MemoryRead → `parallel_safe = True`
- [ ] WebSearch, WebFetch → `parallel_safe = False` (rate-limited APIs)
- [ ] Edit, Write → `parallel_safe = False` (unchanged)
- [ ] Bash → `concurrency_mode()` override unchanged (command-aware)
- [ ] MCP tools default to `parallel_safe = False` (fail-closed, unchanged from current SERIAL)
- [ ] `StreamingToolExecutor` admission control unchanged (consumes `concurrency_mode()`)
- [ ] 114+ test suite passes with 0 regressions
- [ ] New test: two WebFetch calls run IN parallel (stateless, no shared resources)
- [ ] New test: Read+WebFetch run IN parallel (both parallel_safe, independent resources)

---

## Cross-Phase Summary

| P0 Item | Status | DDR Section | Implementation |
|---------|--------|-------------|----------------|
| #1 Result Compression | 📋 Planned | 1 | `compress_tool_result()` in agent/observation_rendering.py |
| #2 MCP Effect Inference | ✅ Complete | 2 | Commit `81a0d7d` — no further work needed |
| #3 Parallel Safety | 📋 Planned | 3 | `BaseTool.parallel_safe` + per-tool declarations |

**Implementation order**: #1 first (presentation layer, zero behavioral
side effects on registry/executor/permissions).  #3 second (one new
property, five tool classes updated, one new behavior for WebFetch/WebSearch).
No change to streaming executor logic.
