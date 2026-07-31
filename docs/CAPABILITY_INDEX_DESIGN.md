# Capability Index Design

## 1. Purpose

This document defines the design for a unified, prompt-facing capability view across tools, skills, MCP, and agents.

The goal is not to merge the existing registries. The goal is to introduce a read-only facade that normalizes capability metadata, overlays runtime state when needed, and produces structured prompt sections for context injection.

This design addresses a current gap: core tool schemas are injected into the model context, but Skill descriptions, MCP discovery information, and subagent capability descriptions are assembled in scattered runtime prompt code. That makes the prompt context harder to reason about and risks creating duplicated logic.

## 2. Core Principles

### 2.1 Keep the Four Registries Separate

The following registries/components remain independent sources of truth:

| Source | Responsibility | Runtime Semantics |
|---|---|---|
| `ToolRegistry` | Built-in and runtime tools, schemas, execution | Direct tool invocation, HITL, permission pipeline |
| `SkillRegistry` | Skill discovery, metadata, and on-demand instruction loading | Loaded through the `Skill` tool or slash invocation |
| `MCPToolIntegration` / MCP layer | MCP server lifecycle, discovered MCP tools, deferred activation | External/internal server connection, deferred tool activation, failure isolation |
| `AgentRegistryV2` | Agent and subagent definitions | Delegation policy, workspace mode, session spawning |

These should not be physically merged. Their lifecycles, execution semantics, and failure domains are different.

### 2.2 Introduce a Facade, Not a Replacement

The new abstraction is `CapabilityIndex`.

`CapabilityIndex` is a read-only query and projection layer. It does not execute tools, load skill bodies, connect MCP servers, or spawn agents. It only collects metadata from providers, overlays runtime state, filters descriptors through a query, and produces a deterministic snapshot.

### 2.3 Separate Metadata, Runtime State, and Prompt Context

Capability data is split into three layers:

1. **Metadata**
   - Static or semi-static facts from registries, files, config, and discovered MCP tool metadata.
   - Examples: skill description, MCP tool name, agent description, path scopes.

2. **Runtime State**
   - Dynamic facts owned by Runtime.
   - Examples: MCP failed server, deferred MCP tool, unavailable tool, current parent agent delegation scope.

3. **Prompt Context**
   - A structured, budget-aware rendering of the current capability snapshot.
   - Produced as `CapabilitySection` objects before final markdown concatenation.

### 2.4 Treat Internal and External MCP Uniformly in the Index

At the catalog/index layer, internal MCP and external MCP should be represented the same way: server name, tool name, description, status, and discovery instructions.

Transport details such as stdio, HTTP, SSE, WebSocket, headers, env, command, or process lifecycle remain Runtime concerns.

### 2.5 Redaction Happens Before Prompt Emission

Sensitive MCP configuration details must never reach prompt context.

The renderer/sanitizer may use runtime state such as error text, but it must sanitize and truncate before producing `CapabilitySection` content.

### 2.6 Avoid Two Logic Paths

Existing formatting logic must be migrated behind the new providers/renderers rather than copied.

Examples:

- `SkillRegistry.format_for_prompt()` should become a compatibility wrapper around `SkillCapabilityProvider` + `CapabilityPromptRenderer`.
- Architecture inspector views should eventually consume `CapabilitySnapshot` instead of rebuilding tools/skills/MCP/agents independently.
- Runtime prompt builder should eventually request a capability context instead of hand-assembling skills and subagents.

## 3. Naming Standard

| Concept | Use | Avoid | Rationale |
|---|---|---|---|
| Unified view layer | `CapabilityIndex` | `CapabilityCatalog`, `CapabilityRegistry` | Index emphasizes query/projection. Registry implies ownership/execution. Catalog implies static full set. |
| Runtime tool availability guard | `ToolAvailabilityGuard` | `CapabilityRegistry` | Existing runtime blocklist only guards tool availability. |
| Query parameters | `CapabilityQuery` | `CapabilityRequest` | Query is declarative filtering, not RPC request semantics. |
| Rendered unit | `CapabilitySection` | `RenderedPrompt`, `PromptFragment` | Section has title, content, priority, token estimate, and kind. |
| Build entrypoint | `build_capability_context()` | `get_capabilities()`, `assemble_context()` | Build indicates collection + rendering into prompt-facing context. |
| Provider method | `list_descriptors(query)` | `list_capabilities()`, `get_all()` | Descriptor is the normalized output; query supports filtering. |
| Snapshot fingerprint | `snapshot.fingerprint` | `hash`, `checksum`, `version` | Fingerprint expresses content identity. |

## 4. Proposed Package Layout

