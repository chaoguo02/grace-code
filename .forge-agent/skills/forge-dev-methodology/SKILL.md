---
name: forge-dev-methodology
description: >
  Systematic methodology for developing and fixing the forge-agent project.
  Use when adding features, fixing bugs, or refactoring any module.
  Covers the complete cycle: audit → research → plan → implement → critique → iterate.
  All changes must be root-cause fixes, not patches. Align with Claude Code patterns.
when_to_use: >
  Use when starting ANY development task on the forge-agent project.
  This skill ensures you follow the proven methodology that took the
  codebase from 67% to 91% CC alignment with 30+ batches of clean fixes.
---

# Forge Agent Development Methodology

## Core Principles

1. **Root cause, never patch.** If a fix feels like a workaround, it is. Find the architectural problem and fix it there.

2. **CC-aligned, not CC-copied.** Understand Claude Code's design intent, then implement the equivalent pattern in our architecture. Adapt for Python/Web differences.

3. **Callback layering, not cross-layer access.** Agent layer (runtime) never touches server layer (WS/HTTP). Use injected callbacks — same pattern as `_event_callback`, `_web_confirm_callbacks`, `_worktree_completion_callback`.

4. **Typed contracts, not ad-hoc dicts.** Every cross-boundary data structure (WS events, API responses) must have a typed definition. Python dataclasses on the backend, TypeScript interfaces on the frontend. One source of truth.

5. **Batch, commit, reflect, iterate.** Every batch ≤ 5 files. Commit after each. Write a global reflection. Pause before the next batch.

## Development Cycle

### Phase 1: Audit

Read the relevant code paths end-to-end. Do NOT jump to implementation.

- Trace every function call from entry point to exit
- Document what data flows in and out at each step  
- Identify all edge cases: errors, empty states, race conditions, concurrency
- Note where our design diverges from CC and why

Output: a list of concrete defects, not vague concerns. Format: `[file:line] — specific problem — impact`.

### Phase 2: Research

Websearch how Claude Code handles the same scenario. Key sources:

- [Claude Code official docs](https://code.claude.com/docs/en/)
- [wuwangzhang1216 source analysis](https://github.com/wuwangzhang1216/claude-code-source-all-in-one)
- [openedclaude.github.io](https://openedclaude.github.io/claude-reviews-claude/)
- GitHub issues for known bugs/limitations

For each finding, note the SOURCE URL. Distinguish documented behavior from community-observed behavior. Identify which CC patterns we should adopt and which we should intentionally diverge from.

### Phase 3: Plan

Write a plan document in `docs/`. Structure:

```markdown
# Plan: <topic>

## Context — why this change, what problem it solves

## Current state — file:line references to existing code

## Target state — what CC does, with source URLs

## Gap analysis — specific differences with severity (🔴🟡🟢)

## Implementation steps — per-batch breakdown with files

## Verification — how to test each batch end-to-end
```

Get user approval before writing any code.

### Phase 4: Implement

Implement in batches. Rules:

- **Each batch ≤ 5 files.** If a change touches more, split it.
- **Commit after each batch.** Descriptive message with `Co-Authored-By: Claude`.
- **Reflect globally after each batch.** What changed? Any side effects? Next batch ready?
- **Push after each batch.** Fail fast, don't accumulate unreviewed changes.

Code patterns to use:

```python
# ✅ Callback injection (clean layering)
self._runtime.set_worktree_completion_callback(callback)

# ✅ Typed events
event_bus.publish_typed(session_id, WsPlanReady(...))

# ❌ Cross-layer access
event_bus.publish_raw(session_id, {"type": "plan_ready", ...})  # avoid

# ❌ Dynamic attributes
getattr(event, "child_session_id", None)  # use typed fields instead
```

### Phase 5: Critique

After each batch, ask:

1. **Does this fix the root cause or mask a symptom?** If it adds a flag/check/fallback without changing the underlying design, it's a patch.

2. **Does this introduce coupling between layers?** Agent layer should never import from server layer. Server layer can depend on agent layer.

3. **Does this handle all states?** Empty, error, concurrent, refresh, timeout — every state must have a defined behavior.

4. **Would this survive a page refresh?** Frontend state that's only in memory is fragile. Persist to API or derive from authoritative sources.

5. **Is the type contract complete?** If you add a field to a WS event, update both `server/events.py` and `web/src/types/`.

### Phase 6: Iterate

Critique will find new problems. Fix them using the SAME methodology — not quick patches. Each iteration should increase the quality score. Stop when the score reaches 8.5+.

## Design Patterns

### Permission Pipeline (hitl/pipeline.py)

```
Layer 1: validateInput → DENY (bypass-immune)
Layer 2: PreToolUse Hooks → DENY/ALLOW/CONTINUE
Layer 3: deny → ask → allow (Phase 1 vs Phase 2)
Layer 4: Permission Mode (bypassPermissions/acceptEdits/plan/dontAsk)
Layer 5: Path Sandbox
Layer 6: Interactive Callback (AUTO → Web callback → TTY → DENY)
```

Key: `_force_interactive` flag propagates through Layers 4-6. Ask rules set it; plan/dontAsk check it.

### Event System (server/events.py + event_bus.py)

```python
# Define event shape ONCE in server/events.py
@dataclass
class WsPlanReady:
    type: Literal["plan_ready"] = "plan_ready"
    plan_text: str = ""
    contract: dict | None = None
    ...

# Emit via publish_typed (type-safe)
event_bus.publish_typed(session_id, WsPlanReady(...))

# Frontend mirrors in web/src/types/
export interface WsMessage {
    type: "plan_ready";
    plan_text?: string;
    contract?: Record<string, unknown> | null;
}
```

### Callback Layering (runtime ↔ server)

Agent layer (runtime) exposes callback registration. Server layer injects WS/HTTP logic.

```python
# Runtime — agnostic of transport
def set_worktree_completion_callback(self, callback): ...

# AgentService — injects WS push
def _on_worktree_done(parent_id, child_id, action, status):
    event_bus.publish_typed(parent_id, WsWorktreeResolved(...))
runtime.set_worktree_completion_callback(_on_worktree_done)
```

### State Machine (Worktree, Plan, etc.)

Every async operation needs explicit states:

```
idle → queued → processing → applied/discarded/retained
                            → failed → retry
```

Frontend renders each state differently. Never assume 202 = done.

## Common Pitfalls

| Pitfall | Why it happens | How to avoid |
|---------|---------------|--------------|
| Optimistic update | 202 Accepted treated as success | Wait for WS confirmation event |
| Wrong field check | Using metadata.path instead of disposition enum | Use authoritative enum values from API |
| Raw dict for events | Quick to write, impossible to validate | Use typed dataclass + publish_typed |
| Broker detection | Checking transient state for mode decision | Use persistent flag (_is_web_mode) |
| JSON file storage | Fast to implement, unsafe for concurrent access | SQLite from the start |
| Cross-layer Event construction | Runtime creating server-layer objects | Callback injection |
| Regex parsing structured data | Quick hack that breaks on format change | Tool function calling with JSON schema |

## File Map

| Concern | File |
|---------|------|
| Permission pipeline | `hitl/pipeline.py` |
| Permission rules | `hitl/permission_rule.py` |
| Completion guard | `agent/completion_guard.py` |
| Agent main loop | `agent/core.py` |
| Session runtime | `agent/session/runtime.py` |
| Subagent execution | `agent/session/subagent.py` |
| WS event types | `server/events.py` |
| WS event bus | `server/services/event_bus.py` |
| Web agent service | `server/services/agent_service.py` |
| Approval broker | `server/services/approval_broker.py` |
| Session API | `server/routers/sessions.py` |
| Plan API | `server/routers/approvals.py` |
| Frontend store | `web/src/stores/chatStore.ts` |
| Frontend types | `web/src/types/session.ts` |
| MCP protocol | `mcp/protocol.py` |
| MCP transport | `mcp/transport.py` |
| Plan revision storage | `server/services/plan_revision_service.py` |
| Worktree service | `agent/session/worktree_service.py` |

## Quality Gates

Before marking a batch as "done":

- [ ] Python syntax check: `python -c "import ast; ast.parse(open('file.py').read())"`
- [ ] No cross-layer imports (agent/ importing from server/)
- [ ] All new WS events have typed dataclass
- [ ] Frontend types match backend dataclass fields
- [ ] States documented: idle/processing/done/error for every async op
- [ ] Page refresh handled: state derived from API, not memory-only
- [ ] Commit message explains what AND why
