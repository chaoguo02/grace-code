# Tool System Normalization Design

## 1. Purpose

This document defines a phased normalization of the tool execution system
based on an 11-dimension audit and Claude Code gap analysis.  The goals are:

1. Fix real behavioral defects that degrade model decision quality
2. Make implicit contracts explicit through type-level declarations
3. Align with CC's proven design patterns without rewriting working code

The toolkit is **not broken**.  The audit confirmed that all 11 dimensions
score ✅ or ⚠️ — nothing is ❌.  The gaps are in **semantic precision** and
**contract consistency**, not in functional completeness.

## 2. Core Principles

### 2.1 Normalization, Not Refactoring

"Normalization" means: take scattered implicit conventions, hardcoded
defaults, and compatibility compromises, and raise them to explicit,
type-safe, documented first-class citizens.  No behavior of existing
tool calls should change — only the **declaration** of their properties
becomes visible to the type system and the decision-making pipeline.

### 2.2 DDR-First Development

Every design decision is recorded as a Decision Record (DDR) answering:
- What cognitive/safety problem does this solve?
- How does Claude Code solve it?
- Why are we choosing the same (or different) approach?
- Which existing dimensions are affected?
- How do we verify that model behavior improved?

No code is written before the DDR is reviewed and approved.

### 2.3 Vertical Integration, Not Horizontality

Each phase delivers a complete vertical slice: declarative metadata →
pipeline consumption → model-visible behavior change → verification.
Phases are independent — completion of Phase N does not block Phase N+1.

### 2.4 Fail-Closed Defaults

All new declarative properties default to the most restrictive value.
Tools must **opt in** to parallel safety, cancellation support, and
trust decay.  This is the CC pattern: safe by default, fast by
declaration.

### 2.5 明确不做

| Anti-requirement | Reason |
|-----------------|--------|
| Runtime `/register` API | Contradicts CC philosophy — tools are system components |
| Auto-generated tool descriptions | CC proves hand-written is superior |
| Speculative parallel + rollback | Complexity >> benefit |
| Automatic summarization of outputs | Free-text summaries lose structural facts |
| Result caching | Adds consistency risk without proven need |

## 3. Gap Analysis Summary

| # | Gap | Severity | Phase | Type |
|---|-----|----------|-------|------|
| 1 | Result compression lacks structured metadata | High | Phase 1 | Behavioral — model sees degraded signals |
| 2 | MCP tools hardcoded `is_read_only=False` | High | ✅ Complete | Contract — fixed in `81a0d7d` |
| 3 | `parallel_safe` coupled to `isReadOnly` | High | Phase 1 | Contract — independent concerns |
| 4 | Session-level trust accumulation absent | Medium | Phase 2 | Behavioral — user confirmation fatigue |
| 5 | Tool descriptions always sent at FULL fidelity | Medium | Phase 2 | Efficiency — context window pressure |
| 6 | Shell SIGTERM integration missing | Medium | Phase 2 | Behavioral — long commands uninterruptible |
| 7 | TraceContext propagation tied to Langfuse | Low | Phase 3 | Engineering — backend portability |
| 8 | ToolResult 16 fields — possible over-carry | Low | Phase 3 | Engineering — serialization overhead |
| 9 | Permission pipeline 13-layer audit | Low | Phase 3 | Inspection — possible abstraction leak |

## 4. Design Decisions

### 4.1 Phase 1: Behavioral and Contract Fixes

#### #1: Tool Result Compression Pipeline

**Problem**: `truncate_output()` (agent/observation_rendering.py:51) is
character-level, not semantic-level.  When a 50k-char Bash build log is
truncated, the model loses the exit code (which is almost always at the
tail).  Empty results get `(no output)` as a generic fallback with no
explicit semantic marker.

**Decision**: Replace `truncate_output()` with `compress_tool_result()`
that extracts structured facts from `Observation.metadata` before
truncation, then assembles a model-visible block with facts + body.

