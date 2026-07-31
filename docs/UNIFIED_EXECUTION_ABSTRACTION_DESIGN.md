# Unified Execution Abstraction Design

## 1. Current State Audit

### 1.1 Three Parallel Execution Paths

Three subsystems each implement their own interception, permission, hook,
and audit logic.  They converge at the `ToolExecutionPipeline` only at the
final `execute()` call — everything upstream (registration, scope,
activation, visibility) is subsystem-specific.

| Dimension | Native Tools | MCP Tools | Skills |
|-----------|-------------|-----------|--------|
| **Registration** | `ToolRegistry.register()` via `bootstrap/registry_factory.py` | `SyncMCPToolManager.load_and_discover()` → `mcp_tool_to_runtime_tool()` → `build_tool()` → `register_many()` | `SkillRegistry._discover()` → `_parse_frontmatter()` → `SkillMetadata` dict; no `ToolRegistry` registration |
| **Visibility Control** | `PolicyAwareToolRegistry._is_tool_visible()` — 7-layer policy check | `_mcp_tool_names_for_spec()` — per-agent name filter; `ToolAvailabilityGuard` — server-failure blocklist | `SkillCapabilityProvider.list_descriptors()` — `llm_invocable_only` filter; `SkillMetadata.model_invocable` property |
| **Permission (HITL)** | `PermissionPipeline.check()` — 6-layer evaluation | Same as native — MCP tools go through full pipeline | `SkillTool.execute()` — `model_invocable` gate + `context="fork"` gate inline; NO `PermissionPipeline` |
| **Hook Dispatch** | `HookDispatcher.dispatch(PRE_TOOL_USE | POST_TOOL_USE | POST_TOOL_USE_FAILURE)` inside `ToolExecutionPipeline` | Same as native — hooks fire on MCP tool calls through the pipeline | `SkillTool.execute()` — no hook dispatch; `EntryPointHook` fires separately at ChatSession level |
| **Audit / Evidence** | `ToolEvidenceRecorder.record_started/completed/blocked()` inside `ToolExecutionPipeline` | Same as native + `MCP_CONNECTED` / `MCP_TOOLS_EXPOSED` evidence | `SkillActivationService.activate()` → `runtime.record_skill_activation()` → `SKILL_LOADED` evidence; separate path from tool evidence |
| **Activation / Deferred** | N/A (always active) | `is_deferred` / `always_load` flags on `MCPToolProps`; `ToolSearch` activation via `activate_tools()` | `SkillTool.execute()` — returns `SkillContextModifier` in `ToolResult.metadata`; `PolicyAwareToolRegistry._apply_skill_modifier()` consumes it |
| **Deactivation / Cleanup** | N/A | `MCPToolIntegration._replace_server_tools()` — unregister stale, register new | `PolicyAwareToolRegistry.deactivate_skill_modifier()` at end of `run()` |

### 1.2 Current Interception Points (where cross-cutting logic lives)

| Interception Point | Location | What It Does | Which Subsystems Use It |
|-------------------|----------|-------------|------------------------|
| **Tool Registry Registration** | `core/base.py:ToolRegistry.register()` | Validates tool, registers in `_tools` dict, sets `_registry` back-ref | Native (static), MCP (dynamic) |
| **PolicyAwareToolRegistry Construction** | `core/policy_registry.py:_is_tool_visible()` | 7-layer visibility check: allowed_tools, phase_policy, denied_tools, effects subset, strict_file_scope, path-access | Native, MCP |
| **ToolAvailabilityGuard.intercept()** | `core/tool_execution.py:_check_tool_availability()` | Blocks UNAVAILABLE tools (failed MCP servers) | MCP only |
| **PermissionPipeline.check()** | `hitl/pipeline.py:PermissionPipeline.check()` | 6-layer HITL evaluation: validate → hooks → rules → mode → sandbox → callback | Native, MCP. NOT Skills |
| **SkillTool.execute()** | `skills/tool.py:SkillTool.execute()` | model_invocable gate + context gate + load_and_render + modifier construction | Skills only |
| **SkillContextModifier.apply** | `core/policy_registry.py:_apply_skill_modifier()` | Merges allowed/denied tools, activates MCP servers, stores model/effort overrides | Skills only |
| **Hook PreToolUse / PostToolUse** | `core/tool_execution.py:_fire_post_tool_hook()` | Fires POST_TOOL_USE / POST_TOOL_USE_FAILURE | Native, MCP. NOT Skills |
| **Evidence Recorder** | `core/tool_execution.py:ToolEvidenceRecorder` | Records TOOL_CALL_STARTED/COMPLETED/BLOCKED | Native, MCP. Skills use separate SKILL_LOADED path |

