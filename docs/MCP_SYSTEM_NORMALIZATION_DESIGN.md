# MCP System Normalization Design

## 1. Purpose

This document defines a phased normalization of the MCP subsystem based on
an 11-dimension audit.  The MCP subsystem is mature — 4 transports, hot
reload, effect inference, evidence recording, full permission pipeline
integration.  But several gaps exist in transport consistency, config
drift, and isolation boundaries.

## 2. Core Principles

### 2.1 Normalization, Not Refactoring

"Normalization" means: fix real defects, make implicit contracts explicit,
and eliminate config drift.  No behavior of existing MCP tool calls should
change — only the **declaration** and **isolation** of their properties
becomes consistent.  The MCP subsystem is mature; the gaps are in
transport consistency, trust boundary enforcement, and ownership clarity.

### 2.2 MCP is a Transport, Not a Framework

MCP servers are external processes or endpoints — they interact with the
host through a wire protocol, not through SDK extension.  All host→server
interaction goes through the protocol layer.  No memory-level coupling.
This principle governs: server isolation (#7 agent-scoped), name
collision (#11), and the fact that resource tools on HTTP transports
must fail cleanly (#1) — there is no backdoor SDK session.

### 2.3 Server Trust Boundary = Process Boundary

The trust boundary between host and MCP server is absolute.  Servers are
zero-trust: they must not inherit host credentials, must declare their
capabilities honestly, and are subject to the full permission pipeline.
Env sanitization (#12), capability gating at connect (#13), and rate
limiting (#3) are direct consequences of this principle.

### 2.4 Protocol Compliance > Feature Parity

If the MCP specification does not define a capability for a given
transport (e.g., `resources/list` on HTTP), the correct behavior is
to return a structured error — NOT to simulate the feature through
non-standard means.  This guarantees interoperability with any
MCP-compliant server.  Governs #1 and #5.

### 2.5 Lifecycle Ownership is Explicit

Every server's lifecycle — spawn, connect, reconnect, close — has a
traceable owner in the codebase.  No implicit lifecycle state.  The
`SyncMCPToolManager` owns bridge instances and the watchdog.  The
`MCPToolIntegration` owns the tool list and loading mode.  Agent-scoped
servers (#7) have explicit connect-on-spawn and disconnect-on-completion.
Daemon thread (#6) follows from this: the watchdog is not a critical
lifecycle participant.

### 2.6 Config is the Single Source of Truth for Server Identity

Server identity, capabilities, and permissions are derived from config
files — not from runtime state mutations.  Runtime state (connected/
disconnected, tool availability) is an overlay, not a replacement.
Config unification (#2) and collision resolution (#11) follow from this.

### 2.7 明确不做

| Anti-requirement | Reason |
|-----------------|--------|
| Auto-reconnect on tool call failure | Current on-demand retry + watchdog is correct — CC only reconnects at watchdog layer |
| MCP server SDK upgrade | Decoupled from normalization — transport layer is independent |
| Transport auto-detection | Explicit config is more predictable — CC uses explicit transport selection |
| HTTP resource simulation | Protocol compliance principle — return error, don't emulate |
| In-process counter → OTel migration in this batch | Deferred to Tool Normalization Phase 3 #7 convergence |

## 3. Gap Analysis Summary

| # | Gap | Severity | Phase | Type | CC Principle |
|---|-----|----------|-------|------|-------------|
| 1 | Resource ops fail on HTTP/SSE/WS | High | Phase 1 | Bug | Protocol Compliance (#2.4) |
| 2 | Dual _parse_server_config drift | High | Phase 1 | Bug | Config as Source of Truth (#2.6) |
| 3 | No reload rate limiting | Medium | Phase 1 | Defect | Trust Boundary (#2.3) |
| 4 | SSE stored responses dead code | Low | Phase 2 | Engineering | — |
| 5 | WsMCPBridge sequential-only | Medium | Phase 2 | Defect | Protocol Compliance (#2.4) |
| 6 | Non-daemon thread blocks exit | Medium | Phase 2 | Defect | Lifecycle Ownership (#2.5) |
| 7 | Agent-scoped MCP leaks to shared session | Medium | Phase 3 | Isolation | MCP is a Transport (#2.2), Lifecycle (#2.5) |
| 8 | ToolAvailability stale after connect | Low | Phase 3 | Engineering | Lifecycle Ownership (#2.5) |
| 9 | No MCP health metrics API | Low | Phase 3 | Observability | — converges to OTel per Phase 3 #9 convergence path |
| 10 | MCP _apply_loading_mode mutation pattern | Low | Phase 3 | Engineering | Reconsidered — merged into Phase 3 as documentation-only |
| 11 | MCP tool name collision resolution undefined | Medium | Phase 1 | Defect | Config as Source of Truth (#2.6) |
| 12 | MCP server spawn environment sanitization | High | Phase 1 | Security | Trust Boundary (#2.3) |
| 13 | No connect-time capability gating | Medium | Phase 2 | Defect | Trust Boundary (#2.3) |

## 4. Design Decisions

### 4.1 Phase 1: Defect and Bug Fixes

#### #1: Resource Tools for HTTP/SSE/WS Bridges

**CC Principle**: Protocol Compliance (#2.4) — if MCP spec doesn't define resources on HTTP, don't emulate them.

**Problem**: `HttpMCPBridge`, `SseMCPBridge`, `WsMCPBridge` inherit
`list_resources()`/`read_resource()` from `MCPToolBridge` which calls
`_require_session()` → `RuntimeError("MCPToolBridge is not connected")`.
Resource tools are silently broken on non-stdio transports.

**Decision**: Override `list_resources()` and `read_resource()` on
`HttpMCPBridge` (which `SseMCPBridge` and `WsMCPBridge` extend).
HTTP resource listing is NOT part of the MCP standard protocol —
resources are only available via `resources/list` on the SDK session.
Since HTTP bridges don't have an SDK session, the correct behavior
is to return a clear error message rather than raise RuntimeError.

**Implementation**:
```python
class HttpMCPBridge(MCPToolBridge):
    def list_resources(self):
        return []  # HTTP bridges don't support MCP resources
    def read_resource(self, uri):
        return {"contents": [], "error": "MCP resources not available on HTTP transport"}
```

#### #2: Unify Config Parsing

**CC Principle**: Config as Single Source of Truth (#2.6) — server identity and capabilities derive from config, not from runtime mutation.

**Problem**: `agent/mcp/config.py:153-225` and
`agent/session/mcp_integration.py:405-454` are independent
implementations with diverging field names (`type` vs `transport`,
`timeout` vs `timeout_seconds`).

**Decision**: Export `_parse_server_config` from `config.py` as the
single source of truth.  Remove the duplicate in `mcp_integration.py`.
All callers use the same function.

#### #3: Rate Limit Tool Reloads

**CC Principle**: Trust Boundary (#2.3) — servers are zero-trust; a misbehaving server must not degrade host performance.

**Problem**: Watchdog and push-based `list_changed` notifications
trigger `discover_tools()` + `_refresh_tool_map()` with no backpressure.
A misbehaving server can cause thundering-herd reloads.

**Decision**: Add a simple cooldown: no more than 1 tool map refresh
per server per 10 seconds.  Track last-refresh timestamp in
`SyncMCPToolManager._last_refresh: dict[str, float]`.

### 4.2 Phase 2: Transport Completeness

#### #4: Remove Dead SSE Response Storage

**CC Principle**: Protocol Compliance (#2.4).

**Problem**: `_sse_responses` dict on `SseMCPBridge` stores server-pushed
RPC responses but no caller retrieves them.  `_rpc_call` on the HTTP path
doesn't check it.

**Decision**: Remove `_sse_responses` dict and associated write logic.
SSE notification handling (which IS used for `list_changed`) remains.

#### #5: WsMCPBridge Concurrent Requests

**CC Principle**: Protocol Compliance (#2.4) — MCP request-response protocol has no response routing by ID.

**Problem**: `WsMCPBridge._rpc_call()` sends then immediately waits for
response — strictly sequential.  Two concurrent tool calls on one
WebSocket connection would interleave incorrectly.

**Decision**: Document the limitation.  `WsMCPBridge._rpc_call()` now
acquires an `asyncio.Lock` per bridge instance and raises a clear error
if concurrent calls arrive before the lock is released.  No response
routing by ID needed — the lock is simpler and correct for the
request-response protocol.

#### #6: Daemon Thread for SyncMCPToolManager

**CC Principle**: Lifecycle Ownership (#2.5) — watchdog is not a critical lifecycle participant.

**Problem**: `daemon=False` on the event-loop thread blocks process
exit if `close_all()` not called.

**Decision**: Change to `daemon=True`.  The thread's purpose is
watchdog health checks and reconnection — none of which are critical
to graceful process shutdown.  `close_all()` still called from
`__exit__` for explicit cleanup.

### 4.3 Phase 3: Engineering Cleanup

#### #7: Agent-Scoped MCP Isolation

**CC Principle**: MCP is a Transport (#2.2) + Lifecycle Ownership (#2.5).

**Problem**: Agent-scoped MCP tools are added to the shared session
`_tools` list and `_tool_map`.  Other agents running concurrently can
potentially see leaked tools.

**Decision**: Store agent-scoped tools in a per-session dictionary
keyed by agent_name in `MCPToolIntegration`.  `_mcp_tool_names_for_spec()`
resolves inline tools from this dict rather than the shared pool.
Cleanup removes the agent entry entirely.

#### #8: Stale ToolAvailability After Connect

**CC Principle**: Lifecycle Ownership (#2.5) — reconnect must restore availability state.

**Problem**: `_sync_mcp_capabilities()` marks failed-server tools as
UNAVAILABLE once at startup.  If a server reconnects later (via
watchdog or tool-call retry), the UNAVAILABLE mark is never cleared.

**Decision**: In `_replace_server_tools`, call
`_tool_availability_guard.mark_available(name)` for each newly-loaded
tool from a reconnected server.

#### #9: Health Metrics Counter

**CC alignment**: CC uses OpenTelemetry spans for MCP health metrics. Our in-process counters are a v1 approximation. Converges to OTel span attributes when TraceContext integration is wired into the MCP bridge layer.

**Problem**: No call latency, error rate, or reconnect frequency
counters are maintained at the MCP layer.

**Decision**: Add three counters to `SyncMCPToolManager`:
`_call_count`, `_call_error_count`, `_reconnect_count`.
Expose via property for the architecture inspector (existing Phase 6
Capability Index integration).

#### #10: Loading Mode Mutation (DOCUMENTATION ONLY)

**CC principle**: CC's equivalent `MCPToolMetadata` is a plain dataclass
with no setter methods. Direct attribute assignment on non-frozen
dataclasses is idiomatic Python. Adding setters would be half-measure
encapsulation — the correct CC pattern is `frozen=True` +
`dataclasses.replace()`, which is a separate project.

**Decision**: No code change. Document the current pattern as
intentional: `MCPToolProps` is a pure data carrier, not a domain
object. All consumers are read-only. The dataclass is intentionally
non-frozen to support the two mutation sites (`_apply_loading_mode`
and `BuiltTool` setters).

#### #11: MCP Tool Name Collision Resolution

**CC Principle**: Config as Single Source of Truth (#2.6) — namespacing
strategy is decided at config parsing, not at registry deduplication.

**Problem**: When two MCP servers provide tools with the same un-prefixed
name, `assemble_tool_pool()` raises `ValueError` on duplicate names
(`tools/pool.py:32-33`), which means the second server's tools fail to
register entirely — the first registration wins silently.

CC uses `{server_name}__{tool_name}` prefix universally, enforced at
config parsing. Our prefix already exists (`mcp__{server}__{tool}`)
but the collision behavior should be documented and tested.

**Decision**: Document the current prefix behavior as the collision
resolution strategy. `mcp__{server}__{tool}` guarantees uniqueness
as long as server names are unique (enforced by config parsing).
Add a test verifying that two servers with the same `tool_name` produce
distinct runtime names. No code change — the mechanism already exists.

#### #12: MCP Server Spawn Environment Sanitization

**CC Principle**: Trust Boundary (#2.3) — servers must not inherit host
credentials. CC explicitly strips `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, and similar sensitive env vars from the stdio subprocess
environment, keeping only a safe allowlist (PATH, HOME, LANG, etc.).

**Problem**: `MCPToolBridge` spawns the stdio subprocess via
`subprocess.Popen` with `env=with_utf8_env()` — which merges the current
process environment. If the host process has API keys in its environment,
the MCP server inherits them.

**Decision**: Add a sanitization step to `MCPToolBridge.connect()`:
after the base `with_utf8_env()`, strip known sensitive env vars.
Use CC's allowlist pattern: only pass PATH, HOME, LANG, LC_ALL,
TMPDIR, TEMP, TMP, USER, LOGNAME, SHELL, and explicitly-configured
env vars from the MCP config. All other env vars are stripped.

**Implementation**: Add `_sanitize_env(base_env, server_config)` static
method on `MCPToolBridge`. Called in `connect()` before spawning.

#### #13: Connect-Time Capability Gating

**CC Principle**: Trust Boundary (#2.3) — a server that claims capabilities
but fails to deliver them must be flagged, not silently accepted.

**Problem**: After `initialize()` handshake, the server declares
`capabilities: {tools: {}, resources: {}}`. If `tools/list` subsequently
returns empty or errors, the server is treated as "connected" with zero
tools — no degradation marker, no logged warning.

**Decision**: In `discover_tools()` (called right after initialize),
compare the returned tool set against the declared capabilities.
If the server declared `tools` capability but returned zero tools:

- If `_last_tool_counts` has a prior record > 0: log WARNING
  "Server {name} declares tools capability but returned 0 tools
  (N expected from prior connection). Server may be degraded."
- If this is the first connection (`_last_tool_counts` has no record):
  log WARNING "Server {name} declares tools capability but returned
  0 tools on first connection. Verify server configuration."

This covers both degraded reconnects and misconfigured first connects
without false positives.

**Implementation**: Store the tool count from each successful discovery
in `SyncMCPToolManager._last_tool_counts: dict[str, int]`. Compare on
subsequent discoveries.

## 5. Migration Phases

### Phase 1: Defect and Bug Fixes (5 items)

- #1: Resource tools for HTTP/SSE/WS
- #2: Unify config parsing
- #3: Rate limit tool reloads
- #11: Document tool name collision resolution
- #12: Environment sanitization for stdio MCP servers

### Phase 2: Transport Completeness (4 items)

- #4: Remove dead SSE response storage
- #5: WsMCPBridge concurrent request handling
- #6: Daemon thread for SyncMCPToolManager
- #13: Connect-time capability gating

### Phase 3: Engineering Cleanup (4 items)

- #7: Agent-scoped MCP isolation
- #8: Stale ToolAvailability
- #9: Health metrics
- #10: Loading mode cleanup (documentation only)

## 6. Impact Analysis Matrix

| Downstream System | Phases Affected | Risk | Mitigation |
|-------------------|----------------|------|------------|
| ToolRegistry | Phase 1, 3 | Low | Config unification + isolation are additive |
| Permission pipeline | None | None | MCP tools already go through full pipeline |
| Streaming executor | None | None | Resource governor slot unchanged |
| Capability index | Phase 1 | Low | McpCapabilityProvider unaffected by config unification |
| Skill activation | Phase 3 | Low | Agent-scoped isolation protects concurrent agents |
| Hot reload (watchdog) | Phase 1 | Low | Rate limiting is additive, not restrictive |

## 7. Acceptance Checklist

### Phase 1

- [ ] `list_resources()` overridden on HttpMCPBridge — returns empty list (not RuntimeError)
- [ ] `read_resource()` overridden on HttpMCPBridge — returns structured error
- [ ] Duplicate `_parse_server_config` removed from mcp_integration.py
- [ ] All callers use config.py version
- [ ] `_last_refresh` dict added — max 1 refresh per server per 10s
- [ ] Rapid `list_changed` notifications within 10s window → only first triggers reload
- [ ] #11: Two servers with same un-prefixed tool name produce distinct runtime names (tested)
- [ ] #12: `_sanitize_env()` implemented on MCPToolBridge — strips sensitive env vars
- [ ] #12: Allowlist includes PATH, HOME, LANG, TMP, SHELL + explicit config env vars only

### Phase 2

- [ ] `_sse_responses` dict removed from SseMCPBridge
- [ ] SSE notification handling unchanged (`list_changed` still works)
- [ ] WsMCPBridge._rpc_call acquires asyncio.Lock
- [ ] Sequential WsMCPBridge calls (call A -> await -> call B -> await) complete successfully after lock integration
- [ ] Concurrent WsMCPBridge calls raise clear error
- [ ] SyncMCPToolManager thread changed to daemon=True
- [ ] Process exit no longer blocked by unclosed MCP manager
- [ ] #13: `_last_tool_counts` dict tracks successful discovery tool counts
- [ ] #13: WARNING logged when server declares tools capability but returns 0 tools (and prior count > 0)

### Phase 3

- [ ] Agent-scoped tools stored in per-agent dict on MCPToolIntegration
- [ ] `_mcp_tool_names_for_spec()` resolves from per-agent dict
- [ ] Cleanup removes agent entry entirely
- [ ] `_replace_server_tools` calls mark_available for each loaded tool
- [ ] `_call_count`, `_call_error_count`, `_reconnect_count` counters added
- [ ] #10 DOCUMENTATION: `MCPToolProps` mutation pattern documented as intentional (no code change)

### Final Cross-Phase

- [ ] MCP tool calls produce identical results before and after
- [ ] All 4 transports work correctly (stdio, HTTP, SSE, WS)
- [ ] Resource tools raise clear errors on HTTP/SSE/WS (no RuntimeError)
- [ ] Hot reload rate limited (1 per server per 10s)
- [ ] Agent-scoped MCP tools isolated from other agents
- [ ] Process exits cleanly with daemon thread