**Fact extraction contract**: `_extract_facts()` reads ONLY from
`Observation.metadata` explicit keys.  **Zero regex or string-pattern
matching on output text is permitted.**  If a tool executor does not
populate these keys, fix the executor — not the renderer.

| Fact | Source key | Populated by |
|------|-----------|-------------|
| `exit_code` | `metadata["exit_code"]` | Bash/Shell executor at process completion |
| `file_paths` | `modified_files` field | All write tools via `result.modified_files` |
| `match_count` | `metadata["match_count"]` | Grep/Glob executor counts result lines |
| `error_lines` | `metadata["error_lines"]` | Tool executor extracts before truncation |
| `preview` | First 200 chars of `output` | Always from raw output (not metadata) |

**Truncation tiers**:

| Tier | Condition | Strategy |
|------|----------|----------|
| 0 | output ≤ 4,000 chars | Return as-is — no truncation |
| 1 | output ≤ 16,000 chars | Head (50%) + tail (50%) with omission marker |
| 2 | output > 16,000 chars | Head (40%) + tail (40%) + facts block (20%). **Tail is never dropped.** |

**Empty result semantics**: Derived from `Observation.outcome`:

| Outcome | Model text |
|---------|-----------|
| `EMPTY` | `(no output — expected for {tool_name})` |
| `NONE` | `(no output)` — unexpected, model should react |
| `BLOCKED` | `(blocked: {error})` |
| `SKIPPED` | `(skipped — {reason})` |
| `PARTIAL` | `(partial output above, {N} chars omitted)` |

**Implementation location**: New `agent/result_compression.py` module.
Two call sites in `agent/observation_rendering.py` replace
`truncate_output()` with `compress_tool_result()`.  No changes to
`ToolResult` or `Observation` types — pure presentation layer.

**Bash executor prerequisite**: `ShellTool.execute()` must write
`exit_code` into `ToolResult.metadata` before returning.  This is a
one-line addition in `tools/shell_tool.py`.  Without it, the
compression pipeline has no exit_code to render.

#### #2: MCP Tool Effect Inference — ✅ COMPLETE

Committed in `81a0d7d`.  9 new tests, 81 regression tests pass.
Three-layer inference: explicit effects list → read_only_hint →
name/description heuristic → UNKNOWN with logged warning.

#### #3: Parallel Safety Declaration Decoupled from Read-Only

**Problem**: `concurrency_mode()` defaults to `SERIAL` for all tools
that don't override it.  Read, Grep, Glob, and other read-only tools
run SERIAL despite operating on disjoint resources.  Web Fetch and
Web Search are stateless and share no resources, but are serialized.
This wastes parallelism opportunities in multi-tool batches.

**Key finding from exploration agent**: `isReadOnly()` and
`concurrency_mode()` are **already architecturally decoupled** in the
current codebase.  No code path derives one from the other.  The
default for both is fail-closed.  The gap is purely declaration —
most tools inherit the `SERIAL` default without declaring their
actual parallel-safety status.

**Decision**: Add `parallel_safe` as an independent property on
`BaseTool` (default `False`, fail-closed).  `concurrency_mode()` already
checks `parallel_safe` — the property just gives that method a clear,
documented source of truth instead of leaving every tool to override
the method individually.

**Per-tool declarations**:

| Tool | `parallel_safe` | Rationale |
|------|----------------|-----------|
| Read, ViewFile | `True` | Disjoint files — independent reads |
| Grep, Glob, FindSymbol | `True` | Stateless search/listing |
| WebSearch, WebFetch | `True` | Stateless network calls — rate limiting is a retry concern, not a parallelism concern |
| GitStatus | `True` | Read-only VCS |
| MemoryRead, MemoryList, MemorySearch | `True` | Stateless queries |
| Edit, Write | `False` | Need file-level lock |
| Bash | depends on cmd | Existing `concurrency_mode()` override already handles this per-command |
| MCP tools | `False` | Fail-closed — opt-in required |