### 1.3 Namespace Management

| Concern | Current Implementation | Gap |
|---------|----------------------|-----|
| Native ↔ MCP collision | `assemble_tool_pool()` (`tools/pool.py:32-33`) raises `ValueError` on duplicate names | MCP tools use `mcp__{server}__{tool}` prefix — collision is impossible by convention, but not enforced architecturally |
| Native ↔ Skill collision | No mechanism. Skills are not in `_tools` dict | A skill named "Read" would shadow the native "Read" tool in the skills listing with no warning |
| MCP ↔ Skill collision | No mechanism | An MCP server registering a "review" tool would shadow a "review" skill |
| Deactivation ordering | Skill modifiers deactivated at end of run; MCP tools removed on disconnect | No dependency ordering — if a skill depends on an MCP tool that gets disconnected mid-run, the skill modifier still references it |

## 2. Gap Analysis

| # | Gap | Severity | Type | CC Principle |
|---|-----|----------|------|-------------|
| U1 | MCP tools register via separate path (`SyncMCPToolManager.register` vs `ToolRegistry.register`) | High | Architecture Debt | P1: Everything Callable Is a Tool |
| U2 | Skill activation bypasses `ToolExecutionPipeline` entirely — SkillTool.execute() has its own gate logic | Critical | Architecture Debt | P1, P3: Single Interception Point |
| U3 | Skills are not `BaseTool` instances in `ToolRegistry._tools` — they live in `SkillRegistry._metadata` dict | High | Architecture Debt | P1 |
| U4 | `SkillContextModifier` mutates `PolicyAwareToolRegistry` state in-place, bypassing `PermissionPipeline` visibility rules | High | Architecture Debt | P3 |
| U5 | Skill activation evidence (`SKILL_LOADED`) is a separate path from tool evidence (`TOOL_CALL_COMPLETED`) | Medium | Architecture Debt | P3 |
| U6 | No unified namespace collision resolution — skill names can shadow native/MCP tool names with no warning | Medium | Architecture Debt | P5: Namespace Collision |
| U7 | `ToolAvailabilityGuard` only applies to MCP tools — native tools have no runtime availability concept | Low | Feature Gap | P1 |
| U8 | Deferred activation mechanism is duplicated: `is_deferred`/`always_load` for MCP, `disable_model_invocation` for Skills | Medium | Architecture Debt | P1 |
| U9 | `McpCapabilityProvider` and `SkillCapabilityProvider` produce separate `CapabilitySnapshot` entries — the model sees disjoint listings | Low | Architecture Debt | P1 |

## 3. Phased Improvement Plan

### Phase 1: Skill-as-Tool Convergence

**Objective**: Model every Skill as a `SkillActivationTool` registered in
`ToolRegistry._tools`, with its `execute()` delegating to the existing
`SkillTool.execute()` body.  Skills become first-class tools in the
unified `_tools` dict.

**Scope — included**:
- `SkillActivationTool(BaseTool)` class wrapping `SkillMetadata`
- Registration: `SkillRegistry._discover()` → `ToolRegistry.register(SkillActivationTool(...))`
- Skills appear in `ToolRegistry.tool_names` and `ToolRegistry.get_schemas()`
- `SkillCapabilityProvider.list_descriptors()` reads from `ToolRegistry` (filter by `isinstance(tool, SkillActivationTool)`), not from `SkillRegistry._metadata`

**Scope — NOT included**:
- Unified permission pipeline for Skills (Phase 2)
- Namespace collision enforcement (Phase 3)
- Skill deactivation lifecycle changes (existing `deactivate_skill_modifier()` preserved)