```text
capabilities/
├── __init__.py              # Public API: CapabilityIndex, CapabilityQuery, build_capability_context
├── models.py                # Pure data models
├── index.py                 # CapabilityIndex facade implementation
├── providers/
│   ├── __init__.py          # CapabilityProvider Protocol
│   ├── tool_provider.py     # ToolCapabilityProvider
│   ├── skill_provider.py    # SkillCapabilityProvider
│   ├── mcp_provider.py      # McpCapabilityProvider
│   └── agent_provider.py    # AgentCapabilityProvider
├── render.py                # CapabilityPromptRenderer; returns CapabilitySection objects
├── sanitize.py              # MCP redaction and error truncation helpers
└── _compat.py               # Temporary compatibility wrappers
```

## 5. Data Model

### 5.1 CapabilityKind

```python
class CapabilityKind(str, Enum):
    TOOL = "tool"
    SKILL = "skill"
    MCP_SERVER = "mcp_server"
    MCP_TOOL = "mcp_tool"
    AGENT = "agent"
```

### 5.2 CapabilityStatus

```python
class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    DEFERRED = "deferred"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    HIDDEN = "hidden"
```

Notes:

- `DEFERRED` means available through discovery/activation, not failed.
- `FAILED` is runtime state.
- `HIDDEN` means intentionally filtered from prompt-facing output.

### 5.3 CapabilityMetadata

```python
@dataclass(frozen=True)
class CapabilityMetadata:
    kind: CapabilityKind
    name: str
    description: str = ""
    source: str = ""
    namespace: str = ""
    when_to_use: str = ""
    invocation: str = ""
    server_name: str = ""
    path_scopes: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    file_path: str = ""
    trusted: bool = True
```

### 5.4 CapabilityRuntimeState

```python
@dataclass(frozen=True)
class CapabilityRuntimeState:
    status: CapabilityStatus = CapabilityStatus.AVAILABLE
    visible_to_model: bool = True
    activation: str = ""
    reason: str = ""
    error: str = ""
```

Example mappings:

| Capability | Status | Visible | Activation |
|---|---|---:|---|
| Direct built-in tool | `AVAILABLE` | true | call directly |
| Model-invocable skill | `AVAILABLE` | true | `Skill` |
| Deferred MCP tool | `DEFERRED` | false | `ToolSearch` |
| Failed MCP server | `FAILED` | false | unavailable |
| Delegatable subagent | `AVAILABLE` | true | `Agent` |

### 5.5 CapabilityDescriptor

```python
@dataclass(frozen=True)
class CapabilityDescriptor:
    metadata: CapabilityMetadata
    runtime: CapabilityRuntimeState

    def fingerprint_key(self) -> tuple:
        ...
```

The descriptor is the normalized unit returned by providers.

### 5.6 CapabilitySnapshot

```python
@dataclass(frozen=True)
class CapabilitySnapshot:
    descriptors: tuple[CapabilityDescriptor, ...]
    fingerprint: str

    def by_kind(self, kind: CapabilityKind) -> tuple[CapabilityDescriptor, ...]:
        ...
```

### 5.7 CapabilityQuery

```python
@dataclass(frozen=True)
class CapabilityQuery:
    kinds: frozenset[CapabilityKind] = frozenset(CapabilityKind)
    excluded_statuses: frozenset[CapabilityStatus] = frozenset({
        CapabilityStatus.HIDDEN,
    })
    visible_to_model: bool | None = True
    namespaces: frozenset[str] | None = None
    parent_agent: str | None = None

    def matches(self, descriptor: CapabilityDescriptor) -> bool:
        if descriptor.runtime.status in self.excluded_statuses:
            return False
        ...
```

This replaces coarse boolean switches such as `include_tools=True`.

The default status behavior is exclusion-based rather than whitelist-based: exclude `HIDDEN`, include every other status. This is intentionally future-proof. If future runtime states such as `DEGRADED` or `RATE_LIMITED` are added, they should become visible by default instead of being silently filtered out because a whitelist was not updated.

### 5.8 CapabilitySection

```python
@dataclass(frozen=True)
class CapabilitySection:
    title: str
    content: str
    priority: int
    token_estimate: int
    kind_filter: CapabilityKind
```

The renderer returns sections, not a final markdown blob. This lets `ContextManager` or the build entrypoint apply budget-aware trimming and ordering.

`token_estimate` is not a Provider responsibility. Providers return data only. The renderer builds section strings but should not depend on a tokenizer. Initial renderer-created sections may set `token_estimate=0` or another sentinel. The `build_capability_context()` entrypoint, or later `ContextManager`, is responsible for batch token estimation immediately before trimming and injection.

## 6. Fingerprint Design

Fingerprinting must be deterministic and resistant to runtime noise.

Each `CapabilityDescriptor` defines `fingerprint_key()` and includes only prompt-relevant semantic fields.

Example:

```python
def fingerprint_key(self) -> tuple:
    error_hash = (
        hashlib.sha256(self.runtime.error.encode()).hexdigest()[:8]
        if self.runtime.error else ""
    )
    return (
        self.metadata.kind.value,
        self.metadata.name,
        self.metadata.namespace,
        self.runtime.status.value,
        self.runtime.visible_to_model,
        error_hash,
    )
```