**Impact**: Read, Grep, Glob, WebSearch, WebFetch, GitStatus, and
MemoryRead tools can now run in parallel batches.  Edit and Write
remain serialized.  MCP tools remain serialized by default (fail-closed).

### 4.2 Phase 2: Experience-Level Alignment

#### #4: Session-Level Trust Accumulation

**Problem**: The permission pipeline has binary trust: allow or deny.
Repeatedly approving the same tool on the same path within a session
causes confirmation fatigue.  The model correctly calls the tool, the
user correctly approves it, and the cycle repeats on the next turn
with identical parameters.

CC''s trust accumulation key is ``(tool_name, path)`` — a two-tuple,
not a three-tuple.  CC treats same-tool + same-path trust as transitive:
once you have approved ``Edit(file=a.py)`` twice, CC trusts that you are
ok with edits to that file generally, regardless of what specific
lines are changing.  CC can do this because it has a **post-hoc safety
review layer** that catches anomalous writes after the fact.

**Our approach — explicit divergence from CC**:

We use a four-category ``params_digest`` (Read → sha256(path), Write →
sha256(path + “|” + tool), Shell -> sha256(first_word + (“|” + second_word if second_word starts with “-” else “”)), Network ->
sha256(domain)).  This is **more granular** than CC — we distinguish
operation types within the same path.

**Divergence rationale**: Our permission model lacks CC''s post-hoc
safety review layer.  Until that layer exists, we cannot safely assume
that approval of Read(a.py) implies trust for Edit(a.py) — a
read-only approval should never cascade into write permission.
This granularity is a **conscious divergence** from CC, documented for
future convergence when post-hoc safety review is implemented.

The convergence trigger: when ``SafetyReviewService`` exists and can
retrospectively flag anomalous writes, the Write digest rule can
collapse from ``sha256(path + "|" + tool)`` → ``sha256(path)``,
aligning with CC''s two-tuple key.

**Decision**: Add an in-memory trust accumulator to
`PermissionPipeline` that tracks approved (tool, path, params_digest)
tuples within a session.  On the Nth approval of the same tuple
(default N=2), auto-approve for the rest of the session.

**params_digest calculation rule** (defined at design time, NOT left
to implementation-time discretion):

| Operation type | Digest input | Rationale |
|---------------|-------------|-----------|
| Read (Read, Grep, Glob, ViewFile) | `sha256(path)` | Same file same trust — content does not affect safety |
| Write (Edit, Write) | `sha256(path + "|" + tool_name)` | Path + operation type — different types are distinct trust events |
| Shell (Bash) | `sha256(first_word_of_cmd)` | `ls` and `rm` must never share trust |
| Network (WebFetch, WebSearch) | `sha256(url_domain)` | Same domain same trust — different paths on same API accumulate |

**Known limitation**: The Shell digest rule uses only
``first_word`` (optionally ``second_word`` if it is a flag like ``-c``
or ``-e``) in v1.  This places ``python script.py`` and ``python -c
"...destructive..."`` in **different** trust buckets (because ``-c``
is a flag), but ``python script1.py`` and ``python script2.py`` in
the **same** bucket.  This is a deliberate trade-off: (a) destructive
Python commands would be caught by the safety floor (Layer 1 —
protected paths, cmd injection patterns), (b) the user explicitly
approved Python invocation twice before auto-trust activates, and (c)
a two-word digest for all shell commands would prevent trust
accumulation for any interpreter (too strict).  The convergence path
is narrowing to ``sha256(first_two_words)`` when a post-hoc safety
review layer is implemented.  Tracked in DDR divergence log above.

**Implementation**:

```python
# hitl/pipeline.py — new class
class SessionTrustAccumulator:
    """Track approved (tool, path, params_digest) tuples within a session."""

    def __init__(self, *, threshold: int = 2) -> None:
        self._approved: dict[tuple, int] = {}  # tuple → approval count
        self._threshold = threshold

    def record_approval(self, key: tuple[str, str, str]) -> None:
        """Record one user approval for *key*."""
        self._approved[key] = self._approved.get(key, 0) + 1

    def is_trusted(self, key: tuple[str, str, str]) -> bool:
        """Return True if *key* has been approved ≥ threshold times."""
        return self._approved.get(key, 0) >= self._threshold
```