**Changes**:

| File | From | To |
|------|------|-----|
| `skills/tool.py` | `SkillTool(BaseTool)` with `execute()` checking `model_invocable` + `context` gates internally | Extract `_internal_execute()` as pure function; `SkillTool.execute()` calls it. Add `SkillActivationTool(BaseTool)` subclass that wraps `SkillMetadata`, delegates `execute()` to `_internal_execute()`. `SkillActivationTool.metadata.effects = {READ_AGENT_STATE, WRITE_AGENT_STATE}` |
| `skills/registry.py` | `self._metadata: dict[str, SkillMetadata]` | After `_parse_frontmatter()`, register `SkillActivationTool(metadata)` via `self._tool_registry.register(skill_tool)` if `_tool_registry` is wired. `_metadata` dict preserved for backward compat. |
| `skills/registry.py` | `SkillRegistry.__init__()` — no `ToolRegistry` connection | Add `attach_tool_registry(registry)` method. Called from `bootstrap/registry_factory.py` after both registries are created. |
| `capabilities/providers/skill_provider.py` | Iterates `skill_registry.list_skill_entries()`, builds `CapabilityDescriptor` from `SkillMetadata` | Iterates `tool_registry.get_schemas()` filtered by `isinstance(tool, SkillActivationTool)`. Each `SkillActivationTool` carries a `to_llm_schema()` that includes description, when_to_use, path_scopes, mcp_servers. |
| `core/base.py` | `SkillContextModifier` consumed by `PolicyAwareToolRegistry._apply_skill_modifier()` | No change. Skill activation still returns a `ToolResult` with `metadata["skill_modifier"]` — `PolicyAwareToolRegistry.execute_tool()` still detects and applies it. |

**Dependencies**: None. `SkillActivationTool` is additive — it registers a new tool class without changing existing paths.

**Rollback safety**: `SkillRegistry.attach_tool_registry(None)` is a valid no-op call. When no `ToolRegistry` is attached, skills continue to work via the old `SkillRegistry._metadata` path. Production rollout can use a config flag to control whether `attach_tool_registry()` is called, enabling hot rollback without code changes.

**Risk & Mitigation**:

| Risk | Mitigation |
|------|-----------|
| `SkillCapabilityProvider` changing the skills listing format | Capability section format preserved — `_render_skills()` output unchanged |
| `SkillActivationTool` name collision with native tools | Phase 3 adds namespace enforcement. Phase 1 uses `skill:{name}` as runtime name in `_tools` dict |
| `SkillRegistry.format_for_prompt()` legacy callers break | Keep `format_for_prompt()` as compatibility wrapper that delegates to `CapabilityProvider` — no change |

**Acceptance Criteria**:
- [ ] `SkillActivationTool` subclass of `BaseTool` defined with `name`, `description`, `parameters_schema`, `execute()`
- [ ] `SkillRegistry` registers each discovered skill as `SkillActivationTool` in `ToolRegistry`
- [ ] `SkillCapabilityProvider.list_descriptors()` reads skills from `ToolRegistry`
- [ ] `SkillRegistry.format_for_prompt()` continues to work (backward compat)
- [ ] Skill tool calls produce identical behavior before and after

---

### Phase 2: Unified Permission Pipeline for Skills

**Objective**: Skills go through `PermissionPipeline.check()` — just like
native and MCP tools.  `SkillActivationTool.execute()` no longer has its
own inline `model_invocable` gate — that check moves into the permission
pipeline as a declarative property.

**Scope — included**:
- `SkillActivationTool.concurrency_mode()` → `SERIAL` (skill activation is always serial)
- `SkillActivationTool.isReadOnly(params)` → `False` (skill activation modifies agent state)
- `SkillActivationTool.metadata.effects = {READ_AGENT_STATE, WRITE_AGENT_STATE}`
- `model_invocable` gate moves from `SkillTool.execute()` to `PermissionPipeline._layer1_validate()` as a tool-level `permission_denial_reason()` check
- `context="fork"` gate stays in `SkillTool.execute()` — it is execution routing, not permission logic

**Changes**:

| File | From | To |
|------|------|-----|
| `skills/tool.py` | `SkillTool.execute()` checks `model_invocable`, returns BLOCKED error | `SkillActivationTool.permission_denial_reason()` returns `"Skill is not model-invocable"` when `not meta.model_invocable`. `execute()` removes the inline check. |
| `core/tool_execution.py` | `ToolExecutionPipeline.execute()` — skills don't go through this | Skills now go through the full pipeline because `SkillActivationTool` is in `ToolRegistry._tools`. No code change — it's automatic. |
| `hitl/pipeline.py` | `PermissionPipeline._layer1_validate()` — no skill-specific logic | `tool.permission_denial_reason(params)` already called on every tool. `SkillActivationTool` returns the `model_invocable` denial — no pipeline change. |

**Dependencies**: Phase 1 complete (`SkillActivationTool` registered in `ToolRegistry`).

**Risk & Mitigation**:

| Risk | Mitigation |
|------|-----------|
| Skills that were always auto-approved now go through interactive approval | `PermissionPipeline` honors `bypassPermissions` and `dontAsk` modes. Read-write-agent-state effects are not in WRITE_WORKSPACE → not denied by plan mode |
| Hooks that fire on tool calls now fire on skill activation | This is correct behavior — CC's `before_tool_use` fires on ALL tool calls, including skill activations. Hook authors may need to filter by tool name |

**Acceptance Criteria**:
- [ ] `SkillActivationTool.permission_denial_reason()` returns `model_invocable` denial
- [ ] Skill activation goes through full `PermissionPipeline.check()` — verified by hook `PRE_TOOL_USE` firing on skill activation
- [ ] `context="fork"` skills still spawn subagent correctly (gate remains in execute)
- [ ] Auto-approved skills (preload, CLI slash) still work without interactive prompt
- [ ] **Hook Author Migration Guide** delivered: documents how to filter skill activation events by `tool_name.startswith("skill:")` or `isinstance(tool, SkillActivationTool)`, ensuring existing hooks don't accidentally block or miss skill calls

---

### Phase 3: Unified Namespace and Collision Resolution

**Objective**: Single namespace with explicit priority: native > skill > MCP.
Collision detection at registration time with WARNING log and rejection of
lower-priority registrant.

**Scope — included**:
- `ToolRegistry.register()` checks namespace before accepting a new tool
- Collision detection keyed on canonical tool name
- Priority: native (source="system") > skill (source="project") > MCP (source="mcp")
- Rejected registrations log WARNING with detail
- Existing collisions (if any in current data) are NOT fixed — only new collisions are prevented

**Scope — NOT included**:
- Retroactive collision cleanup
- Alias collision resolution (separate concern)

**Changes**:

| File | From | To |
|------|------|-----|
| `core/base.py` | `ToolRegistry.register(tool)` — no collision check beyond `ValueError` on duplicate | Add `_priority_for(tool) → int` static method. Add `_check_collision(name, tool) → None | ValueError` — returns detail string if lower-priority tool conflicts with higher-priority. Called in `register()` before `_tools[name] = tool`. `SOURCE_PRIORITY = {"system": 3, "project": 2, "mcp": 1}` — higher number wins. |
| `tools/factory.py` | `BuiltTool` — no `source` field | Add `source: str = "system"` parameter. `mcp_props is not None → source="mcp"`. `SkillActivationTool → source="project"`. |
| `core/base.py` | `ToolRegistry._tools: dict[str, BaseTool]` — no priority concept | Add `_SOURCE_PRIORITY = {"system": 3, "project": 2, "mcp": 1}`. Add `_check_collision(name, tool) → str | None` — returns rejection detail when lower-priority tool conflicts with higher-priority registered tool. Called in `register()` before `_tools[name] = tool`. Higher-priority tool always wins. Same-priority collision → first-wins (second rejected with WARNING). MCP tools register with their ORIGINAL server name (no host-side prefix injection). Tool name collision is per-session, not global. |
| `skills/registry.py` | `SkillActivationTool` — no source field | Pass `source="project"` (or `source="managed"` for `trusted=False` MCP skills) |
| `capabilities/render.py` | Skills listed separately from MCP/Subagents in capability context | No change — rendering already separates by kind. Namespace resolution is a registry concern, not a rendering concern. |