Rules:

- Sort descriptors by `fingerprint_key()` before hashing.
- Use compact deterministic JSON or tuple serialization.
- Use SHA256 for all fingerprint and error-hash computations. Avoid MD5 even for non-security fingerprints, because enterprise scanners commonly flag MD5 usage and create avoidable compliance noise.
- Do not include timestamps.
- Do not include full error text.
- Do not include source file path unless it affects prompt output.
- Prefer existing registry fingerprints where available, especially for skills.

Snapshot fingerprint:

```python
descriptors_sorted = sorted(descriptors, key=lambda d: d.fingerprint_key())
raw = json.dumps(
    [d.fingerprint_key() for d in descriptors_sorted],
    separators=(",", ":"),
    sort_keys=False,
)
fingerprint = hashlib.sha256(raw.encode()).hexdigest()[:16]
```

## 7. Provider Design

### 7.1 CapabilityProvider Protocol

```python
class CapabilityProvider(Protocol):
    def list_descriptors(self, query: CapabilityQuery) -> tuple[CapabilityDescriptor, ...]:
        ...
```

Providers adapt existing registries. They do not own execution or lifecycle.

### 7.2 ToolCapabilityProvider

Inputs:

- `ToolRegistry`

Reuse existing facts:

- `registry.tool_names`
- `registry.get_schemas()`
- `registry.metadata_for(name)`
- tool description from registered tool object

Existing code to consolidate later:

- `ArchitectureService._tools()` currently reconstructs similar metadata.

MCP tools should preferably be represented by `McpCapabilityProvider`, not duplicated as ordinary tools.

### 7.3 SkillCapabilityProvider

Inputs:

- `SkillRegistry`

Reuse:

- `registry.list_skill_entries()`
- `SkillMetadata` fields

Important fields:

- `description`
- `when_to_use`
- `model_invocable`
- `user_can_invoke`
- `paths`
- `allowed_tools`
- `disallowed_tools`
- `mcp_servers`
- `source`
- `trusted`

Migration rule:

- `SkillRegistry.format_for_prompt()` becomes a compatibility wrapper around this provider and `CapabilityPromptRenderer`.

### 7.4 McpCapabilityProvider

Inputs:

- `MCPToolIntegration`

Reuse:

- `integration.tools`
- `integration.deferred_tool_descriptors()`
- `integration.server_tools`
- `integration.failed_servers`
- `integration.tool_names`
- tool `mcp_props`

Server fallback description is mandatory:

```python
f"MCP server '{name}' providing {tool_count} tools"
```

No MCP server or tool descriptor may enter prompt-facing output with an empty description.

Status mapping:

| Runtime fact | Descriptor status |
|---|---|
| Server in failed_servers | `FAILED` |
| Tool has `should_defer=True` | `DEFERRED` |
| Tool is loaded and visible | `AVAILABLE` |
| Tool belongs to failed server | `FAILED` or `UNAVAILABLE` |

Do not expose:

- command
- args
- env
- headers
- cwd
- raw URL containing secrets
- raw config JSON

### 7.5 AgentCapabilityProvider

Inputs:

- `AgentRegistryV2`
- parent `AgentDefinition` or parent agent name

Reuse:

- `agent_registry.delegatable_by(parent)`
- `AgentDefinition` fields

Runtime-sensitive aspects:

- Whether a child is delegatable depends on parent policy.
- Worktree protocol text depends on whether any visible child uses worktree isolation.
- Read-only delegation boundaries depend on current delegation scope.

This provider may need runtime context, but it must not spawn or mutate sessions.

## 8. CapabilityIndex Design

```python
class CapabilityIndex:
    def __init__(self, providers: Sequence[CapabilityProvider]) -> None:
        self._providers = tuple(providers)

    def snapshot(self, query: CapabilityQuery) -> CapabilitySnapshot:
        descriptors: list[CapabilityDescriptor] = []
        for provider in self._providers:
            descriptors.extend(provider.list_descriptors(query))
        normalized = self._normalize(descriptors)
        filtered = tuple(d for d in normalized if query.matches(d))
        fingerprint = self._fingerprint(filtered)
        return CapabilitySnapshot(filtered, fingerprint)
```

Responsibilities:

- call providers
- normalize names and ordering
- dedupe descriptors
- apply query filtering
- compute fingerprint

Non-responsibilities:

- execute tools
- load skill content
- connect MCP servers
- activate deferred MCP tools
- spawn agents
- enforce permissions

## 9. Renderer Design

`CapabilityPromptRenderer` returns sections.

```python
class CapabilityPromptRenderer:
    def render(
        self,
        snapshot: CapabilitySnapshot,
        query: CapabilityQuery,
    ) -> list[CapabilitySection]:
        ...
```

