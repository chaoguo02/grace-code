# Skill System Normalization Design

## 1. Purpose

This document defines a phased normalization of the Skill subsystem based on
an 11-dimension audit.  The Skill subsystem is mature — 7-source discovery,
concurrent loading, live reload, 4 activation paths, evidence recording,
full rendering pipeline.  But one critical bug and several isolation gaps exist.

## 2. Core Principles

### 2.1 Normalization, Not Refactoring

Skills are discovered, parsed, loaded, and activated through four distinct
paths (tool call, HTTP request, CLI slash, preload).  All four paths
already work correctly in the common case.  The gaps are in edge-case
error handling (fork crash, missing-dependency silence, legacy-format
description gaps), not in architectural redesign.

### 2.2 Skill = Prompt Template, Not Code Plugin

A skill is a structured prompt fragment with metadata — it is NOT an
executable module.  All "execution" is the LLM interpreting the prompt
and calling tools through the standard pipeline.  A skill itself contains
no runtime logic.  This principle governs: why `SkillMetadata` only
needs to satisfy the minimum spawn protocol for `context="fork"` (#1),
why inline commands (`!cmd`) are blocked for untrusted skills (#6),
and why skill content is injected as a user message rather than
executed as code.

### 2.3 Activation is Semantic, Not Syntactic

Skill activation is driven by the LLM matching `description` and
`when_to_use` against the current task — never by filename, path, regex
trigger, or manual rule engine.  `paths` in `SkillMetadata` is
informational for the model, not a mechanical activation gate.  The
`triggers` field was explicitly removed from the frontmatter reference.
This principle governs: why we don't auto-activate on file path match,
why the description is the primary discovery mechanism (#5), and why
description fidelity matters (#14).

### 2.4 Trust Boundary = Provenance

A skill's trust level is determined by its source (builtin, project,
user, MCP) — NOT by its content.  Untrusted skills (`trusted=False`)
have inline commands blocked at load time and are subject to content
sanitization.  This principle governs: why untrusted skill command
blocking must be auditable (#6), why content sanitization applies at
load time not runtime (#11), and why MCP dependency validation is a
load-time concern (#3).

### 2.5 Skill Isolation = Session Scope

Skill effects (model/effort overrides, tool allow/deny modifications)
are scoped to the session and do not persist across runs or affect
the global registry.  The `SkillContextModifier` is applied at the
start of each run and deactivated at completion.  This principle
governs: why `context="fork"` spawns a subagent rather than forking
the current process (#1), and why preload is a distinct path from
tool-call activation.

### 2.6 Description Fidelity is Non-Negotiable

The skill description is the LLM's sole basis for choosing whether to
activate the skill.  Any programmatic modification of the description
(truncation, splicing, generation) must preserve semantic integrity.
An empty or excessively long description makes the skill invisible to
the LLM.  This principle governs: why legacy-command descriptions must
be meaningful (#5), why descriptions must be validated at registration
time (#14), and why auto-generated descriptions are explicitly
rejected (anti-requirements).

### 2.7 Activation Audit Trail

Every skill activation produces a complete audit record: which turn,
what user intent, which skill was selected, and whether it succeeded.
This is not just observability — it is the verifiability guarantee of
semantic activation.  If a user reports "my skill wasn't activated,"
the audit trail provides the diagnostic evidence.  Governs: why
activation decisions need tracing (#12) and why discovery errors
must be stored (#8).

### 2.8 明确不做

| Anti-requirement | Reason |
|-----------------|--------|
| Auto-activation by path scope | CC uses LLM semantic matching, not regex triggers |
| Skill dependency graph | CC has no skill-to-skill dependency — orthogonal concern |
| skills-lock.json implementation | File format exists but no code reads it — reserved for future external tooling |
| Auto-generated skill descriptions | CC proves hand-written is superior |
| Triggers field revival | Explicitly removed per CC frontmatter reference — description is the matching mechanism |

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

**CC Principle**: Skills are On-Demand Instruction Injection (#2.2) — `context="fork"` spawns a subagent, so `SkillMetadata` must satisfy the spawn protocol.

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
    return self._frontmatter.get("agent_kind", AgentKind.NAMED_SUBAGENT)

@property
def intent(self) -> "TaskIntent":
    from agent.task import TaskIntent
    return self._frontmatter.get("intent", TaskIntent.EDIT)
```

Defaults are NAMED_SUBAGENT and EDIT, but frontmatter can override via agent_kind/intent keys. This satisfies — `SkillMetadata` satisfies the duck-type
requirements of `AgentSpawnRequest.named()` without becoming an AgentDefinition.

#### #2: Any import location

**Problem**: `runtime_prompt_builder.py:27` uses `Any` in function signature
but only imports it inside `TYPE_CHECKING` block (line 10).

**Decision**: Move `Any` import to the top-level imports.

### 4.1 Phase 1: Defect & Security Fixes

#### #3: MCP Dependency Validation at Skill Load

**CC Principle**: Four Activation Paths, One Evidence Contract (#2.4) — MCP dependency mismatches must be traceable to the skill activation event.

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

**CC Principle**: Four Activation Paths, One Evidence Contract (#2.4) — preload failures must produce observable errors.

**Problem**: `runtime_prompt_builder.py:157-162` logs a WARNING and silently
omits the skill when `load_and_render()` returns `None`.  The agent
silently starts with a missing skill.

**Decision**: Change from `logging.WARNING` to `logging.ERROR`.  If a
skill is preloaded and can't be found, the agent should know about it.
Add `(skill "{name}" failed to load — file missing or malformed)` as a
runtime notice message appended after the `[PRELOADED SKILLS]` block.

### 4.2 Phase 2: Experience and Security

#### #5: Legacy Commands Description Default

**CC Principle**: LLM Semantic Matching (#2.3) — descriptions are the primary discovery mechanism; legacy commands without descriptions are invisible to the model.

**Problem**: Legacy commands (flat `.md` files with no frontmatter) get
`description=""` — they appear in the skills listing with no description
for LLM matching.

**Decision**: If no frontmatter description, use the first 100 chars of
the file body as the description.  This matches CC's behavior where
legacy commands get a best-effort description from content.

#### #6: Untrusted MCP Skill Audit Log

**CC Principle**: Untrusted Skills Have Restricted Capabilities (#2.5) — security events must be observable.

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

### Phase 1: Defect and Security Fixes (4 items)
- #3: MCP dependency validation
- #4: Missing skill surface error
- #9: Untrusted skill content sanitization
- #11: Description validation at registration

### Phase 2: Experience and Security (3 items)
- #5: Legacy commands description
- #6: Untrusted skill audit log
- #10: Skill activation audit trail

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
- [ ] #9:  function strips system-prompt-injection patterns from untrusted skills
- [ ] #9: Sanitization runs at load time (parse_frontmatter), not at activation time
- [ ] #11: Description validation at registration — non-empty, < 2000 chars, no markdown artifacts
- [ ] #11: Skills failing validation are registered as degraded with WARNING log
- [ ] #11: Degraded skills still appear in listing with "(description unavailable)" marker
- [ ] `_mcp_validation_warnings` stored on SkillMetadata
- [ ] Missing skill preload: ERROR log + runtime notice appended
- [ ] Agent sees explicit "(skill X failed to load)" in preload block
- [ ] _sanitize_untrusted_content() strips injection patterns for non-builtin skills
- [ ] Builtin skills bypass sanitization
- [ ] Description validation rejects empty / >2000 token / malformed descriptions
- [ ] Non-compliant skills marked degraded with visible annotation

### Phase 2

- [ ] Legacy commands without frontmatter get body-trimmed description
- [ ] Description length capped at 100 chars from body
- [ ] Untrusted skill inline command block: WARNING logged with cmd details
- [ ] #10: Skill activation audit trail records (turn, intent, skill_name, match_reason, outcome) per activation
- [ ] #10: Audit entries stored in session trace for diagnostic queries
- [ ] Blocked commands visible in log for audit
- [ ] Activation audit record: turn_id, intent, matched_skill, match_reason, result
- [ ] Audit records queryable via architecture inspector

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