**Dependencies**: Phase 1 complete (Skills in ToolRegistry), Phase 2 nice-to-have.

**Risk & Mitigation**:

| Risk | Mitigation |
|------|-----------|
| Existing MCP tool names collide with native tool names | Prefix `mcp__{server}__{tool}` already prevents this. No existing collision. |
| Skill named "Read" registers as `skill:read` — OK. But legacy code checking `tool_names` sees both "read" and "skill:read" | Legacy code uses `tool.name` which is now namespaced. `PolicyAwareToolRegistry._is_tool_visible()` filters by canonical name — the "read" tool (native) and "skill:read" tool (skill) are distinct entries |
| Phase 3 rejects lower-priority registration — what about dynamic MCP tools that reconnect? | `_replace_server_tools()` unregisters stale tools before registering new ones. No collision with self. Collision only occurs if another server or skill shares the same canonical name. |

**Acceptance Criteria**:
- [ ] `ToolRegistry.register()` rejects lower-priority registrant when collision detected
- [ ] WARNING log identifies both conflicting tools (name, source, priority)
- [ ] Native tool "Read" + MCP tool "search" → both registered (different names)
- [ ] Skill "review" + MCP tool "search" → both registered (different names)
- [ ] Native tool "Read" + Skill "read" → Skill rejected (system > project), WARNING logged
- [ ] Two MCP servers both registering "search" → first registers, second rejected with WARNING (same priority, first-wins)
- [ ] LLM sees flat tool names (no prefixes) — tool name is the name the tool declares

---

### Phase 4: Unified Observability — Single Audit Trail

**Objective**: All tool calls — whether native, MCP, or skill activation —
produce the same structured evidence: `TOOL_CALL_STARTED` →
`TOOL_CALL_COMPLETED/FAILED/BLOCKED`.  `SKILL_LOADED` evidence type is
deprecated.

**Scope — included**:
- `SkillActivationTool.execute()` goes through `ToolExecutionPipeline` → `ToolEvidenceRecorder` automatically records `TOOL_CALL_STARTED/COMPLETED`
- `MCP_CONNECTED` / `MCP_TOOLS_EXPOSED` preserved as service-level lifecycle evidence (not tool-call evidence)
- Deprecation notice on `EvidenceKind.SKILL_LOADED`

**Scope — NOT included**:
- Changing the evidence schema (no new fields)
- Session-level MCP connect/disconnect evidence (separate concern)

**Changes**:

| File | From | To |
|------|------|-----|
| `skills/activation.py` | `SkillActivationService.activate()` → `runtime.record_skill_activation()` | Deprecate `record_skill_activation()` call for the `execute()` path. Preload/CLI/http paths still call it for lifecycle tracking. |
| `core/tool_execution.py` | `ToolEvidenceRecorder` — no `tool_source` field | Inject `tool_source` into evidence metadata: `record_started/completed()` read `tool.metadata.source` and store as `evidence_metadata["tool_source"]`. Values: `"system"` (native), `"mcp"` (MCP transport), `"project"` (skill). Enables per-source audit aggregation. |
| `agent/session/run_evidence.py` | `EvidenceKind.SKILL_LOADED = "skill_loaded"` | Add deprecation note: "SKILL_LOADED is deprecated for skill tool calls (now recorded as TOOL_CALL_COMPLETED). Preserved for preload/http/cli lifecycle tracking." |

**Dependencies**: Phase 1 + 2 complete (SkillActivationTool goes through ToolExecutionPipeline).

**Risk & Mitigation**:

| Risk | Mitigation |
|------|-----------|
| Existing code that queries `SKILL_LOADED` evidence breaks | `SKILL_LOADED` is preserved for preload/CLI paths. The Completion Guard checks `required_skills` against evidence store — this still works because `TOOL_CALL_COMPLETED` entries carry `tool_name`. Update the guard to check both evidence types. |
| Fingerprint tracking for skills changes from `SKILL_LOADED` fingerprint to `TOOL_CALL_COMPLETED` parameters_digest | `parameters_digest` for skill activation is the skill name — deterministic and equivalent to the old fingerprint |

