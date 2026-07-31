# Context Module Convergent Refactoring Design

## 1. Purpose

This document defines a convergent refactoring of the context management module
(`context/`, `agent/session/runtime.py`, `agent/context_trimming.py`, `prompts/`)
based on a deep audit (sections 2.1–2.9) and comparison with Claude Code's
reference architecture.

The goal is NOT a rewrite. The architecture skeleton (dual message model,
layered compression, WAL persistence) is sound. The goal is to eliminate
over-engineering, fix real defects, and align responsibilities so that upcoming
work on memory auto-injection, subagent communication, context budgeting, tool
calling, MCP, and skill invocation builds on a clean foundation.

## 2. Problem Summary

### 2.1 Over-Engineering Identified

| Problem | Location | Waste |
|---------|----------|-------|
| Capability acknowledgment round-trips (user+assistant per section) | `context/manager.py:368-383` | ~4 extra messages per request, ~200-400 tokens |
| `MessageKind` enum duplicates `role` for USER/ASSISTANT/SYSTEM/TOOL_RESULT | `llm/base.py:28-37` | 4 redundant enum values, dual classification everywhere |
| 5-layer compression pipeline (ToolBudget→Snip→Micro→Collapse→Auto) | `agent/context_trimming.py`, `context/compaction.py` | Snip+Micro have overlapping semantics; Collapse store is memory-only |
| `ConversationCompactor` duplicates thrashing counters with `ContextPlanner` | `compaction.py:123-124`, `manager.py:127-128` | Two independent state machines for the same decision |
| `runtime_prompt_builder` has dead fallback injection path guarded by boolean flag | `runtime_prompt_builder.py:67-82` | TODO comment since Phase 4, active fallback code |

### 2.2 Real Defects

| Defect | Severity | Location |
|--------|----------|----------|
| `ContextPlanner` mutable counters without locks | 🔴 | `context/manager.py:127-136` |
| Raw MCP error text stored in `CapabilityDescriptor.runtime.error` | 🔴 | `capabilities/providers/mcp_provider.py:37` |
| `ConversationHistory` no thread safety (bare list, no locks) | ⚠️ | `context/history.py:235` |
| `_claim_new_messages()` setattr race condition | ⚠️ | `agent/session/runtime.py:2840-2871` |
| `MemoryContext.set_session_context()` shared-instance race | ⚠️ | `memory/context.py` |
| `FileReadCache` shared reference across tools | ⚠️ | `agent/session/runtime.py:191` |

### 2.3 Design Principle Violations

| Principle | Violation |
|-----------|-----------|
| "Runtime owns lifecycle, not tool state" | `FileReadCache` lives on `SessionRuntime` |
| "Control-flow metadata in content, not side-channel" | `MessageKind` duplicates `role` instead of using content markers |
| "Single source of truth for compaction decisions" | `ContextPlanner` + `ConversationCompactor` duplicate thrashing state |
| "Capability descriptions are data, not conversation turns" | Synthetic assistant acknowledgments for injected sections |

### 2.4 Injection Visibility Gap

`_RUNTIME_PREFIXES` (defined in two places: `session_store.py:780-786` and
`runtime.py:2111-2117`) filters 14 prefixes from frontend display and DB
persistence.  The exploration agent found **8 injection sites whose prefixes
are NOT in the filter list** — these messages are visible in the frontend
and persisted to SQLite:

| Prefix | Source | Risk |
|--------|--------|------|
| `[RUNTIME EVIDENCE STATE]` | `run_evidence.py` / `core.py` | Evidence state leaks to frontend |
| `[RUNTIME BLOCK]` | `completion_guard.py` / `core.py` | Completion block reasons visible to user |
| `[SESSION START HOOK CONTEXT]` | `runtime.py:2096` | Hook context may contain internal instructions |
| `[Stop hook blocked completion]` | `core.py:2912` | Stop hook internal state exposed |
| `[Subagent: {name}]` | `subagent.py:602` | Subagent system prompts visible to user |
| `[Skill: {name}]` | `entry/chat.py:384` | Skill activation visible (CLI, `RUNTIME_NOTICE`-guarded) |
| `<task-notification>` | `task_tool.py` | XML task notifications visible |
| `[Parent message from ...]` | `runtime.py:2574` | Parent-child inter-agent messages visible |