Integration point: `PermissionPipeline._layer3_rules()` checks the
accumulator before falling through to interactive approval.  This is
a non-security, non-bypassable optimization — the accumulator only
auto-approves patterns that the user has **already** explicitly
approved multiple times.

#### #5: Tool Description Progressive Disclosure

**Problem**: The current `format_tool_descriptions()` sends the full
description of every visible tool in every request.  When the tool
count exceeds ~30, this consumes significant context window tokens
and increases the model's cognitive load for tool selection.

The `ToolDescriptionTier` enum and per-tier `to_llm_schema()` method
were added in commit `4a33ea6`.  The missing piece is the **selection
logic** — currently all tools default to FULL tier.

**CC reference — how does Claude Code solve this?**

CC uses three layers of defense, applied in order:

1. **Source control**: Hand-crafted, aggressively concise descriptions.
   Typical tool description: 50–150 tokens.  Even with 30+ tools, total
   description volume stays under 2,000–3,000 tokens.  This is the
   foundation — the other two layers exist only because this one works.

2. **Semantic routing** (not statistical routing): Tools are selected
   based on task intent, not call history.  "Fix this bug" → Read, Edit,
   Grep, Bash only.  "Search this API" → WebSearch, WebFetch, Read only.
   The model's task intent drives tool visibility, not "what was recently
   called."  Low-frequency but currently-needed tools are never degraded.

3. **Fallback truncation** (not NAME_ONLY): When tool descriptions truly
   overflow the available context, CC truncates description text but
   **preserves the parameter schema**.  CC never degrades to name-only —
   because a tool with no parameters visible to the model is a tool that
   doesn't exist.  The budget is **dynamic**: `available_context -
   conversation_tokens - system_prompt_tokens = remaining_for_tools`,
   computed per-request, not a static constant.

**Our approach — v1 with explicit CC convergence path**

We implement a v1 approximation of CC's approach, with each component
labelled for its convergence path:

**Prerequisite (Phase 2 #5 gate)**: Audit all tool description lengths.
Any tool with description > 200 tokens must be trimmed to ≤ 200 tokens
before the selection logic is implemented.  This is CC's first line of
defense — you cannot fix description overflow downstream if the source
descriptions are bloated.  A one-time audit script reports `{name: token_count}`
description_tokens}` for every registered tool.

**Selection layer v1**: Frequency/recency as a **temporary approximation**
of CC's semantic routing.  Explicitly labelled as "v1 — converges to
task-intent-based semantic routing when task classification matures."

1. **Recency**: tools called in the last 5 turns → FULL
2. **Frequency**: top 5 most-called tools in current session → FULL
3. **Remaining**: all other tools → SUMMARY (description + params, no contract)
4. **No NAME_ONLY tier in normal operation**: See fallback below.

**Fallback layer — SCHEMA_ONLY, not NAME_ONLY**:

When tool description tokens exceed the available context budget
(dynamic, not static), the lowest-frequency tools degrade to
**SCHEMA_ONLY**: tool name + parameter schema + **one-line description**
(first sentence only, ≤ 80 chars).  The parameter schema is NEVER
dropped — without it, the model cannot invoke the tool.

This replaces the original `NAME_ONLY` tier.  The `ToolDescriptionTier`
enum gains `SCHEMA_ONLY` as the new minimum tier (replacing `NAME_ONLY`
which is deleted).

**Dynamic budget calculation**:

```python
remaining_for_tools = max(
    1000,
    available_context_tokens
    - conversation_tokens
    - system_prompt_tokens
    - RESERVE_FOR_RESPONSE
)
```

