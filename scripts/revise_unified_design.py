"""Apply review revisions to UNIFIED_EXECUTION_ABSTRACTION_DESIGN.md."""
with open("docs/UNIFIED_EXECUTION_ABSTRACTION_DESIGN.md", "r", encoding="utf-8") as f:
    c = f.read()

count = 0

# ── Blocker #1: ModifierScope resolution ──
old1 = """1. **`skill:{name}` naming convention vs `Skill:{name}`**: CC uses `Skill(name=...)` as a runtime tool name (distinct from the tool "Skill" which is the generic loader). Our `Skill` tool is ALREADY the generic loader (registered as "Skill" in tool_names). Adding a `skill:{name}` namespaced tool for each skill creates two tools that both call `SkillTool.execute()`. Is this acceptable, or should we rename the generic "Skill" tool to something else?"""
new1 = """1. **`skill:{name}` naming convention vs `Skill:{name}`**: RESOLVED: Rename the generic loader tool from "Skill" to "__skill_loader" (internal-only, not exposed in LLM schemas). Since Phase 1 makes every skill an independent tool, the generic loader is no longer needed by the LLM — it was a workaround for skills not being first-class tools. The loader remains available for backward-compat CLI/HTTP paths but is excluded from `get_schemas()` by a new `SkillActivationTool.schema_visible = False` flag."""
c = c.replace(old1, new1)
count += 1

# ── Blocker #2: Flat namespace rewrite ──
old2 = """##### Phase 3 namespace scope**: Should namespace collision be global (across ALL sessions) or per-session? Current ToolRegistry is per-session (built fresh for each `run_session()` via `build_registry_for_session()`). Global uniqueness means skill named "review" would block any MCP tool named "review" across all sessions. Per-session means collision only matters within one agent"s tool pool. Which is correct?"""
new2 = """3. **Phase 3 namespace scope**: RESOLVED: Per-session. ToolRegistry is built fresh for each `run_session()` via `build_registry_for_session()`. Collision resolution only matters within one agent's tool pool. This matches CC's behavior — tool visibility is per-session, not global."""
c = c.replace(old2, new2)
count += 1

# ── Open Q#4 resolution (Option A) ──
old3 = """4. **Phase 4 Completion Guard migration**: The Completion Guard currently checks `required_skills` by looking at `SKILL_LOADED` evidence entries. After Phase 4, skill activations through the tool path produce `TOOL_CALL_COMPLETED` instead. Should we:
   - A) Update the guard to check both evidence types (graceful migration)
   - B) Migrate preload/CLI/http paths to also produce `TOOL_CALL_COMPLETED` evidence (full migration)
   The DDR says A (backward compat), but B is architecturally cleaner. Trade-off: B requires changing 3 additional code paths."""
new3 = """4. **Phase 4 Completion Guard migration**: RESOLVED: Option A (graceful migration). Completion Guard accepts both `TOOL_CALL_COMPLETED(tool_name=skill:name)` and `SKILL_LOADED(name)` as valid required-skill evidence for 60 days. After the deprecation window, `SKILL_LOADED` is removed from the guard check. Full migration (B) was rejected because preload/CLI/http paths are lifecycle events, not tool calls — retaining `SKILL_LOADED` for them is semantically correct."""
c = c.replace(old3, new3)
count += 1

# ── Open Q#2: ModifierScope resolution ──
old4 = """2. **Skill deactivation timing**: Currently `deactivate_skill_modifier()` runs at the end of every `run()`. If Phase 1 makes skills first-class tools, the modifier should deactivate when the tool call completes, not when the run ends. But some skills (preloaded ones) apply their modifier for the entire run duration, not just one turn. This creates a dual-lifetime problem: tool-call-activated skills have turn-scoped modifiers; preloaded skills have run-scoped modifiers. How to model this in a unified way?"""
new4 = """2. **Skill Modifier Lifecycle (Blocker #1 resolution)**: RESOLVED: Option A — `ModifierScope` enum with TURN / RUN scoping.

**Implementation**: `SkillActivationTool.execute()` returns a `SkillContextModifier` with `scope=TURN`. `ToolExecutionPipeline._fire_post_tool_hook()` now includes a `deactivate_turn_scoped_modifier()` call — the modifier is deactivated at tool-call completion, not at end-of-run. Preload/CLI/HTTP paths produce modifiers with `scope=RUN`, still deactivated by `deactivate_skill_modifier()` at end of `run()`. `PolicyAwareToolRegistry` maintains two independent modifier stacks: `_turn_modifiers: list` and `_run_modifiers: list`.

This solves the state-leak bug: a turn-scoped skill activated at turn 3 will have its tool restrictions removed before turn 4, rather than persisting until the entire run ends."""
c = c.replace(old4, new4)
count += 1