Suggested sections:

1. `Skills`
2. `MCP Tool Discovery`
3. `MCP Failures`
4. `Subagents`
5. Optional `Tool Notes`

The renderer should not decide final context placement. It only creates structured sections with priority and markdown content. Token estimation is performed after rendering by `build_capability_context()` or `ContextManager`, so renderer logic remains pure, deterministic, and easy to unit test.

Final markdown can be produced by:

```python
def build_capability_context(...):
    snapshot = index.snapshot(query)
    sections = renderer.render(snapshot, query)
    sections = estimate_section_tokens(sections)  # batch token estimation
    selected = trim_sections(sections, budget)
    return "[CAPABILITY CONTEXT]\n\n" + "\n\n".join(
        f"## {s.title}\n{s.content}" for s in selected
    )
```

## 10. Prompt Injection Strategy

### 10.1 Stable Base Prompt Anchor

`prompts/base.md` should contain a stable anchor for future insertion:

```markdown
## Capabilities
Available capabilities may include tools, skills, MCP tools, and subagents.
Use the runtime capability context to decide whether to call a tool directly,
load a skill, search deferred MCP tools, or delegate to a subagent.

<!-- CAPABILITY_CONTEXT_ANCHOR -->
```

Do not inject dynamic skill names or MCP tools directly into `base.md`.

### 10.2 Short-Term Runtime Injection

In the initial phases, inject `[CAPABILITY CONTEXT]` through `runtime_prompt_builder` as a runtime user message.

This is a temporary compatibility path and must include a TODO marker:

```python
# TODO(capabilities): Migrate capability context injection to ContextManager.
```

### 10.3 Long-Term ContextManager Integration

`ContextManager.build_request_messages()` should eventually accept:

```python
capability_sections: list[CapabilitySection] | None = None
```

or:

```python
capability_context: str | None = None
```

Preferred long-term form is structured sections. `ContextManager` should handle budget trimming and final concatenation.

Recommended message order:

```text
system core
capability context
long-term context / memory / project rules
conversation history
task anchor
```

## 11. Security and Redaction

### 11.1 Redaction Scope

Redaction belongs in `capabilities/sanitize.py` and renderer-facing paths.

Never emit:

- MCP command
- command args
- env values
- headers
- cwd
- secret-bearing URLs
- raw server config JSON

Allowed in prompt:

- server name
- tool runtime name
- original tool name
- sanitized description
- status
- sanitized error summary
- counts

### 11.2 Error Sanitization

Error text should be:

- truncated
- token-like strings redacted
- multiline text normalized
- included in fingerprint only as short hash

Example:

```python
def sanitize_error(text: str, limit: int = 240) -> str:
    ...
```

## 12. Observability

After prompt injection is stable, capability fingerprint should enter trace/log metadata.

Suggested fields:

- `capability_fingerprint`
- `capability_descriptor_count`
- `capability_sections`
- `capability_token_estimate`
- `capability_trimmed_count`

This should happen before ArchitectureService migration, so runtime behavior and snapshot stability can be validated without coupling UI inspector changes to prompt injection changes.

## 13. Migration Plan

### Phase 0: Naming Cleanup

1. Rename `agent/capability_registry.py` to `agent/tool_availability_guard.py`.
2. Rename `CapabilityRegistry` to `ToolAvailabilityGuard`.
3. Update all imports and references.
4. Keep behavior identical.
5. Before completing Phase 0, run a global residual-name audit for `CapabilityRegistry`.

Rationale: the current name conflicts with the new capability index concept.

Residual-name audit requirements:

- Python string literals and dynamic imports. Use a global search for `CapabilityRegistry` in `*.py`; remaining matches must be intentional compatibility comments or removed.
- YAML/TOML/JSON configuration files.
- Log format strings and trace span names.
- Test fixtures and mock patch paths.
- Pickle/cache/serialization keys, if any. If old keys exist, clear or migrate the old cache explicitly.

### Phase 1: Models, Index, Skill Provider

1. Add `capabilities/` package.
2. Implement models, query, snapshot, index, renderer skeleton.
3. Implement `SkillCapabilityProvider`.
4. Update `SkillRegistry.format_for_prompt()` to call new compatibility layer.
5. Keep external API behavior stable.

Tests:

- Existing skill prompt visibility tests.
- Snapshot golden test for skill sections.
- Path scope and MCP dependency rendering.
- `disable_model_invocation` behavior.
- `user_invocable=false` behavior.

### Phase 2: MCP Provider and MCP Prompt Sections

1. Implement `McpCapabilityProvider`.
2. Add MCP server fallback descriptions.
3. Add redaction utilities.
4. Render loaded/deferred/failed MCP sections.
5. Keep `ToolSearch` activation logic unchanged.

Tests:

- Deferred MCP section mentions `ToolSearch`.
- Failed server section is sanitized.
- No env/header/command/raw config leaks.
- Empty server descriptions get synthetic fallback.