No static `TOOL_DESC_TOKEN_BUDGET` constant.  The budget is a function
of the actual context pressure, not a pre-configured number.  When
context is abundant (fresh session), all tools are FULL.  When context
is tight (50-turn session with history compaction), descriptions tighten.

**Telemetry (Phase 3 #9)**:

- `tool_desc_degraded_to_schema_only`: counts how many tools were
  degraded to SCHEMA_ONLY per request.  If this counter is consistently
  >0, the root cause is either: (a) individual tool descriptions are
  too long (source control failure), or (b) the frequency/recency
  approximation is failing for task-relevant tools (semantic routing
  needed).  Either way, adjusting the fallback threshold is NOT the fix.

**CC convergence path** (documented, not implemented):

| v1 Approach | Converges To | Trigger |
|-------------|-------------|---------|
| Frequency/recency tool selection | Semantic routing by task intent | TaskIntent classification matures |
| SCHEMA_ONLY fallback | Truncate description, preserve schema | Source control makes fallback rare |
| Dynamic budget | Dynamic budget (same) | Already aligned |

**Implementation**: A static function on `ContextPlanner` that takes
the list of tool schemas + session call history + available context
budget, returns the same list with `tier` set on each schema.  Called
from `ToolRegistry.get_schemas()` integration point.  Zero behavior
change for sessions with < 20 tools (all default to FULL — budget is
ample).  Progressive disclosure only activates when context pressure
is real.

#### #6: Shell Tool SIGTERM Integration

**Problem**: `CancellationToken.cancel()` sets an Event flag, but the
Bash subprocess never checks it.  A 60-second `npm install` continues
running after the user cancels, wasting resources and forcing the user
to wait.

`supports_cancellation` was added to `BaseTool` in commit `04480c9`.
`ShellTool.supports_cancellation` is `True`.  The missing piece is
**actually delivering the signal to the subprocess**.

**Decision**: When `CancellationToken.is_cancelled` becomes `True`,
the ShellTool executor sends `SIGTERM` (or `CTRL_BREAK_EVENT` on
Windows) to the subprocess.  After a 5-second grace period, if the
process hasn't exited, send `SIGKILL` (or `TerminateProcess`).  This
is the only "semi-forcible" cancellation path in the system.

**Implementation**: A small helper function in `tools/shell_tool.py`
that is called from the `execute()` method when the tool declares
`supports_cancellation=True`.  The cancellation token is passed
through the `ToolExecutionPipeline` (already plumbed in `04480c9`).

### 4.3 Phase 3: Engineering Cleanup

#### #7: TraceContext Native Propagation — ✅ COMPLETE

Committed in `ea62c1f`.  `observability/trace_context.py` provides a
`contextvars.ContextVar`-based propagation mechanism with zero backend
dependency.  Integration into the hook system and evidence recorder
is deferred to Phase 3 implementation.

#### #8: ToolResult Field Complexity Audit

**Problem**: `ToolResult` has 16 fields.  Some (`data`, `cached`,
`duration_ms`) are never rendered to the model.  Others (`subagent_tokens_used`,
`structured_findings`) are consumed by specific subsystems and never
appear in the output pipeline.  The flat field list creates ambiguity
about which fields are "output payload" vs "metadata for internal routing."

**Decision**: Document the 16 fields into three categories.  No field
removal — this is a documentation-only normalization for Phase 3.  If
a future cleanup phase removes fields, this document serves as the
canonical reference.

| Category | Fields | Consumer |
|----------|--------|----------|
| **Output payload** | `output`, `error`, `tool_error` | Observation rendering → model |
| **Action evidence** | `modified_files`, `outcome`, `attachments` | CompletionGuard, evidence chain |
| **Runtime metadata** | `success`, `duration_ms`, `cached`, `subagent_tokens_used`, `structured_findings`, `metadata`, `data`, `invocation_id`, `attempt_count`, `eventual_success` | Agent loop, budget, memory |

**Implementation**: Docstring update on `ToolResult` with the table above.
No code changes.

#### #9: Permission Pipeline Layer Audit

**Problem**: The HITL permission pipeline has 6 layers (validate → hooks →
rules → permission mode → path sandbox → interactive callback).
Superficially, `build_registry_for_session()` also layers policy on top
via `PolicyAwareToolRegistry`, which adds 7 more decision points.  The
audit question: are all 13 layers necessary, or is there abstraction
leakage?

**Decision**: Phase 3 is **observation only** — collect telemetry on
which layers actually block tool calls in production.

**CC baseline**: CC operates with 6 permission layers. Our 13-layer
pipeline includes 7 additional decision points from
PolicyAwareToolRegistry.  The audit hypothesis is that these 7
layers are either redundant with the base 6 or represent abstraction
leakage from the policy registry pattern.  Telemetry should
**specifically test this hypothesis**, not just count firings:
each blocked call records which of the 13 layers performed the block.
After the observation period, layers that never blocked are candidates
for removal.

No code removal happens in Phase 3 without telemetry data.

**Implementation**: Add a DEBUG-level counter to each layer that logs
"layer N blocked tool X N times in this run."  The counter starts at 0
per session and accumulates.  After **100 sessions OR 30 days
(whichever comes first)**, analyze the distribution and propose specific
layer removals/merges.  If 30 days pass with fewer than 100 sessions,
base the analysis on available data — partial data is better than
indefinite deferral.

## 5. Migration Phases

### Phase 1: Behavioral and Contract Fixes (3 items)

1. **#1 Result Compression**: Add `compress_tool_result()`, bash exit_code
   metadata, 3-tier truncation, empty result semantics.
2. **#2 MCP Effect Inference**: ✅ Complete (`81a0d7d`).
3. **#3 Parallel Safety**: Add `BaseTool.parallel_safe`, declare on
   Read/Grep/Glob/Web*/Git/Memory, wire into `concurrency_mode()`.

**Cost**: ~3 files changed for #1, ~8 files for #3. ~300 lines total.
**Risk**: Low. #1 is pure presentation. #3 changes parallelism behavior
but all changes are opt-in — serial-by-default is preserved for tools
that don't declare.

### Phase 2: Experience-Level Alignment (3 items)

1. **#4 Trust Accumulation**: `SessionTrustAccumulator` class, integration
   into permission pipeline.
2. **#5 Progressive Disclosure**: Tier selection policy in ContextPlanner,
   wire into `ToolRegistry.get_schemas()`.
3. **#6 SIGTERM Integration**: Signal delivery in ShellTool executor,
   grace-period-then-kill behavior.

**Cost**: ~4 files changed, ~200 lines total.
**Risk**: Medium. #4 changes permission behavior (auto-approval of
repeated patterns). #6 kills processes.