**Acceptance Criteria**:
- [ ] Skill tool call produces `TOOL_CALL_STARTED` + `TOOL_CALL_COMPLETED` evidence
- [ ] Completion Guard accepts both `TOOL_CALL_COMPLETED(tool_name=skill:name)` and `SKILL_LOADED(name)` as valid required-skill evidence
- [ ] Preload, CLI slash, and HTTP skill activation still produce `SKILL_LOADED` evidence
- [ ] `EvidenceKind.SKILL_LOADED` has deprecation docstring

---

## 4. Risk Register

| Risk | Phase | Severity | Mitigation | Status |
|------|-------|----------|------------|--------|
| SkillCapabilityProvider output changes format → model sees different skills listing | 1 | Medium | Capability section format preserved in `_render_skills()`. Only the descriptor source changes — from SkillRegistry to ToolRegistry | 📋 |
| Skills now go through PermissionPipeline → previously auto-approved skills get blocked | 2 | Medium | PermissionPipeline honors bypassPermissions/dontAsk. Skills with READ_AGENT_STATE/WRITE_AGENT_STATE effects are not in WRITE_WORKSPACE → not denied by plan mode | 📋 |
| Namespace collision breaks existing registration order at startup | 3 | Low | Priority check only applies to new registrations. Existing tools in current dataset use prefixes — no retroactive rejection | 📋 |
| Unified evidence changes break Completion Guard required_skills validation | 4 | Medium | Completion Guard updated to accept both TOOL_CALL_COMPLETED and SKILL_LOADED. Backward compat preserved | 📋 |
| `SkillTool.execute()` keeps `context="fork"` gate — dual personality: tool call vs spawn request | 1-2 | Low | Fork gate is execution routing, not permission logic. It stays in execute(). No collision with unified pipeline | 📋 |

## 5. Open Questions

1. **`skill:{name}` naming convention vs `Skill:{name}`**: RESOLVED: Rename the generic loader tool from "Skill" to "__skill_loader" (internal-only, not exposed in LLM schemas). Since Phase 1 makes every skill an independent tool, the generic loader is no longer needed by the LLM — it was a workaround for skills not being first-class tools. The loader remains available for backward-compat CLI/HTTP paths but is excluded from `get_schemas()` by a new `SkillActivationTool.schema_visible = False` flag.

2. **Skill Modifier Lifecycle (Blocker #1 resolution)**: RESOLVED: Option A — `ModifierScope` enum with TURN / RUN scoping.

**Implementation**: `SkillActivationTool.execute()` returns a `SkillContextModifier` with `scope=TURN`. `ToolExecutionPipeline._fire_post_tool_hook()` now includes a `deactivate_turn_scoped_modifier()` call — the modifier is deactivated at tool-call completion, not at end-of-run. Preload/CLI/HTTP paths produce modifiers with `scope=RUN`, still deactivated by `deactivate_skill_modifier()` at end of `run()`. `PolicyAwareToolRegistry` maintains two independent modifier stacks: `_turn_modifiers: list` and `_run_modifiers: list`.

This solves the state-leak bug: a turn-scoped skill activated at turn 3 will have its tool restrictions removed before turn 4, rather than persisting until the entire run ends.

3. **Phase 3 namespace scope**: Should namespace collision be global (across ALL sessions) or per-session? Current ToolRegistry is per-session (built fresh for each `run_session()` via `build_registry_for_session()`). Global uniqueness means skill named "review" would block any MCP tool named "review" across all sessions. Per-session means collision only matters within one agent's tool pool. Which is correct?

4. **Phase 4 Completion Guard migration**: RESOLVED: Option A (graceful migration). Completion Guard accepts both `TOOL_CALL_COMPLETED(tool_name=skill:name)` and `SKILL_LOADED(name)` as valid required-skill evidence for 60 days. After the deprecation window, `SKILL_LOADED` is removed from the guard check. Full migration (B) was rejected because preload/CLI/http paths are lifecycle events, not tool calls — retaining `SKILL_LOADED` for them is semantically correct.
