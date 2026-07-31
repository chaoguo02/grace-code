# Skill System Normalization Design

## 1. Purpose

This document defines a phased normalization of the Skill subsystem based on
an 11-dimension audit.  The Skill subsystem is mature — 7-source discovery,
concurrent loading, live reload, 4 activation paths, evidence recording,
full rendering pipeline.  But one critical bug and several isolation gaps exist.

## 2. Core Principles

### 2.1 Normalization, Not Refactoring
### 2.2 DDR-First Development
### 2.3 Vertical Integration
### 2.4 Fail-Closed Defaults
### 2.5 明确不做

| Anti-requirement | Reason |
|-----------------|--------|
| Auto-activation by path scope | CC uses LLM semantic matching, not regex triggers |
| Skill dependency graph | CC has no skill-to-skill dependency — orthogonal concern |
| skills-lock.json implementation | File format exists but no code reads it — reserved for future external tooling |
| Auto-generated skill descriptions | CC proves hand-written is superior |

## 3. Gap Analysis Summary

| # | Gap | Severity | Phase | Type |
|---|-----|----------|-------|------|
| 1 | Fork-via-SkillMetadata: AttributeError at runtime | 🔴 Critical | Phase 0 | Bug |
| 2 | runtime_prompt_builder.py: `Any` import not at top | 🟡 Low | Phase 0 | Bug |
| 3 | No MCP dependency validation at skill load | 🟡 Medium | Phase 1 | Defect |
| 4 | Missing skill silently swallowed in preload | 🟡 Medium | Phase 1 | Defect |
| 5 | Legacy commands format has description gap | 🟡 Medium | Phase 2 | Experience |
| 6 | Untrusted MCP skill: inline commands blocked without audit log | 🟡 Low | Phase 2 | Security |
| 7 | `triggers` field: silently ignored (should log deprecation) | 🟢 Low | Phase 3 | Engineering |
| 8 | `_load_mcp_dependencies` no validation | 🟡 Medium | Phase 1 | Defect |
| 9 | `SkillMetadata` no agent_kind → fork crash | 🔴 Critical | Phase 0 | Bug (same as #1) |
| 10 | `_discover_source` exception silence too aggressive | 🟡 Medium | Phase 2 | Defect |

## 4. Design Decisions

### 4.0 Phase 0: Critical Bug Fix

#### #1: Fork-via-SkillMetadata AttributeError

**Problem**: `entry/chat.py:397-402` passes `SkillMetadata` as `definition=meta`
to `AgentSpawnRequest.named()`.  `AgentSpawnRequest.__post_init__` checks
`definition.agent_kind is AgentKind.NAMED_SUBAGENT` (`agent/session/models.py:639`).
`SkillMetadata` has no `agent_kind` attribute → **AttributeError at runtime**
for any `context="fork"` skill activated via CLI.

**CC reference**: CC's SkillMetadata carries enough fields to satisfy the
spawn interface.  The fix is not to make `SkillMetadata` a full AgentDefinition
duplicate — it's to add the minimum fields needed for the spawn protocol.

**Decision**: Add `agent_kind = AgentKind.NAMED_SUBAGENT` as a computed property
on `SkillMetadata` when `context == "fork"`.  Add `description` (already present),
`intent` (default to `TaskIntent.EDIT`), and `tools` (default to frozenset of
allowed_tools from metadata) as computed properties for the spawn path.

**Implementation**:
```python
# skills/registry.py — SkillMetadata class
@property
def agent_kind(self) -> "AgentKind":
    from agent.session.models import AgentKind
    return AgentKind.NAMED_SUBAGENT

@property
def intent(self) -> "TaskIntent":
    from agent.task import TaskIntent
    return TaskIntent.EDIT
```

This is the minimum viable fix — `SkillMetadata` satisfies the duck-type
requirements of `AgentSpawnRequest.named()` without becoming an AgentDefinition.

#### #2: Any import location

**Problem**: `runtime_prompt_builder.py:27` uses `Any` in function signature
but only imports it inside `TYPE_CHECKING` block (line 10).

**Decision**: Move `Any` import to the top-level imports.

### 4.1 Phase 1: Defect Fixes

#### #3: MCP Dependency Validation at Skill Load

**Problem**: `_load_mcp_dependencies()` reads server names from
`agents/openai.yaml` but never validates that those servers exist in
the session's MCP configuration.  The mismatch is only discovered at
activation time when tools are silently absent.

**Decision**: During `_discover_source()`, after parsing `_load_mcp_dependencies()`,
cross-reference each server name against the session's MCP config if available.
Log a WARNING for each unmatched server.  Store the validation result in
`SkillMetadata._mcp_validation_warnings` for the architecture inspector.
Skills with missing MCP dependencies are still registered and callable —
the model receives the MCP annotation and can reason about availability.

#### #4: Missing Skill in Preload — Surface Error

**Problem**: `runtime_prompt_builder.py:157-162` logs a WARNING and silently
omits the skill when `load_and_render()` returns `None`.  The agent
silently starts with a missing skill.

**Decision**: Change from `logging.WARNING` to `logging.ERROR`.  If a
skill is preloaded and can't be found, the agent should know about it.
Add `(skill "{name}" failed to load — file missing or malformed)` as a
runtime notice message appended after the `[PRELOADED SKILLS]` block.

### 4.2 Phase 2: Experience and Security

#### #5: Legacy Commands Description Default

**Problem**: Legacy commands (flat `.md` files with no frontmatter) get
`description=""` — they appear in the skills listing with no description
for LLM matching.

**Decision**: If no frontmatter description, use the first 100 chars of
the file body as the description.  This matches CC's behavior where
legacy commands get a best-effort description from content.

#### #6: Untrusted MCP Skill Audit Log

**Problem**: Untrusted MCP skills have all inline commands replaced with
`"[blocked: untrusted skill inline command]"` before execution
(`skills/registry.py:705-715`).  No audit log records which commands
were blocked.

**Decision**: Add `logger.warning("Blocked inline command in untrusted skill %s: %r", skill_name, cmd)` before replacement.  Simple, no new infrastructure.

### 4.3 Phase 3: Engineering Cleanup

#### #7: Deprecation Warning for `triggers` Field

**Problem**: docstring says triggers were removed but old YAML with
`triggers:` is silently ignored.

**Decision**: Add `logger.info("Skill %s has deprecated 'triggers' field — ignored. Use 'description' for LLM semantic matching instead.", name)` when triggers key is detected during frontmatter parsing.

#### #8: Exception Silence in _discover_source

**Problem**: `_discover_source()` catches all exceptions in its thread-pool
future, logs a WARNING, and replaces results with `({}, {})`.  If a source
directory is missing or misconfigured, the entire source is silently dropped.

**Decision**: Keep the WARNING log but also store the exception message
in `SkillRegistry._source_errors: dict[str, str]` for the architecture
inspector.  This makes discovery failures visible at the UI level.

## 5. Migration Phases

### Phase 0: Critical Bug Fix (2 items)
- #1: Fork-via-SkillMetadata AttributeError
- #2: Any import fix

### Phase 1: Defect Fixes (2 items)
- #3: MCP dependency validation
- #4: Missing skill surface error

### Phase 2: Experience and Security (2 items)
- #5: Legacy commands description
- #6: Untrusted skill audit log

### Phase 3: Engineering Cleanup (2 items)
- #7: triggers deprecation warning
- #8: _discover_source error storage

## 6. Impact Analysis Matrix

| Downstream System | Phases Affected | Risk | Mitigation |
|-------------------|----------------|------|------------|
| CLI skill activation | Phase 0 | High | #1 fixes crash — critical |
| Subagent spawning | Phase 0 | Low | Skill fork path uses spawn — same fix |
| MCP integration | Phase 1 | Low | Validation is read-only, no behavioral change |
| Runtime prompt builder | Phase 1 | Low | Error notice is additive |
| Capability index | None | None | SkillCapabilityProvider unaffected |
| Architecture explorer | Phase 3 | Low | Source errors + deprecation visible in UI |

## 7. Acceptance Checklist

### Phase 0

- [ ] `SkillMetadata.agent_kind` computed property — returns NAMED_SUBAGENT
- [ ] `SkillMetadata.intent` computed property — returns EDIT
- [ ] CLI `/skill-name` with context=fork → spawns subagent without AttributeError
- [ ] `Any` imported at top level in runtime_prompt_builder.py
- [ ] No NameError on type introspection

### Phase 1

- [ ] `_load_mcp_dependencies` cross-references session MCP config
- [ ] WARNING logged for each unmatched MCP server at discovery time
- [ ] `_mcp_validation_warnings` stored on SkillMetadata
- [ ] Missing skill preload: ERROR log + runtime notice appended
- [ ] Agent sees explicit "(skill X failed to load)" in preload block

### Phase 2

- [ ] Legacy commands without frontmatter get body-trimmed description
- [ ] Description length capped at 100 chars from body
- [ ] Untrusted skill inline command block: WARNING logged with cmd details
- [ ] Blocked commands visible in log for audit

### Phase 3

- [ ] `triggers` field detection → INFO log with deprecation message
- [ ] `_source_errors` dict populated on source discovery failure
- [ ] Architecture inspector exposes source errors

### Final Cross-Phase

- [ ] Fork-via-skill no longer crashes
- [ ] All 4 activation paths work (tool_call, http_request, cli_slash, preload)
- [ ] MCP dependency mismatches visible at load time (not silent)
- [ ] Legacy commands have usable descriptions
- [ ] Discovery failures visible in UI