### Phase 3: Engineering Cleanup (3 items)

1. **#7 TraceContext Integration**: Wire `TraceScope` into
   `ToolExecutionPipeline` and hook dispatcher (context already defined).
2. **#8 ToolResult Doc**: Add category table to `ToolResult` docstring.
3. **#9 Permission Audit**: Add per-layer telemetry counters.

**Cost**: ~3 files changed, ~100 lines total.
**Risk**: Low. #7 is additive — no existing path changed. #8 is docs-only.
#9 is telemetry-only — no behavior change.

## 6. Impact Analysis Matrix

| Downstream System | Phases Affected | Risk | Mitigation |
|-------------------|----------------|------|------------|
| Permission pipeline | Phase 2 | Medium | Trust accumulator only auto-approves after ≥2 explicit user approvals. Never bypasses Layer 1 (safety floor) |
| Streaming executor | Phase 1 | Low | parallel_safe opt-in only — default SERIAL unchanged for undeclared tools |
| Shell/Bash tool | Phase 1, 2 | Medium | exit_code metadata is additive. SIGTERM only for supports_cancellation=True |
| MCP integration | Phase 1 | Low | parallel_safe defaults to False (unchanged from current SERIAL for all MCP) |
| Observation rendering | Phase 1 | Low | compress_tool_result() replaces truncate_output() — wider contract, same call sites |
| Context budget | Phase 2 | Low | Progressive disclosure reduces tool description tokens — budget benefits |
| Observability (Langfuse) | Phase 3 | Low | TraceContext propagation is additive — Langfuse path unchanged |
| Frontend | None | None | No UI changes in any phase |

