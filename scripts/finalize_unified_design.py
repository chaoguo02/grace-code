"""Apply user final decisions to UNIFIED_EXECUTION_ABSTRACTION_DESIGN.md."""
with open("docs/UNIFIED_EXECUTION_ABSTRACTION_DESIGN.md", "r", encoding="utf-8") as f:
    c = f.read()

count = 0

# Fix 1: Q#1 — User final decision: Flat Original Name + Deprecate Generic Loader
old1 = """1. **`skill:{name}` naming convention vs `Skill:{name}`**: RESOLVED: Rename the generic loader tool from "Skill" to "__skill_loader" (internal-only, not exposed in LLM schemas). Since Phase 1 makes every skill an independent tool, the generic loader is no longer needed by the LLM — it was a workaround for skills not being first-class tools. The loader remains available for backward-compat CLI/HTTP paths but is excluded from `get_schemas()` by a new `SkillActivationTool.schema_visible = False` flag."""
new1 = """1. **`skill:{name}` vs `Skill:{name}` vs Flat Name**: FINAL DECISION: Flat Original Name + Deprecate Generic Loader. All skills register by their frontmatter-declared original name (e.g., `review`, `web-search`). No prefixes — LLM must not perceive internal source markers. The existing generic "Skill" tool is immediately deprecated: renamed to `__legacy_skill_loader` and excluded from `get_schemas()` via `visible_to_llm=False`. If a skill's original name conflicts with a native tool, Phase 3 priority rules apply (Native > Skill → Skill rejected with WARNING)."""
c = c.replace(old1, new1)
count += 1

# Fix 2: Q#2 — Add explicit effective_modifiers merge rule
old2 = """**Implementation**: `SkillActivationTool.execute()` returns a `SkillContextModifier` with `scope=TURN`. `ToolExecutionPipeline._fire_post_tool_hook()` now includes a `deactivate_turn_scoped_modifier()` call — the modifier is deactivated at tool-call completion, not at end-of-run. Preload/CLI/HTTP paths produce modifiers with `scope=RUN`, still deactivated by `deactivate_skill_modifier()` at end of `run()`. `PolicyAwareToolRegistry` maintains two independent modifier stacks: `_turn_modifiers: list` and `_run_modifiers: list`.

This solves the state-leak bug: a turn-scoped skill activated at turn 3 will have its tool restrictions removed before turn 4, rather than persisting until the entire run ends."""
new2 = """**Implementation**:

- `ModifierScope(TURN, RUN)` enum added to `core/types.py`.
- `SkillContextModifier` gains `scope: ModifierScope = ModifierScope.TURN`.
- `SkillActivationTool.execute()` produces modifier with `scope=TURN` → pushed onto `_turn_modifiers: list`.
- `ToolExecutionPipeline.after_tool_use` hook calls `deactivate_turn_scoped_modifier()` → pops from `_turn_modifiers`.
- Preload/CLI/HTTP paths produce modifier with `scope=RUN` → pushed onto `_run_modifiers: list`.
- `deactivate_skill_modifier()` at end of `run()` clears only `_run_modifiers`.
- **Visibility merge rule**: `effective_modifiers = _run_modifiers + _turn_modifiers` — turn modifiers are applied AFTER run modifiers (turn scope overrides run scope on conflict).

This solves the state-leak bug: a turn-scoped skill activated at turn 3 will have its tool restrictions removed before turn 4, rather than persisting until the entire run ends."""
c = c.replace(old2, new2)
count += 1

# Fix 3: Q#3 — User confirms per-session. Add clearer instruction.
old3 = """3. **Phase 3 namespace scope**: RESOLVED: Per-session. ToolRegistry is built fresh for each `run_session()` via `build_registry_for_session()`. Collision resolution only matters within one agent's tool pool. This matches CC's behavior — tool visibility is per-session, not global."""
new3 = """3. **Phase 3 namespace scope**: FINAL DECISION: Per-Session Namespace with Deterministic Priority. Namespace collision detection only operates within a single `build_registry_for_session()` call. Session A registering `review` (skill) does not block Session B from registering `review` (MCP tool). Within the same session, priority is deterministic: `system(3) > project(2) > mcp(1)`. Same-priority conflicts use first-wins semantics. No global registry — no cross-session lock contention."""
c = c.replace(old3, new3)
count += 1

# Fix 4: Q#4 — Add @deprecated annotation + sunset timeline
old4 = """4. **Phase 4 Completion Guard migration**: RESOLVED: Option A (graceful migration). Completion Guard accepts both `TOOL_CALL_COMPLETED(tool_name=skill:name)` and `SKILL_LOADED(name)` as valid required-skill evidence for 60 days. After the deprecation window, `SKILL_LOADED` is removed from the guard check. Full migration (B) was rejected because preload/CLI/http paths are lifecycle events, not tool calls — retaining `SKILL_LOADED` for them is semantically correct."""
new4 = """4. **Phase 4 Completion Guard migration**: FINAL DECISION: Graceful Migration (Option A) with Sunset Timeline. Completion Guard accepts both `TOOL_CALL_COMPLETED(tool_name=X)` and `SKILL_LOADED(name=X)`. `EvidenceKind.SKILL_LOADED` marked `@deprecated(since="v2.0", sunset="v2.0+60d")`. Tool-activated skills emit ONLY `TOOL_CALL_COMPLETED` — no `SKILL_LOADED`. Lifecycle paths (preload/CLI/HTTP) continue producing `SKILL_LOADED` until sunset, then re-evaluated for removal. Full migration (B) was rejected because lifecycle paths are not tool calls — forcing `TOOL_CALL_COMPLETED` would pollute the tool execution audit trail."""
c = c.replace(old4, new4)
count += 1

with open("docs/UNIFIED_EXECUTION_ABSTRACTION_DESIGN.md", "w", encoding="utf-8") as f:
    f.write(c)
print(f"Applied {count} final decision updates")