### Phase 3: Agent Provider and Subagent Prompt Migration

1. Implement `AgentCapabilityProvider`.
2. Move subagent listing and delegation guidance from runtime prompt builder into renderer.
3. Keep runtime prompt builder as caller only.

Tests:

- Delegation disabled means no subagent section.
- Worktree child causes worktree result protocol section.
- Read-only delegation scope emits read-only boundary.

### Phase 4: Runtime Prompt Builder Integration

1. Add `build_capability_context()` entrypoint.
2. In `runtime_prompt_builder`, replace hand-built Skill/MCP/Subagent prompt code with capability context output.
3. Add TODO for Phase 5 ContextManager migration.
4. Preserve preloaded skills and agent memory injection separately, because they are actual content injection, not capability catalog.

Tests:

- Runtime prompt contains one `[CAPABILITY CONTEXT]` block.
- No duplicate `## Available Skills` blocks.
- Skill and MCP sections reflect current runtime state.

### Phase 5: ContextManager Structured Integration

1. Extend `ContextManager.build_request_messages()` to accept capability sections or context.
2. Move final budget trimming of sections into ContextManager.
3. Keep runtime prompt builder path temporarily behind compatibility flag or TODO until verified.

Tests:

- Capability sections are injected before long-term memory.
- Budget trimming preserves higher-priority sections.
- Prompt cache stable prefix is not polluted by dynamic MCP state.

### Phase 5.5: Observability Integration

1. Add capability fingerprint and section stats to tracing/log metadata.
2. Validate fingerprint stability and prompt size impact.
3. Use production/dev traces to identify overly verbose sections.

### Phase 6: ArchitectureService Migration

1. Update ArchitectureService to consume `CapabilitySnapshot` where possible.
2. Remove duplicate skill/tool/MCP/agent metadata expansion logic.
3. Keep API shape stable for the frontend.

Tests:

- Architecture snapshot output remains compatible.
- Capability fingerprint exposed in inspector metadata.
- UI receives same or richer facts with no duplicated logic.

### Phase 7: PromptAssembler Cleanup

1. Reduce `PromptAssembler` responsibility to template rendering.
2. Move tool description formatting into capability/provider/render layer.
3. Keep `base.md` stable except for generic capability anchor.

Tests:

- Base prompt rendering remains deterministic.
- Tool schema descriptions still appear where expected.
- No regression in existing context assembly tests.

## 14. Compatibility Rules

- Do not remove `SkillRegistry.format_for_prompt()` immediately; convert it to a compatibility wrapper.
- Do not change `ToolSearch` behavior in the same phase as MCP provider introduction.
- Do not change ArchitectureService and prompt injection in the same phase.
- Do not merge ToolRegistry, SkillRegistry, MCPToolIntegration, or AgentRegistryV2.
- Do not let `CapabilityIndex` mutate runtime state.
- Do not let `PromptAssembler` depend on runtime objects.

## 15. Acceptance Criteria

The design is complete when:

1. Tool, Skill, MCP, and Agent registries remain independent.
2. Capability metadata can be queried through `CapabilityIndex`.
3. Capability prompt output is produced as structured `CapabilitySection` objects.
4. Skill prompt formatting has one implementation path.
5. MCP deferred discovery is represented once and reused by prompt context and later inspector views.
6. Runtime state remains in Runtime-owned components.
7. Sensitive MCP config never appears in prompt output.
8. Capability snapshot fingerprint is deterministic and observable.
9. Runtime prompt builder no longer hand-assembles Skill/MCP/Subagent descriptions.
10. ArchitectureService eventually consumes the same capability snapshot rather than duplicating metadata extraction.

## 16. Initial Implementation Scope

The first implementation batch should be limited to:

1. Rename runtime `CapabilityRegistry` to `ToolAvailabilityGuard`.
2. Add `capabilities/` models and `CapabilityIndex` skeleton.
3. Add `SkillCapabilityProvider`.
4. Convert `SkillRegistry.format_for_prompt()` to use the new compatibility path.
5. Add golden tests for skill capability sections.

MCP, Agent, ContextManager, ArchitectureService, and PromptAssembler migrations should be separate batches.

## 17. Batch Acceptance Checklist

This checklist is the source of truth for incremental acceptance. Each batch must be tested, reviewed, and checked off before the next batch starts.

Legend:

- `[ ]` Not started
- `[~]` In progress
- `[x]` Accepted

### Phase 0: Naming Cleanup

Implementation checklist:

- [x] Rename `agent/capability_registry.py` to `agent/tool_availability_guard.py`.
- [x] Rename runtime class `CapabilityRegistry` to `ToolAvailabilityGuard`.
- [x] Rename runtime state enum `CapabilityState` to `ToolAvailabilityState`.
- [x] Update runtime-owned fields from `_capability_registry` to `_tool_availability_guard`.
- [x] Update `ToolRegistry` constructor/property/internal references to `tool_availability_guard`.
- [x] Update `ToolExecutionPipeline` parameter and availability check naming.
- [x] Update tests importing or constructing the old runtime guard.
- [x] Update non-design documentation references where the old name described runtime tool availability.
- [x] Remove the old `agent/capability_registry.py` file after imports are migrated.

Required validation:

- [x] Run targeted MCP/tool execution regression tests.
- [x] Run lifecycle and permission boundary regression tests.
- [x] Run residual-name audit for `CapabilityRegistry`, `CapabilityState`, `capability_registry`, and `agent.capability_registry` in Python/config files.
- [x] Confirm remaining Markdown mentions of `CapabilityRegistry` are only in this design document as historical migration context.

Accepted evidence:

- [x] `python -m pytest tests/test_weather_mock_mcp.py tests/test_tool_execution_pipeline.py` passed.
- [x] `python -m pytest tests/test_mcp_lifecycle.py tests/test_permission_session_boundary.py` passed.
- [x] Python/config residual-name search returned no old runtime names.

### Phase 1: Models, Index, Skill Provider

Implementation checklist:

- [x] Add `capabilities/` package with public API exports in `capabilities/__init__.py`.
- [x] Add pure data models in `capabilities/models.py`:
  - [x] `CapabilityKind`
  - [x] `CapabilityStatus`
  - [x] `CapabilityMetadata`
  - [x] `CapabilityRuntimeState`
  - [x] `CapabilityDescriptor`
  - [x] `CapabilitySnapshot`
  - [x] `CapabilityQuery`
  - [x] `CapabilitySection`
- [x] Implement exclusion-based `CapabilityQuery` defaults: exclude `HIDDEN`, include all other statuses.
- [x] Implement stable `CapabilityDescriptor.fingerprint_key()` using SHA256 for error hash.
- [x] Implement deterministic `CapabilitySnapshot` fingerprinting.
- [x] Add `CapabilityProvider` protocol in `capabilities/providers/__init__.py`.
- [x] Add `SkillCapabilityProvider` using `SkillRegistry.list_skill_entries()`.
- [x] Add renderer skeleton in `capabilities/render.py` that returns `CapabilitySection` objects.
- [x] Keep renderer tokenizer-free; section `token_estimate` may start at `0`.
- [x] Add compatibility layer in `capabilities/_compat.py`.
- [x] Convert `SkillRegistry.format_for_prompt()` to call the new compatibility path.
- [x] Preserve external output compatibility for existing `format_for_prompt()` callers.

Required validation:

- [x] Run existing skill prompt visibility tests.
- [x] Add and run Skill capability snapshot golden tests.
- [x] Test path-scope rendering.
- [x] Test MCP dependency metadata rendering for skills.
- [x] Test `disable_model_invocation` filtering.
- [x] Test `user_invocable=false` remains model-visible when model-invocable.
- [x] Run residual duplicate-logic audit: `SkillRegistry.format_for_prompt()` must not maintain independent markdown assembly logic.

Accepted evidence:

- [x] Existing skill prompt tests pass: `python -m pytest tests/test_capability_index_skill.py tests/test_skill_prompt_visibility.py`.
- [x] New golden snapshot tests pass: `tests/test_capability_index_skill.py`.
- [x] No behavior regression for runtime skill listing.

### Phase 2: MCP Provider and MCP Prompt Sections

Implementation checklist:

- [x] Add `capabilities/providers/mcp_provider.py`.
- [x] Represent MCP servers and MCP tools as descriptors.
- [x] Reuse `MCPToolIntegration.tools`, `server_tools`, `failed_servers`, `tool_names`, and `deferred_tool_descriptors()` facts.
- [x] Generate mandatory fallback server descriptions: `MCP server '<name>' providing <tool_count> tools`.
- [x] Map deferred MCP tools to `CapabilityStatus.DEFERRED`.
- [x] Map failed MCP servers to `CapabilityStatus.FAILED`.
- [x] Add `capabilities/sanitize.py` for MCP error/config redaction.
- [x] Render `MCP Tool Discovery` section.
- [x] Render `MCP Failures` section.
- [x] Keep `ToolSearch` activation behavior unchanged.

Required validation:

- [x] Deferred MCP section mentions `ToolSearch`.
- [x] Failed MCP server output is sanitized and truncated.
- [x] Prompt output does not include MCP command, args, env, headers, cwd, raw URLs with secrets, or raw config JSON.
- [x] Empty MCP server descriptions receive synthetic fallback descriptions.
- [x] MCP provider snapshot fingerprint is deterministic across repeated builds.
- [x] Existing MCP lifecycle tests pass.
- [x] Existing weather MCP tests pass.

Accepted evidence:

- [x] MCP provider tests pass: `python -m pytest tests/test_capability_index_mcp.py tests/test_mcp_lifecycle.py tests/test_weather_mock_mcp.py`.
- [x] No secret-bearing MCP config appears in rendered sections.
- [x] `ToolSearch` tests continue to pass unchanged.

### Phase 3: Agent Provider and Subagent Prompt Migration

Implementation checklist:

- [x] Add `capabilities/providers/agent_provider.py`.
- [x] Reuse `AgentRegistryV2.delegatable_by(parent)`.
- [x] Represent delegatable subagents as `CapabilityKind.AGENT` descriptors.
- [x] Include relevant agent metadata: name, description, workspace mode, model/effort inheritance, skills, MCP servers, and tool constraints.
- [x] Move subagent listing markdown from `runtime_prompt_builder` into `CapabilityPromptRenderer`.
- [x] Preserve worktree result protocol guidance when any visible child uses worktree isolation.
- [x] Preserve read-only delegation boundary guidance when applicable.
- [x] Keep runtime prompt builder as caller only.

Required validation:

- [x] Delegation disabled produces no subagent section.
- [x] Public delegatable subagents appear for eligible parent agents.
- [x] Worktree child causes worktree result protocol text to appear.
- [x] Read-only delegation scope emits read-only boundary.
- [x] Existing multi-agent/subagent tests pass.

Accepted evidence:

- [x] Agent provider tests pass: `python -m pytest tests/test_capability_index_agent.py tests/test_skill_prompt_visibility.py tests/test_scenario_agent_definitions.py tests/test_subagent_contract.py`.
- [x] Runtime prompt builder no longer owns independent subagent markdown assembly.

### Phase 4: Runtime Prompt Builder Integration

Implementation checklist:

- [x] Add `build_capability_context()` entrypoint.
- [x] Build snapshot through `CapabilityIndex` from active providers.
- [x] Render sections through `CapabilityPromptRenderer`.
- [x] Batch-estimate section tokens before trimming.
- [x] Trim sections by priority and budget.
- [x] Inject a single `[CAPABILITY CONTEXT]` runtime user message from `runtime_prompt_builder`.
- [x] Add `# TODO(capabilities): Migrate capability context injection to ContextManager.` at the temporary injection point.
- [x] Preserve preloaded skills content injection separately.
- [x] Preserve agent memory injection separately.
- [x] Remove hand-built Skill/MCP/Subagent capability blocks from runtime prompt builder.

Required validation:

- [x] Runtime prompt contains exactly one `[CAPABILITY CONTEXT]` block.
- [x] Runtime prompt contains no duplicate `## Available Skills` blocks.
- [x] Skill and MCP sections reflect current runtime state.
- [x] Preloaded skill bodies still inject only when explicitly configured.
- [x] Agent memory still injects independently from capability context.
- [x] CLI and Web/VS Code runtime paths receive the same capability context behavior.

Accepted evidence:

- [x] Runtime prompt builder tests pass: `python -m pytest tests/test_capability_context_runtime.py tests/test_capability_index_skill.py tests/test_capability_index_mcp.py tests/test_capability_index_agent.py tests/test_skill_prompt_visibility.py`.
- [x] Existing chat/session runtime tests pass: `python -m pytest tests/test_mcp_lifecycle.py tests/test_weather_mock_mcp.py tests/test_scenario_agent_definitions.py tests/test_subagent_contract.py`.

### Phase 5: ContextManager Structured Integration

Implementation checklist:

- [x] Extend `ContextManager.build_request_messages()` to accept structured capability sections or capability context.
- [x] Move final capability section trimming into `ContextManager`.
- [x] Inject capability context before long-term memory/project rules.
- [x] Ensure dynamic capability context does not pollute stable prompt-cache prefix.
- [x] Keep runtime prompt builder compatibility path until structured injection is verified.

Required validation:

- [x] Capability sections are injected before long-term memory.
- [x] Budget trimming preserves higher-priority sections.
- [x] Lower-priority sections are dropped deterministically under budget pressure.
- [x] Prompt cache stable prefix remains stable when MCP runtime state changes.
- [x] Existing context assembly and compaction tests pass.

Accepted evidence:

- [x] ContextManager capability-section tests pass: `python -m pytest tests/test_context_manager_capabilities.py tests/test_capability_context_runtime.py tests/test_capability_index_skill.py tests/test_capability_index_mcp.py tests/test_capability_index_agent.py`.
- [x] No regression in context stats or compaction behavior: `python -m pytest tests/test_context_planner.py tests/test_compaction_trigger.py tests/test_skill_prompt_visibility.py tests/test_mcp_lifecycle.py tests/test_weather_mock_mcp.py`.

### Phase 5.5: Observability Integration

Implementation checklist:

- [x] Add capability fingerprint to trace/log metadata.
- [x] Add descriptor count to trace/log metadata.
- [x] Add rendered section titles and count to trace/log metadata.
- [x] Add token estimate and trimmed section count to trace/log metadata.
- [x] Verify repeated identical snapshots produce stable fingerprints.