## 7. Acceptance Checklist

### Phase 1: Behavioral and Contract Fixes

#### #1: Result Compression

- [ ] `ToolResultFacts` dataclass defined with exit_code, file_paths, match_count, error_lines, preview
- [ ] **PREREQUISITE (implemented BEFORE `compress_tool_result()`)**: Bash executor writes `exit_code` into `ToolResult.metadata` at process completion.  Verified by standalone test: `ShellTool.execute(cmd)` → `result.metadata["exit_code"]` is non-empty string.
- [ ] `_extract_facts()` reads ONLY from `Observation.metadata` explicit keys — **zero regex** on output text
- [ ] `_truncate_body()` implements 3-tier truncation; Tier 2 preserves both head (40%) AND tail (40%)
- [ ] No free-text "summary line" — structured facts ARE the summary
- [ ] `compress_tool_result()` replaces `truncate_output()` in both rendering call sites
- [ ] Empty result semantics: 5 Outcome markers → explicit model text
- [ ] Long Bash output: exit code always visible regardless of truncation tier
- [ ] Long Read output: file path always visible in facts block
- [ ] Empty Grep: explicit "(no matches found)"
- [ ] Permission denied: explicit "(blocked: Permission denied)"
- [ ] No change to Observation/ToolResult types — pure rendering layer

#### #2: MCP Effect Inference

- [x] MCP tools no longer hardcoded `is_read_only=False`
- [x] `list_resources` and `read_resource` explicitly READ_WORKSPACE
- [x] 9 new inference tests pass
- [x] 81 regression tests pass

#### #3: Parallel Safety

- [ ] `BaseTool.parallel_safe` property defined (default `False`, fail-closed)
- [ ] `BaseTool.concurrency_mode()` derives from `parallel_safe`
- [ ] `BuiltTool._parallel_safe` field + property override
- [ ] `build_tool()` accepts `parallel_safe` parameter
- [ ] Read, ViewFile → `parallel_safe = True`
- [ ] Grep, Glob, FindSymbol → `parallel_safe = True`
- [ ] WebSearch, WebFetch → `parallel_safe = True`
- [ ] GitStatus → `parallel_safe = True`
- [ ] MemoryRead, MemoryList, MemorySearch → `parallel_safe = True`
- [ ] Edit, Write → `parallel_safe = False` (unchanged)
- [ ] Bash → `concurrency_mode()` override unchanged
- [ ] MCP tools default to `parallel_safe = False`
- [ ] `StreamingToolExecutor` admission control unchanged
- [ ] New test: two WebFetch calls run IN parallel
- [ ] New test: Read+WebFetch run IN parallel

### Phase 2: Experience-Level Alignment

#### #4: Trust Accumulation

- [ ] `SessionTrustAccumulator` class defined with approval threshold
- [ ] Accumulator integrated into `PermissionPipeline._layer3_rules()`
- [ ] Accumulator only auto-approves after ≥2 explicit user approvals
- [ ] Accumulator never bypasses Layer 1 (safety floor — protected paths, cmd injection)
- [ ] Trust resets on session restart (in-memory only)
- [ ] Trust key includes: tool_name, path_parameter, params_digest
- [ ] params_digest calculation rule documented (Read→sha256(path), Write→sha256(path+"|"+tool), Shell→sha256(cmd_word), Network→sha256(domain))
- [ ] New test: 1st approval prompts, 3rd identical call auto-approves
- [ ] New test: different path on same tool → fresh prompt (not trusted)
- [ ] New test: same path, different operation type (Read vs Edit) → separate trust (not shared)