# ── Phase 3 rewrite: Flat namespace ──
old5 = """| `tools/factory.py` | `BuiltTool` — no `source` field | Add `source: str = "system"` parameter. `mcp_props is not None → source="mcp"`. |"""
new5 = """| `tools/factory.py` | `BuiltTool` — no `source` field | Add `source: str = "system"` parameter. `mcp_props is not None → source="mcp"`. `SkillActivationTool → source="project"`. |
| `core/base.py` | `ToolRegistry._tools: dict[str, BaseTool]` — no priority concept | Add `_SOURCE_PRIORITY = {"system": 3, "project": 2, "mcp": 1}`. Add `_check_collision(name, tool) → str | None` — returns rejection detail when lower-priority tool conflicts with higher-priority registered tool. Called in `register()` before `_tools[name] = tool`. Higher-priority tool always wins. Same-priority collision → first-wins (second rejected with WARNING). MCP tools register with their ORIGINAL server name (no host-side prefix injection). Tool name collision is per-session, not global. |"""
c = c.replace(old5, new5)
count += 1

# Update Phase 3 acceptance criteria
old6 = """**Acceptance Criteria**:
- [ ] `ToolRegistry.register()` rejects lower-priority registrant when collision detected
- [ ] WARNING log identifies both conflicting tools (name, source, priority)
- [ ] Native tool "Read" + MCP tool "mcp__docs__read" → both registered (different names)
- [ ] Skill "review" + MCP tool "mcp__github__review" → both registered (skill gets `skill:review`, MCP gets `mcp__github__review`)
- [ ] Two MCP servers both registering "search" → first registers `mcp__a__search`, second also registers `mcp__b__search` (different namespaces) — no collision"""
new6 = """**Acceptance Criteria**:
- [ ] `ToolRegistry.register()` rejects lower-priority registrant when collision detected
- [ ] WARNING log identifies both conflicting tools (name, source, priority)
- [ ] Native tool "Read" + MCP tool "search" → both registered (different names)
- [ ] Skill "review" + MCP tool "search" → both registered (different names)
- [ ] Native tool "Read" + Skill "read" → Skill rejected (system > project), WARNING logged
- [ ] Two MCP servers both registering "search" → first registers, second rejected with WARNING (same priority, first-wins)
- [ ] LLM sees flat tool names (no prefixes) — tool name is the name the tool declares"""
c = c.replace(old6, new6)
count += 1

# ── Phase 4 evidence tag ──
old7 = """| `core/tool_execution.py` | `ToolEvidenceRecorder` — no skill-specific handling | No change. Since `SkillActivationTool` goes through `ToolExecutionPipeline`, evidence is recorded automatically. |"""
new7 = """| `core/tool_execution.py` | `ToolEvidenceRecorder` — no `tool_source` field | Inject `tool_source` into evidence metadata: `record_started/completed()` read `tool.metadata.source` and store as `evidence_metadata["tool_source"]`. Values: `"system"` (native), `"mcp"` (MCP transport), `"project"` (skill). Enables per-source audit aggregation. |"""
c = c.replace(old7, new7)
count += 1

# ── Phase 1 feature flag ──
old8 = """**Dependencies**: None. `SkillActivationTool` is additive — it registers a new tool class without changing existing paths."""
new8 = """**Dependencies**: None. `SkillActivationTool` is additive — it registers a new tool class without changing existing paths.

**Rollback safety**: `SkillRegistry.attach_tool_registry(None)` is a valid no-op call. When no `ToolRegistry` is attached, skills continue to work via the old `SkillRegistry._metadata` path. Production rollout can use a config flag to control whether `attach_tool_registry()` is called, enabling hot rollback without code changes."""
c = c.replace(old8, new8)
count += 1

# ── Phase 2 Hook Migration Guide ──
old9 = """**Acceptance Criteria**:
- [ ] `SkillActivationTool.permission_denial_reason()` returns `model_invocable` denial
- [ ] Skill activation goes through full `PermissionPipeline.check()` — verified by hook `PRE_TOOL_USE` firing on skill activation
- [ ] `context="fork"` skills still spawn subagent correctly (gate remains in execute)
- [ ] Auto-approved skills (preload, CLI slash) still work without interactive prompt"""
new9 = """**Acceptance Criteria**:
- [ ] `SkillActivationTool.permission_denial_reason()` returns `model_invocable` denial
- [ ] Skill activation goes through full `PermissionPipeline.check()` — verified by hook `PRE_TOOL_USE` firing on skill activation
- [ ] `context="fork"` skills still spawn subagent correctly (gate remains in execute)
- [ ] Auto-approved skills (preload, CLI slash) still work without interactive prompt
- [ ] **Hook Author Migration Guide** delivered: documents how to filter skill activation events by `tool_name.startswith("skill:")` or `isinstance(tool, SkillActivationTool)`, ensuring existing hooks don't accidentally block or miss skill calls"""
c = c.replace(old9, new9)
count += 1

with open("docs/UNIFIED_EXECUTION_ABSTRACTION_DESIGN.md", "w", encoding="utf-8") as f:
    f.write(c)
print(f"Applied {count} revisions to UNIFIED_EXECUTION_ABSTRACTION_DESIGN.md")