Required validation:

- [x] Observability tests cover capability metadata fields.
- [x] Fingerprint is stable across repeated runs with unchanged providers.
- [x] Fingerprint changes when prompt-relevant capability status changes.
- [x] Error-message noise does not create fingerprint churn beyond the sanitized hash design.

Accepted evidence:

- [x] Trace/log metadata includes capability fields.
- [x] No measurable prompt assembly performance regression beyond accepted budget.
- [x] All 19 observability tests pass: `python -m pytest tests/test_capability_observability.py -v`.
- [x] All 47 capability tests pass: `python -m pytest tests/test_capability_observability.py tests/test_context_manager_capabilities.py tests/test_capability_context_runtime.py tests/test_capability_index_skill.py tests/test_capability_index_mcp.py tests/test_capability_index_agent.py tests/test_skill_prompt_visibility.py`.
- [x] No regression in context planner or compaction: `python -m pytest tests/test_context_planner.py tests/test_compaction_trigger.py`.

### Phase 6: ArchitectureService Migration

Implementation checklist:

- [x] Update `ArchitectureService` to consume `CapabilitySnapshot` where possible.
- [x] Remove duplicate skill metadata expansion logic from ArchitectureService.
- [x] Remove duplicate MCP metadata expansion logic from ArchitectureService.
- [x] Remove duplicate agent capability expansion logic where provider output is sufficient.
- [x] Preserve existing API response shape for frontend compatibility.
- [x] Expose capability fingerprint in architecture/inspector metadata.

Required validation:

- [x] Architecture snapshot output remains backward compatible.
- [x] Frontend inspector still renders tools, skills, MCP, and agents.
- [x] ArchitectureService no longer rebuilds duplicate prompt-facing capability descriptions.
- [x] Existing architecture service tests pass.

Accepted evidence:

- [x] ArchitectureService tests pass: `python -m pytest tests/test_architecture_service.py -v` (8 tests).
- [x] Full capability suite passes: `python -m pytest tests/test_architecture_service.py tests/test_capability_index_skill.py tests/test_capability_index_mcp.py tests/test_capability_index_agent.py tests/test_capability_observability.py tests/test_context_manager_capabilities.py tests/test_capability_context_runtime.py -v` (50 tests).
- [x] ProjectOverviewService consumer tests pass: `python -m pytest tests/test_project_overview_service.py -v` (2 tests).
- [x] UI receives same shape with new `fingerprint` key (structurally open TypeScript types — extra key silently ignored).

### Phase 7: PromptAssembler Cleanup

Implementation checklist:

- [x] Reduce `PromptAssembler` to template loading and variable substitution responsibilities.
- [x] Move tool description formatting into the capability provider/render layer.
- [x] Keep `base.md` stable except for generic capability guidance and anchor.
- [x] Ensure `PromptAssembler` does not depend on ToolRegistry, SkillRegistry, MCP integration, or AgentRegistry runtime objects.

Required validation:

- [x] Base prompt rendering remains deterministic.
- [x] Tool schema descriptions still appear where expected.
- [x] Existing prompt builder tests pass.
- [x] Existing context assembly tests pass.
- [x] No runtime object imports are introduced in `prompts/assembler.py`.

Accepted evidence:

- [x] PromptAssembler cleanup tests pass: `python -m pytest tests/test_prompt_renderer.py -v` (2 tests).
- [x] No regression in model-visible tool schema prompt content.
- [x] Full suite passes: `python -m pytest tests/test_prompt_renderer.py tests/test_architecture_service.py tests/test_capability_observability.py tests/test_context_manager_capabilities.py tests/test_capability_context_runtime.py tests/test_capability_index_skill.py tests/test_capability_index_mcp.py tests/test_capability_index_agent.py tests/test_skill_prompt_visibility.py -v` (57 tests).
- [x] Context planner, compaction, and project overview tests pass: `python -m pytest tests/test_context_planner.py tests/test_compaction_trigger.py tests/test_project_overview_service.py -v` (13 tests).

### Final Cross-Phase Acceptance

- [x] Tool, Skill, MCP, and Agent registries remain independent.
- [x] Capability metadata can be queried through `CapabilityIndex`.
- [x] Capability prompt output is produced as structured `CapabilitySection` objects.
- [x] Skill prompt formatting has one implementation path.
- [x] MCP deferred discovery is represented once and reused by prompt context and inspector views.
- [x] Runtime state remains in Runtime-owned components.
- [x] Sensitive MCP config never appears in prompt output.
- [x] Capability snapshot fingerprint is deterministic and observable.
- [x] Runtime prompt builder no longer hand-assembles Skill/MCP/Subagent descriptions.
- [x] ArchitectureService consumes the same capability snapshot rather than duplicating metadata extraction.