This is not a security vulnerability (these messages don't carry secrets),
but they are **implementation details that should not appear in the user
interface**.  The fix is simple: add the missing prefixes to
`_RUNTIME_PREFIXES` in both locations.

## 3. Core Design Principles

### 3.1 Single Classification Path

Messages are classified by `role: str` (the API-facing value). Internal
control-flow distinctions use content markers (bracket-prefixed sections) or
explicit filtering at storage boundaries. The `kind` field shrinks to an
optional marker for non-LLM messages only.

### 3.2 Capability Context as Data, Not Conversation

Injected context blocks (capabilities, memory, project rules) are data
prefixes that the model reads but does not need to verbally acknowledge.
Single `role="user"` message injection, no synthetic assistant replies.

### 3.3 Linear Compression Pipeline

Replace the 5-layer pipeline with a clear 2-phase approach:
1. **Rule-based pre-processing** (merged Snip + Micro + ToolBudget): zero-API,
   structural cleanup that runs before every LLM call
2. **Semantic compaction** (AutoCompact + Collapse): LLM-driven summarization
   that runs when token pressure exceeds threshold

### 3.4 Runtime as Session Container

`SessionRuntime` owns only: session identity, lifecycle (create/run/cancel),
cancellation tokens, and the session state dictionaries that provide physical
isolation. Tool-level optimization state (read cache), transport callbacks
(stream), and pre-run setup (skill activation) move to dedicated components.

### 3.5 Defensive Safety Boundaries

- All error text from external sources (MCP, tool output) is sanitized at
  ingestion time, not just at render time
- All shared mutable state in Runtime is lock-protected
- `ConversationHistory` gets a shallow-copy accessor for cross-thread reads

## 4. Design Decisions

### 4.1 Message Model Simplification

**Current state**: `LLMMessage` has both `role: str` and `kind: MessageKind`
(7 enum values).  The exploration agent found:

| Value | Ever created? | Checked/read? | Adds info beyond role? |
|-------|--------------|---------------|----------------------|
| `USER` | Only in `list_messages()` read-back | Never read after assignment | No — mirrors `role` |
| `ASSISTANT` | Only in `list_messages()` read-back (wrong for `role="tool"`) | Never read after assignment | No — mirrors `role` (with a bug) |
| `SYSTEM` | Never | Never | Dead |
| `TOOL_RESULT` | Never | Never | Dead |
| `COMPACTION_BOUNDARY` | Never | Never | Dead — compaction uses content-prefix matching |
| `RUNTIME_NOTICE` | 2 sites: `turns.py` + `entry/chat.py` | `append_message()` gate | **YES** — distinguishes injected infrastructure from real messages |
| `PLAN_CONTEXT` | Never | `append_message()` gate (unreachable) | Dead — gate exists but never triggered |

**Decision**: Shrink `MessageKind` to a single value:

```python
class MessageKind(str, Enum):
    """Non-standard message marker for storage/display filtering.
    
    Only RUNTIME_NOTICE is set at creation time.  Messages read back from
    the database leave ``kind`` as ``None`` — the ``role`` field is
    sufficient for all display/control-flow purposes.
    """
    RUNTIME_NOTICE = "runtime_notice"
```

Remove: USER, ASSISTANT, SYSTEM, TOOL_RESULT, COMPACTION_BOUNDARY, PLAN_CONTEXT.

Remove the `kind = MessageKind.USER/ASSISTANT` reconstruction in
`session_store.py:817` — it was never consumed downstream.  This also
fixes the latent bug where `role="tool"` messages were incorrectly
labelled `ASSISTANT`.

**Impact on downstream systems**:
- `session_store.py:817`: Remove kind assignment entirely → `kind=None` from DB
- `session_store.py:628`: `message.kind in (RUNTIME_NOTICE,)` — single-value check
- `agent/loop/turns.py:201`: unchanged (RUNTIME_NOTICE remains)
- `entry/chat.py:386`: unchanged (RUNTIME_NOTICE remains)
- All code that checked `kind == MessageKind.USER` → check `role == "user"`.
  The exploration agent confirmed there is **zero production code** that
  branches on `kind == USER/ASSISTANT` — only the `list_messages()` assignment
  and a single test assertion.

### 4.2 Capability Context Injection Simplification

**Current state**: Each injected section (capabilities, long-term memory) gets
a user+assistant pair:
```
user: "[CAPABILITY CONTEXT]\n..."
assistant: "Understood. I will use the available capabilities as needed."
user: "[MEMORY]\n..."
assistant: "Understood. I have the project context and memory index..."
```

**⚠️ Critical constraint**: Capability descriptions (Skills, MCP tools,
Subagent types) are **static behavioral constraints**.  The model must
treat them as permanent rules, not transient user requests.  In the LLM
protocol, `role="system"` carries higher instruction-following priority
than `role="user"`.  Moving capabilities into a user message would
**downgrade their priority** — the model may ignore tool availability
rules or treat MCP discovery instructions as optional user suggestions.

Claude Code keeps static capability descriptions in the system prompt
(or system-level injection).  Only dynamic session context (memory,
task-specific state) goes into user messages.

**Decision**: Two-tier injection:

**Tier 1 — System prompt** (high priority, stable, cached):
Capability context `[CAPABILITY CONTEXT]` moves **into the system prompt**
as an appended section.  It joins `system_core_text` + `variable_text`
in `StructuredContext`, after the stable `system_core` layer.  This means
it participates in prompt caching (Anthropic `cache_control` on stable
prefix) and carries system-level instruction priority.

**Tier 2 — User message** (dynamic, per-request):
Long-term memory context and project rules stay as a single user message.
No synthetic assistant acknowledgment.

```python
# Tier 1: Capabilities go into system prompt
structured_ctx.add_layer(ContextLayer(
    name="capability_context",
    priority=ContextPriority.PROJECT,
    content=capability_context,    # Skills, MCP, Subagents
    cacheable=True,                # joins stable prefix cache
))

# Tier 2: Memory + rules as single user message
if long_term_context:
    messages.append(LLMMessage(
        role="user",
        content=f"[SYSTEM CONTEXT]\n{long_term_context}",
    ))
```

**Token savings**: ~200-400 tokens per request (2 assistant messages removed).
At 50-turn sessions, this saves 10K-20K tokens.  No priority downgrade.

**Ordering stability**: `[SYSTEM CONTEXT]` internal sections (memory, rules,
session context) are assembled in a **fixed, documented order**.  The order
is determined by `build_injection_context()` which already produces a
deterministic sequence.  No dict-based iteration that would randomize order.

**Impact on downstream systems**:
- `context/manager.py`: Capabilities move from user message to system prompt
- `context/manager.py`: Remove synthetic assistant messages entirely
- `context/manager.py`: Single `[SYSTEM CONTEXT]` user message for memory
- Memory auto-injection (future): appends to `[SYSTEM CONTEXT]` block
- Prompt caching: Stable capability prefix now cached (positive side effect)

**Attention dilution check** (Phase 2 acceptance gate):
- Run a "complex capabilities + large memory" A/B session
- Verify model correctly recalls both capability constraints AND memory details
- If attention dilution observed, add XML-tag demarcation inside sections

### 4.3 Compression Pipeline Simplification

**Current state**: 5 layers, 2 thrashing state machines, CollapseStore is
memory-only.

**Decision**: Merge into 2 phases:

**Phase A — Rule Pre-processing** (runs every turn, zero API cost):
1. Tool output budget caps (ToolResultBudget logic, unchanged)
2. Structural cleanup: merged Snip + Micro into one pass:
   - Clear old tool outputs → `[Old tool result content cleared]`
   - Remove rejected/empty tool turns entirely
   - Both operations in a single backward walk (currently two separate walks)

**Phase B — Semantic Compaction** (runs when token pressure exceeds threshold):
1. Collapse → AutoCompact, OR'd: collapse is cheaper (summarizes a subset),
   AutoCompact covers the whole history. If collapse succeeds, AutoCompact is
   skipped.
2. Single thrashing counter, single cooldown clock → owned by ContextPlanner

**Implementation**:
- Delete `ConversationCompactor._consecutive_compactions` and
  `_steps_since_last_compact` — trust `ContextPlanner` as single source
- Merge `SnipCompactor.snip()` + `MicroCompactor.compact()` into
  `StructuralCompactor.compact()` — single backward pass
- `CollapseStore` gains persistence via `ContextTrimmingState` serialization
  to `sessions.metadata_json` (cheap, cross-restart survival)

**⚠️ Semantic Equivalence Risk**: Snip and Micro currently execute as two
independent passes with implicit priority ordering (Snip deletes empty/rejected
turns first, Micro clears remaining old outputs second).  A single backward
walk where one message matches both criteria must faithfully reproduce the
original two-pass outcome.  If that proves impossible, keeping two passes but
eliminating the state duplication is an acceptable fallback — do not sacrifice
correctness for "single layer" aesthetics.

**Acceptance strategy**:
1. Before merging, capture the current Snip→Micro output as a golden file
   using the existing test suite
2. After merging, diff against the golden file
3. Every discrepancy must be manually confirmed as "harmless or better"
4. If ≥5% of test cases differ in non-trivial ways, fall back to two-pass
   with shared state — the silver is eliminating dual thrashing, not the
   single pass

**Impact on downstream systems**:
- `agent/context_trimming.py`: `_snip_history()` + `_micro_compact()` →
  `_structural_compact()` (single function)
- `context/compaction.py`: SnipCompactor + MicroCompactor →
  StructuralCompactor (single class)
- `context/manager.py`: ContextPlanner becomes single thrashing owner

### 4.4 Runtime Responsibility Rationalization

**Current State**: `SessionRuntime` holds 25 distinct responsibilities. The
exploration agent inventoried every field and method. Three patterns dominate:

1. **Pre-run staging**: `_web_confirm_callbacks`, `_stream_callbacks`,
   `_text_lifecycle_callbacks`, `_text_delta_callbacks`, `_pending_skill_activations`,
   `_session_permission_modes`, `_session_injected_rules`, `_pending_model_switches`,
   `_pending_effort`, `_pending_thinking`, `_pending_skill_modifiers` — all follow
   the same "set by HTTP thread, popped at run start" pattern. Many are lazily
   created with `hasattr` guards (not in `__init__`), not cleaned in `dispose`,
   and have no lock protection.

2. **Service concerns on the wrong object**: `AgentTeamService` logic (500+ lines),
   `WorktreeResolutionService` (background daemon thread, never joined),
   `RunLifecycleService` (160-line `_finalize_run`), `HeadlessApprovalService`
   (approval brokers) — all live on Runtime because it's the only object shared
   between HTTP handlers and agent threads.

3. **Shared mutable state without locks**: `FileReadCache` shared across
   concurrent child sessions, `_last_msg_id_*` dynamic attributes polluting
   namespace unboundedly, `_cancellation_tokens` dict accessed externally via
   raw `getattr` from `agent_service.py`.

**Decision**: Three categories with specific field mappings:

**Category A — Keep in Runtime** (session identity + lifecycle):
| Field | Rationale |
|-------|-----------|
| `_active_sessions` + `_active_sessions_lock` | TOCTOU guard — physical session isolation |
| `_cancellation_tokens` | Session lifecycle — tied to thread creation/cancellation |
| `_background_runs` + `_background_runs_lock` | Background execution tracking — ties to cancellation |
| `_backend_store` | Per-session model isolation — needs atomicity with TOCTOU |
| `_spawn_lock` + `_spawn_reservations` | Governor admission serialization — execution concern |
| `_shared_executor` | Thread pool — lifecycle-bound |
| `_evidence_stores` (EvidenceStoreManager) | Run-scoped evidence — internally synchronized |

**Category B — Move to `SessionPreRunConfig` dataclass** (unified staging):
All 11 `_pending_*` / `_*_callbacks` staging dicts collapse into a single
dataclass stored in one dict: `_pending_config: dict[str, SessionPreRunConfig]`.
This eliminates:
- 11 separate dicts with no lock protection
- 4 lazy `hasattr` initializations (fields invisible to `__init__`, `dispose`, `cleanup`)
- The leak risk where staging callbacks remain if `run_session` raises before popping

```python
@dataclass
class SessionPreRunConfig:
    """All pre-run staging state for one session — consumed once, then discarded.

    **Serializability contract**: ``created_at`` uses ``time.perf_counter()``
    (monotonic clock for interval measurement, relative to an arbitrary origin).
    Perf-counter values are NOT wall-clock timestamps and are NOT serializable.
    Never pickle/JSON-serialize this field — it exists purely for staleness
    detection within a single process lifetime.  If cross-process transfer is
    ever needed, replace with ``time.time_ns()`` + independent staleness math.

    Do NOT add ``__getstate__`` / ``__setstate__`` to work around this —
    cross-process transfer of pre-run staging state is a design error, not a
    serialization gap.  Pre-run configs that leave the creating process should
    fail loudly, not silently carry a meaningless timestamp.
    """
    created_at: float = 0.0          # time.perf_counter() — NOT serializable, NOT wall-clock
    web_confirm_callback: Callable | None = None
    stream_callback: StreamCallback | None = None
    text_lifecycle_callback: Callable | None = None
    text_delta_callback: Callable | None = None
    pending_skill_activations: list[dict] = field(default_factory=list)
    permission_mode: str | None = None
    injected_rules: tuple = ()
    model_switch: str | None = None
    effort: str | None = None
    thinking: dict | None = None
    skill_modifiers: list = field(default_factory=list)

    _STALENESS_SECONDS: float = 30.0

    @property
    def is_stale(self) -> bool:
        """Config that was set but never consumed may carry stale state."""
        return self.created_at > 0 and (
            time.perf_counter() - self.created_at > self._STALENESS_SECONDS
        )
```

**Staleness guard**: At consumption time, if `is_stale` evaluates to `True`,
log a warning and discard the config (fall back to session defaults).  This
prevents the scenario where a cancelled/error'd session's config is consumed
by a subsequent run for the same session_id.  `dispose()` and
`cleanup_session()` explicitly pop `_pending_config[session_id]` on every
exit path (including parameter validation failures before `run_session`).

**Category C — Move to dedicated services** (misplaced concerns):
| Current Location | Move To | Rationale |
|-----------------|---------|-----------|
| `_teams` + `_team_proposals` + 10 team methods (~500 lines) | `AgentTeamService` | Pure team lifecycle, zero coupling to session runtime |
| `_worktree_queue` + `_worktree_results` + worker thread | `WorktreeResolutionService` | Background daemon never joined, TOCTOU race on result dict |
| `_approval_brokers` | `HeadlessApprovalService` | Transport-layer concern (HTTP/WebSocket) |
| `_publish_run_terminal` + `_finalize_run` (~160 lines) | `RunLifecycleService` | Server-layer concern (CAS update + WS broadcast) |
| `_completion_verifiers` | **Delete** | Dead code — registered but never invoked |
| `_circuit_breaker` | Refactor to per-root-session | Currently shared across all sessions |

**FileReadCache** stays at Runtime level (it IS a runtime-wide singleton for
file-tool consistency), but must add a `threading.Lock` to protect against
concurrent reads from background child sessions sharing the base registry.

**Impact on downstream systems**:
- Tool calling: `FileReadCache` gets a lock; `ToolExecutor` receives it via injection
- Subagent communication: Cancellation tokens and spawn lock remain
- MCP: Per-session backend isolation preserved
- Skill invocation: Pending activations move to `SessionPreRunConfig`, consumed identically

### 4.5 Thread Safety Hardening

**P0 fixes**:

| Component | Fix |
|-----------|-----|
| `ContextPlanner` | Audit instance lifecycle — must be per-agent-run, not shared.  If shared, fix instantiation (create one per `ReActAgent`), do NOT add a lock as band-aid. |
| `CapabilityDescriptor.runtime.error` | Call `sanitize_error()` at storage time in `McpCapabilityProvider.list_descriptors()` (`mcp_provider.py:37`) |
| `ConversationHistory` | Return `tuple` from `to_dicts()` / `to_list()` for type-level immutability.  Internal mutation paths use documented `_mutate()` context. No runtime `copy()` needed. |

**P1 defenses**:

| Component | Fix |
|-----------|-----|
| `_claim_new_messages()` | Move claim state off Runtime entirely. Claim is a **turn-level** operation, not a session-level one.  Each `ReActAgent` or `TurnExecutor` should own its claim cursor (last-seen message id) as a local variable, passed to `_claim_new_messages()` as a parameter.  Runtime should be a stateless conduit: `claim_new_messages(session_id, since_id=X)` returns new messages, no stored state on `self`.  The per-session tracking dict can be temporary scaffolding during migration if needed, but the target state is zero Runtime state for claims. |
| `MemoryContext` per-session methods | Accept `session_id` parameter and store internally keyed by session_id, OR document single-session assumption |

### 4.6 Tool Result Degradation Strategy (replaces Turn-based TTL)

**Original proposal error**: The Turn-based TTL assumed tool result value
decays with turn count.  This is **demonstrably wrong** for coding tasks:
- A Read result at turn 3 may be the editing reference at turn 30
- A Grep result from turn 5 may be re-referenced during cross-file refactoring
- Tool result "hotness" depends on task semantics, not time

Claude Code uses a different paradigm: **Budget-Driven Degradation**.
Full retention until token pressure triggers degradation.  Metadata index
always preserved.  This is the proven correct strategy.

**Decision**: Replace Turn-based TTL with Budget-Driven Degradation.

**Core Principles**:
1. **Zero magic numbers**: All degradation decisions are driven by Token Budget,
   never by hardcoded turn counts
2. **Metadata never lost**: Degradation replaces content with a structured
   summary marker while preserving the index (file path, line range, command,
   exit code).  The model can always know *what* was done even when the *full
   output* is summarized.
3. **Prompt-layer degradation, storage-layer completeness**: Raw content
   always remains in SQLite.  Degradation only affects what is assembled
   into the prompt.  Frontend display and audit trails are unaffected.
4. **Recency protection**: The most recent K tool results (K=3~5) are
   protected from degradation, ensuring the immediate reasoning chain's
   integrity.

**Degradation priority** — applied ONLY when `TokenBudget.compute_plan()`
reports `history > _COMPACTION_TRIGGER_RATIO * history_budget`:

1. **Largest first**: Tool results sorted by content byte length, descending.
   Freeing the biggest results gives the most budget relief per operation.
2. **Referenced-result skip (with constraints)**: If a subsequent message
   contains the file path or tool result ID of this result, skip degradation.
   **Constraints to prevent false positives**:

   - **Time window**: Only check the **last N messages** (N=10) after the
     result.  A file path mentioned 30 turns ago does not protect a result
     from 30 turns ago — stale references are not active reasoning.
   - **Write-invalidation for reads**: If a subsequent Edit/Write tool result
     touches the same file path, the prior Read result for that path is NO
     LONGER protected.  The content has been overwritten; the old read is
     obsolete regardless of whether the model mentioned the file.  This
     requires tool-type awareness (`tool_name in {"Edit", "Write",
     "file_edit", "file_write"}`) but no semantic analysis.

   Example: Turn 3 `Read src/main.py` is NOT protected by turn 8
   `Edit src/main.py` — write-invalidation cancels the protection.
3. **Size tie-breaker**: Among equal-size results, older ones degrade first
   (natural LIFO preference for recent results).

**Degradation product**: Replace tool result content with:
```
[Tool result summarized — {tool_name}]
  File: {file_paths if known}
  Lines: {line_range if known}
  Command: {command if known}
  Exit: {exit_code}
  Summary: {first 200 chars of output}

[Full output preserved in session storage. Use a targeted read if you need
the complete content.]
```

**Contrast with original proposal**:

| Dimension | Turn-based TTL ❌ | Budget-Driven Degradation ✅ |
|-----------|-------------------|------------------------------|
| Trigger | Fixed turn count | Token budget pressure |
| Small-file Read | Cleared after 8 turns (wasteful) | Preserved indefinitely (cheap) |
| Large-output Bash | Cleared after 15 turns (may be needed) | Degraded first (frees most budget) |
| Recoverability | Hard break → forced re-invocation | Gradual → model can decide to re-read |
| Config burden | Per-tool tuning | Unified budget policy, zero config |
| Dead-loop risk | High | Minimal |

**Implementation**:
- Delete `created_at_turn` marker, `_tool_ttl_tier()`, TTL tier constants
- Add `degrade_tool_results(messages: list[dict], budget: int) → list[dict]`
  to `StructuralCompactor` (runs in pre-processing pass when budget is tight)
- Insert a metadata block at degradation time recording file paths and
  exit codes extracted from the original output
- Recent-K protection: skip the last 5 tool results regardless of size

**Impact on downstream systems**:
- Tool calling: no per-result metadata needed (budget-driven, not turn-driven)
- Context budget: degradation runs BEFORE token counting, so TokenBudget sees
  the degraded size
- Memory injection: memory recall results treated as tool results (degraded
  by size, protected by recency)
- Subagent communication: child completion notifications NOT degraded
  (referenced-result skip catches them)

## 5. Migration Phases

### Phase 0: P0 Defect Fixes (no behavioral change)

Scope: Fix thread safety, security, and visibility without changing any API or behavior.

1. **ContextPlanner**: Do NOT add a lock.  Audit whether `ContextPlanner` is
   actually shared across threads.  If it is per-agent-run (probable), the
   architecture is correct and no lock is needed — document the assumption.
   If it IS shared (a global singleton or class variable), fix the
   **instantiation lifecycle** (create one per agent run) rather than
   masking the race with a lock.  A lock on a genuinely shared planner is
   a band-aid over a design error.

2. **McpCapabilityProvider**: Apply `sanitize_error()` to error text at
   storage time in `list_descriptors()` (`mcp_provider.py:37`).

3. **ConversationHistory**: Declare read-only contract on accessors.
   `to_dicts()` and `to_list()` return **tuple** or **frozen copy** — the
   type system prevents mutation, eliminating the need for runtime `copy()`.
   For internal mutation paths (`_trim()`, compaction replacement), add a
   private `_mutate()` context manager that documents the exception.

4. **_RUNTIME_PREFIXES**: Move to single shared constant.  Use
   **length-descending sort** before prefix-match iteration to prevent
   substring shadowing (e.g., `[RUNTIME A]` matching before `[RUNTIME AB]`).
   Or use a **Trie** with O(1) prefix lookup per character — either is
   acceptable; simple `startswith` in an arbitrary-order list is not.

5. **Add 8 missing prefixes**: `[RUNTIME EVIDENCE STATE]`, `[RUNTIME BLOCK]`,
   `[SESSION START HOOK CONTEXT]`, `[Stop hook blocked`, `[Subagent:`,
   `[Skill:`, `<task-notification>`, `[Parent message from`.

**Acceptance**:
- `ContextPlanner` lifecycle confirmed per-agent-run (or fixed to be so)
- `ConversationHistory.to_dicts()` returns tuple — type-checked immutability
- `_RUNTIME_PREFIXES` matching is length-descending or Trie-based
- All 22+ runtime-injected prefix patterns are filtered from frontend display
- Existing tests pass unchanged

### Phase 1: Message Model Simplification

1. Shrink `MessageKind` to single value: `RUNTIME_NOTICE`
2. Remove dead values: USER, ASSISTANT, SYSTEM, TOOL_RESULT, COMPACTION_BOUNDARY, PLAN_CONTEXT
3. Remove `kind = MessageKind.USER/ASSISTANT` reconstruction in `session_store.py:817`
   (this also fixes the latent bug where `role="tool"` messages were labelled `ASSISTANT`)
4. `session_store.py:628`: narrow check to `message.kind == MessageKind.RUNTIME_NOTICE`
5. **DB deserialization compat layer**: Existing SQLite rows may have `kind="USER"` or
   `kind="ASSISTANT"` from old enum values stored in `session_message_archive` or
   `compaction_runs`.  Two-part strategy:

   **Part A — Pre-scan (before any migration code ships)**: Write a read-only
   script that scans all `session_messages`, `session_message_archive`, and
   `compaction_runs` tables for non-standard `kind` values.  Report the
   distribution: `{kind_value: row_count}` per table.  If counts are all zero,
   the compat layer is pure insurance.  If any count > 0, decide whether to
   clean the data (UPDATE existing rows to `NULL`) or keep the compat mapping.
   **Do not assume the compat layer is safe without knowing what's in the DB.**

   **Part B — Runtime compat (code)**: In `_row_to_session()` and
   `list_messages()`, map any unknown `kind` string → `None` (never crash).
   Log a **WARNING**-level message with the offending kind value and session_id,
   using a **process-level dedup set** (`_seen_bad_kinds: set[tuple[str, str]]`)
   to avoid log storms.  Example: `logger.warning("Unknown MessageKind %r in
   session %s — mapped to None", kind_value, session_id)` fires exactly once
   per unique (kind_value, session_id) pair.

6. **`RUNTIME_NOTICE` contract**: Document in `llm/base.py` docstring — "This kind is
   exclusively for transient control-flow messages that must NOT persist to DB and
   must NOT appear in frontend display.  It couples 'non-persistent' and 'non-visible'
   semantics.  If a future use case needs only one of these properties, split into two
   values rather than overloading this one."
7. Search and replace: any remaining `kind == MessageKind.USER/ASSISTANT/SYSTEM...` → `role == "user"/"assistant"/"system"`

**Cost**: ~20 lines changed across 5 files. No behavioral change.

**Tests**:
- Message round-trip (create → persist → read → verify `kind is None`)
- `RUNTIME_NOTICE` still filtered at persist time
- Skill injection messages still blocked from DB by `kind` check
- **Pre-scan script**: all 3 tables scanned, distribution reported, counts verified
- **DB compat**: old row with `kind="USER"` loads without error, produces `kind=None`, logs one WARNING per (kind, session) pair
- **Dedup**: 100 identical bad-kind rows in one session → exactly 1 WARNING log line
- Existing `test_react_turn_seams.py` assertion updated to check only `RUNTIME_NOTICE`

### Phase 2: Capability Context Simplification

1. Move `[CAPABILITY CONTEXT]` into system prompt via `StructuredContext.add_layer()`
   (priority=PROJECT, cacheable=True — joins stable prompt-cache prefix)
2. Long-term memory + rules → single `[SYSTEM CONTEXT]` user message
3. Remove synthetic assistant acknowledgments in `ContextManager` entirely
4. Remove dead fallback path in `runtime_prompt_builder.py:77-80`
5. Document fixed internal ordering of `[SYSTEM CONTEXT]` sections
6. After implementation: run A/B attention test (capabilities + memory recall)

**Tests**:
- Capabilities appear in system prompt, not user message
- ContextManager output: exactly one `[SYSTEM CONTEXT]` block (memory only)
- No `Understood. I will use...` messages in output
- Memory auto-injection appends to `[SYSTEM CONTEXT]` block
- `[SYSTEM CONTEXT]` internal section order is deterministic (not dict-iteration based)
- A/B test: model correctly recalls memory details with new format

### Phase 3: Compression Pipeline Rationalization

1. Merge `SnipCompactor` + `MicroCompactor` → `StructuralCompactor`
2. Remove duplicate thrashing state from `ConversationCompactor`
3. Persist `CollapseStore` via `ContextTrimmingState` serialization
4. Unify collapse + compaction decision into single `ContextPlanner` path

**Tests**:
- Structural compaction produces same result as Snip+Micro in sequence
- Thrashing protection: `ContextPlanner` is sole source of truth
- CollapseStore survives serialization round-trip `to_dicts()` → `from_dicts()`

### Phase 4a: Runtime Staging Rationalization (low risk)

1. Create `SessionPreRunConfig` dataclass — unify 11 staging dicts into one
2. Replace `_pending_config: dict[str, SessionPreRunConfig]` in Runtime
3. Move staging consumers (`chat_pipeline.py`, `agent_service.py`) to use `SessionPreRunConfig`
4. `_claim_new_messages()`: move last-seen cursor to `ReActAgent` local state (target).
   Transitional per-session dict accepted during migration only.
5. Delete dead `_completion_verifiers` code
6. `_RUNTIME_PREFIXES` as single shared constant imported by both `session_store.py` and `runtime.py`

**Tests**:
- `SessionPreRunConfig` consumed once, then discarded — no leak if `run_session` raises early
- `SessionPreRunConfig` staleness guard: 30s+ stale config logged + discarded
- `claim_new_messages()` with explicit `since_id` parameter produces same output as dynamic-attr version
- All 22+ runtime-injected prefixes filtered from both frontend and DB

### Phase 4b: Service Extraction (high risk — requires interface design first)

**⚠️ Do NOT start 4b before producing a dedicated "Service Interface Design" document.**

The following responsibilities are currently deep-coupled in `SessionRuntime`
(500+ lines of team lifecycle, daemon thread for worktree, 160-line
`_finalize_run`).  Moving them is NOT a cut-and-paste operation — each
requires defining an explicit communication contract between the extracted
service and Runtime.

| Current Location | Extract To | Communication Pattern | Interface Design Required |
|-----------------|-----------|----------------------|--------------------------|
| `_teams` + 10 team methods (~500 lines) | `AgentTeamService` | Callbacks for session state transitions; event bus for team lifecycle events | Which Runtime fields does team logic access via `self.*`? Define explicit parameter surface |
| `_worktree_queue` + `_worktree_results` + worker thread | `WorktreeResolutionService` | Async queue with result callback; Runtime provides file-system access | TOCTOU race on result dict must be fixed in service interface |
| `_approval_brokers` | `HeadlessApprovalService` | Per-session broker creation on-demand; HTTP handler access pattern unchanged | Broker lookup path must not change for HTTP handlers |
| `_publish_run_terminal` + `_finalize_run` (~160 lines) | `RunLifecycleService` | CAS update + WS broadcast as atomic unit; injected as callback | Evidence evaluation + CAS must be testable independently of Runtime |

**Pre-4b acceptance gate**: Each service above must have a documented:
1. Interface definition (parameters, return types, error modes)
2. Communication contract with Runtime (direct call? callback? event?)
3. List of `self.*` Runtime attributes it currently accesses (to be replaced with explicit parameters)
4. Thread safety guarantee (single-threaded? lock-protected? immutable?)

**FileReadCache** stays at Runtime level — it IS a runtime-wide singleton
by design (tool consistency across all sessions).  But instead of adding a
lock (which only prevents corruption, not semantic errors), investigate
content-addressed cache keys (sha256 of file path + mtime) to make it
**immutable by construction**.

If content-addressing is infeasible in Phase 4b scope, add `threading.Lock()`
as temporary hardening with a TODO to migrate to content-addressed or
per-session isolation in a follow-up.

### Phase 5: Tool Result Degradation (replaces Turn-based TTL)

1. Delete all Turn-based TTL code: `created_at_turn`, `_tool_ttl_tier()`, tier constants
2. Implement `StructuralCompactor.degrade_tool_results(messages, budget) → list[dict]`
3. Degradation product: structured summary marker preserving file path, line range,
   command, exit code, first 200 chars of output
4. Degradation triggers ONLY when TokenBudget reports history over threshold
5. Recent K protection: last 5 tool results never degraded
6. Referenced-result skip with constraints: last 10 messages only; write-invalidation
   for Read results when a subsequent Edit/Write touches the same path
7. Size-prioritized: largest results degraded first; older as tie-breaker
8. Storage layer unchanged: raw content always in DB; degradation is prompt-only

**Tests**:
- Degradation runs only when budget tight, not every turn
- Last 5 results always present (recency protection)
- Referenced file path in assistant message protects result from degradation
- Degraded result still shows file path + exit code + 200-char summary
- Referenced-result skip only active for mentions in last 10 messages, not stale references
- Write-invalidation: Read result degrades if a later Edit/Write touched the same file
- No degradation at all when budget is healthy
- 30-turn coding session: no read-forget-reread dead loop
- Zero hardcoded turn thresholds anywhere in the codebase

## 6. Impact Analysis Matrix

| Downstream System | Phases Affected | Risk | Mitigation |
|-------------------|----------------|------|------------|
| Memory auto-injection | Phase 2 | Low | Memory results degraded by size like other tool results, protected by recency |
| Subagent communication | Phase 4a | Low | `CancellationToken` + `_spawn_lock` unchanged |
| Tool calling (ToolExecutor) | Phase 4a, 4b, 5 | Medium | `FileReadCache` → content-addressed; degradation is prompt-only |
| MCP integration | Phase 0 | Low | Sanitize at source, existing render-time sanitize remains |
| Skill invocation | Phase 2 | Low | Preloaded skills remain separate (`[PRELOADED SKILLS]` block) |
| Context budget (TokenBudget) | Phase 3, 5 | Medium | Degradation runs BEFORE token counting; structural compact same output |
| Frontend (ArchitectureExplorer) | Phase 1 | Low | `_RUNTIME_PREFIXES` filter unchanged |
| Langfuse / observability | Phase 2 | Low | Context message count decreases (fewer messages), stats adapt |
| Plan mode / reflection | None | None | Injection paths unchanged |

## 7. Acceptance Checklist

### Phase 0: P0 Defect Fixes

- [ ] `ContextPlanner` lifecycle audited: confirmed per-agent-run (not shared).  If shared, fixed to per-agent-run — no lock added.
- [ ] `McpCapabilityProvider.list_descriptors()` applies `sanitize_error()` to error text
- [ ] `ConversationHistory.to_dicts()` and `to_list()` return `tuple` — type-level immutability, no runtime `copy()`
- [ ] `ConversationHistory` internal mutation paths use documented `_mutate()` context
- [ ] `_RUNTIME_PREFIXES` moved to single shared constant (not two copies)
- [ ] Prefix matching is length-descending sorted or Trie-based — not arbitrary `startswith` iteration
- [ ] 8 newly-added prefixes: `[RUNTIME EVIDENCE STATE]`, `[RUNTIME BLOCK]`, `[SESSION START HOOK CONTEXT]`, `[Stop hook blocked`, `[Subagent:`, `[Skill:`, `<task-notification>`, `[Parent message from`
- [ ] All existing tests pass unchanged

### Phase 1: Message Model Simplification

- [x] `MessageKind` enum has only `RUNTIME_NOTICE`
- [x] All 6 dead values removed: USER, ASSISTANT, SYSTEM, TOOL_RESULT, COMPACTION_BOUNDARY, PLAN_CONTEXT
- [x] `session_store.py:817` no longer reconstructs `kind` from `role`
- [x] `session_store.py:628` gate narrowed to `message.kind is MessageKind.RUNTIME_NOTICE`
- [x] DB compat: pre-scan confirmed zero kind columns in any DB table — kind was never persisted
- [x] No WARNING needed: there is no kind column in schema, no data to map. Compat is:
     `kind=None` for all DB-read messages (the new default), which is correct for all cases.
- [x] `role="tool"` messages no longer incorrectly labelled `kind=ASSISTANT` on read-back
- [x] `RUNTIME_NOTICE` contract documented in `llm/base.py` (non-persistent AND non-visible; split if these diverge)
- [x] Zero production code branches on removed MessageKind values (confirmed: none existed)
- [x] `RUNTIME_NOTICE` messages still filtered from both frontend display and DB persistence
- [x] Skill activation messages in `entry/chat.py:386` still blocked from DB
- [x] 111/111 tests pass, 0 regressions

### Phase 2: Capability Context Simplification

- [x] Capability context moved to system prompt via `StructuredContext` (priority=PROJECT, cacheable=True)
- [x] Long-term memory → single `[SYSTEM CONTEXT]` user message (no ack)
- [x] Zero synthetic assistant acknowledgment messages in ContextManager output
- [x] Dead fallback path in `runtime_prompt_builder.py` removed
- [x] `[SYSTEM CONTEXT]` internal section order guaranteed by `build_injection_context()` (already deterministic)
- [x] Token savings: ~200-400 tokens per request (2 assistant acks removed)
- [ ] A/B attention test: model correctly recalls memory details AND capability constraints
- [ ] If attention dilution observed: add XML-tag demarcation, do NOT revert to multi-message
- [x] 103/103 tests pass, 0 regressions

### Phase 3: Compression Pipeline Rationalization

- [x] Only one structural pre-processing function (not Snip + Micro separately)
- [x] `ConversationCompactor` does not own thrashing counters
- [x] `ContextPlanner` is the single source of thrashing truth
- [x] `CollapseStore` survives `to_json()` / `from_json()` round-trip
- [x] Pre-merge golden file captured from current Snip→Micro output
- [x] Post-merge diff against golden file: 0/7 diffs, 0 non-trivial diffs — GATE PASS
- [x] If >=5% non-trivial diffs fallback not needed (0% diffs)
- [x] Compaction tests produce identical output (byte-equivalent)
- [x] 103/103 tests pass, 0 regressions

### Phase 4a: Runtime Staging Rationalization

- [ ] `SessionPreRunConfig` dataclass defined — unifies 11 staging dicts, includes `created_at` timestamp + `is_stale` property
- [ ] `_pending_config: dict[str, SessionPreRunConfig]` replaces all `_pending_*` / `_*_callbacks` dicts
- [ ] 4 lazy `hasattr` initializations removed (`_pending_model_switches`, `_pending_effort`, `_pending_thinking`, `_pending_skill_modifiers`)
- [ ] `chat_pipeline.py` sets `SessionPreRunConfig` fields instead of calling Runtime staging methods
- [ ] `agent_service.py` uses `SessionPreRunConfig` for permission mode + rules staging
- [ ] Staleness guard: config >30s old at consumption → logged warning + discarded
- [ ] `dispose()` and `cleanup_session()` pop `_pending_config[session_id]` on ALL exit paths
- [ ] `_claim_new_messages()`: last-seen cursor moved to `ReActAgent` local state. Runtime signature: `claim_new_messages(session_id, since_id) → list` with zero stored state on self
- [ ] Dead `_completion_verifiers` code removed (registered but never invoked)
- [ ] `_RUNTIME_PREFIXES` moved to shared constant imported by both `session_store.py` and `runtime.py`

### Phase 4b: Service Extraction

- [ ] "Service Interface Design" document produced BEFORE any code extraction (required gate)
- [ ] Each service has documented: interface, communication contract, `self.*` dependency list, thread safety guarantee
- [ ] `AgentTeamService`: 10 team methods extracted, explicit parameter surface replaces `self.*` Runtime access
- [ ] `WorktreeResolutionService`: worker thread + queue extracted, TOCTOU race on result dict fixed in interface
- [ ] `HeadlessApprovalService`: broker creation + lookup extracted, HTTP handler path unchanged
- [ ] `RunLifecycleService`: `_finalize_run` + `_publish_run_terminal` extracted, CAS + WS broadcast as atomic unit
- [ ] `FileReadCache`: investigate content-addressed keys (sha256 of path + mtime). If infeasible, add `threading.Lock()` as temporary hardening with TODO

### Phase 5: Tool Result Degradation

- [ ] All Turn-based TTL code deleted: `created_at_turn`, `_tool_ttl_tier()`, tier constants
- [ ] `StructuralCompactor.degrade_tool_results(messages, budget)` implemented
- [ ] Degradation only triggers when TokenBudget reports history over threshold
- [ ] Largest results degraded first; older as tie-breaker (not primary sort key)
- [ ] Recent K=5 results protected regardless of size
- [ ] Referenced-result skip: only last N=10 messages checked; file mentions ≥10 turns old do not protect
- [ ] Write-invalidation: Read result for file X loses protection if subsequent Edit/Write for file X exists
- [ ] Degraded result shows structured metadata: file_path, line_range, command, exit_code, 200-char summary
- [ ] Storage layer unchanged — raw content always in DB; degradation is prompt-layer only
- [ ] 30-turn coding session test: zero read-forget-reread dead loops
- [ ] Zero hardcoded turn thresholds anywhere in the codebase

### Final Cross-Phase Acceptance

- [ ] All 70+ existing capability/context/architecture tests pass
- [ ] Context planner + compaction + overview tests pass
- [ ] No regression in model-visible prompt content quality
- [ ] Session isolation: two concurrent sessions do not share mutable state
- [ ] No MCP error text leaks into descriptors without sanitization
- [ ] Compression pipeline: single decision path, single thrashing owner
- [ ] Runtime: session container only, no tool-level state

## 8. Interaction with Existing Capability Index

The capability index implementation (Phases 0-7) is **not affected** by this
refactoring at the producer level.  The two efforts are orthogonal:

- Capability Index: "what capabilities exist and how are they described"
- Context Refactoring: "how are descriptions assembled into the prompt"

The `[CAPABILITY CONTEXT]` block produced by `build_capability_context()` is
a data input to the system prompt's capability layer — the producer doesn't
change, only where it's injected (user message → system prompt).  This is a
**priority upgrade** (system > user), not a downgrade.
