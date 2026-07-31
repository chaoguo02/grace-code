# Unified Execution Abstraction — Implementation Acceptance Checklist

## 1. Architecture Decision Lock-in

- [x] 1.1 Flat Namespace: Remove all `skill:`/`mcp__` prefixes; LLM sees only original tool name
- [x] 1.2 Dual-Stack Model: `ModifierScope(TURN, RUN)` enum, `_turn_modifiers` + `_run_modifiers`, merge rule
- [x] 1.3 Per-Session Namespace: Collision only in `build_registry_for_session()`, no global registry
- [x] 1.4 Evidence Migration: `SKILL_LOADED` `@deprecated(since=v2.0, sunset=v2.0+60d)`, dual evidence in Completion Guard
- [x] 1.5 Rollback Safety: `attach_tool_registry(None)` no-op + config flag for hot rollback

## 2. Phase 1 Entry Gates

- [x] 2.1 `SkillActivationTool` contract: name, description, execute(), metadata.effects, metadata.source — all frozen in design
- [x] 2.2 Legacy Loader: `__legacy_skill_loader` rename + `visible_to_llm=False`
- [x] 2.3 CapabilityProvider: switch to `ToolRegistry` + `isinstance(tool, SkillActivationTool)`
- [x] 2.4 Backward compat: `SkillRegistry.format_for_prompt()` delegates to new path
- [x] 2.5 Feature flag: config key for unified registry toggle

## 3. Safety & Risk Mitigation

- [x] 3.1 Permission coverage: `model_invocable` → `permission_denial_reason()` test matrix
- [x] 3.2 Hook Migration Guide: draft with `isinstance` filter + `tool_name` examples
- [x] 3.3 TURN cleanup exception safety: `deactivate_turn_scoped_modifier()` must pop even when tool fails
- [x] 3.4 Collision logging: WARNING format with conflicting name, source, priority, rejected detail
- [x] 3.5 Evidence `tool_source` injection: `ToolEvidenceRecorder` reads `tool.metadata.source`

## 4. Test & Verification Baseline

- [ ] 4.1 Phase 1 E2E: discovery → registration → LLM schema → activation → modifier → cleanup chain
- [ ] 4.2 Phase 2 Permission regression: auto-approved skills unchanged in new pipeline
- [ ] 4.3 Phase 3 Collision scenarios: Native>Skill, MCP>MCP first-wins, cross-session isolation
- [ ] 4.4 Phase 4 Evidence dual: Completion Guard accepts both evidence types
- [ ] 4.5 Performance baseline: p99 tool call latency benchmark (delta < 1ms)

## 5. Documentation & Communication

- [ ] 5.1 Architecture Explorer update: unified pipeline diagram, Dual-Stack Modifier sequence diagram
- [ ] 5.2 Breaking Change Notice: draft for third-party hook/plugin authors
- [ ] 5.3 Sunset Timeline: `SKILL_LOADED` deprecation recorded in release notes template

## 6. Pre-Implementation Non-Negotiables

- [ ] 6.1 Dual-Stack `_turn_modifiers` / `_run_modifiers` data structures are in Phase 1 (not deferred to Phase 3)
- [ ] 6.2 `__legacy_skill_loader` has zero production code-path dependencies (audit before Phase 1 ships)
- [x] 6.3 `TURN` modifier fault injection test: `SkillActivationTool.execute()` raises → `after_tool_use` still pops
- [ ] 6.4 Evidence `tool_source` literals (`"system"` / `"mcp"` / `"project"`) defined in single module (core/types.py)