#### #5: Progressive Disclosure

- [ ] **PREREQUISITE GATE (blocks selection logic implementation)**: Audit all built-in tool descriptions. Any >200 tokens must be trimmed to ≤200. One-time audit script runs and **exits non-zero if ANY tool exceeds limit**. Selection logic implementation is BLOCKED on zero failures.
- [ ] Tools called in last 5 turns → FULL tier
- [ ] Top 5 most-called tools → FULL tier
- [ ] Remaining tools → SUMMARY tier
- [ ] Dynamic budget calculation implemented: `remaining_for_tools = max(1000, available_context - conversation_tokens - system_prompt_tokens - RESERVE_FOR_RESPONSE)`. No static token budget constant exists.
- [ ] Phase 3 #9 telemetry includes `tool_desc_degraded_to_schema_only` counter for budget tuning feedback
- [ ] Integrated into `get_schemas()` or `format_tool_descriptions()` call path
- [ ] Zero behavior change for sessions with < 20 tools
- [ ] New test: mock 30-tool session, verify some tools are SUMMARY
- [ ] New test: tool called once → promoted to FULL on next turn

#### #6: SIGTERM Integration

- [ ] ShellTool executor sends OS signal on CancellationToken.cancel()
- [ ] Windows: CTRL_BREAK_EVENT; Unix: SIGTERM
- [ ] 5-second grace period before SIGKILL/TerminateProcess
- [ ] Cancellation token passed through ToolExecutionPipeline.execute()
- [ ] Non-shell tools unaffected (supports_cancellation=False path unchanged)
- [ ] New test: cancel kills long-running subprocess within 10 seconds
- [ ] New test: cancel during short command → no kill needed (already exited)

### Phase 3: Engineering Cleanup

#### #7: TraceContext Integration

- [ ] `TraceScope` wired into `ToolExecutionPipeline.execute()`
- [ ] `HookContext` reads `trace_context` from ContextVar
- [ ] Evidence recorder reads `trace_context` from ContextVar
- [ ] Langfuse observer reads `trace_context` and maps to span attributes
- [ ] Unit test: ContextVar propagation verified with mock observer

#### #8: ToolResult Doc

- [ ] Docstring updated with 3-category table (output payload / action evidence / runtime metadata)
- [ ] No field removal — documentation only

#### #9: Permission Audit

- [ ] Per-layer counter added to PermissionPipeline
- [ ] Counter logged at DEBUG level per session summary
- [ ] No behavior change — telemetry only

### Final Cross-Phase Acceptance

- [ ] 114+ test suite passes after each phase with 0 regressions
- [ ] Model behavior improves on 3 verified scenarios: long build log, empty search, blocked call
- [ ] New properties all default to fail-closed (False/SERIAL)
- [ ] No runtime /register API added (explicit anti-requirement)
- [ ] No auto-summary generation of tool descriptions or outputs
- [ ] No change to ToolResult/Observation data model types
- [ ] Streaming executor admission control logic unchanged

## 8. Interaction with Context Module Refactoring

The context module refactoring (Phase 0-5) is **orthogonal** to this
normalization.  The two efforts touch different layers:

| Context Refactoring | Tool Normalization |
|---------------------|-------------------|
| "How are descriptions assembled into the prompt" | "What metadata do tools declare about themselves" |
| Compression pipeline for conversation history | Compression pipeline for individual tool results |
| Runtime staging, message assembly | Tool execution, permission, parallelism |

Phase 1 #1 (Result Compression) is the only overlap point — it replaces
`truncate_output()` which is a presentation-layer function that sits
between tool execution (producing `ToolResult`) and context assembly
(consuming `Observation` for history injection).  It is compatible with
both the old and new context pipeline.

## 9. Interaction with Capability Index

The capability index (Phases 0-7) is **not affected** by tool normalization.
Capabilities describe *what* tools exist; normalization describes *how*
tools declare their properties.  The two are independent layers.
