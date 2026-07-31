# MCP System Normalization Design

## 1. Purpose

This document defines a phased normalization of the MCP subsystem based on
an 11-dimension audit.  The MCP subsystem is mature — 4 transports, hot
reload, effect inference, evidence recording, full permission pipeline
integration.  But several gaps exist in transport consistency, config
drift, and isolation boundaries.

## 2. Core Principles

### 2.1 Normalization, Not Refactoring
### 2.2 DDR-First Development
### 2.3 Vertical Integration
### 2.4 Fail-Closed Defaults
### 2.5 明确不做

| Anti-requirement | Reason |
|-----------------|--------|
| Auto-reconnect on tool call failure | Current on-demand retry + watchdog is correct |
| MCP server SDK upgrade | Decoupled from normalization |
| Transport auto-detection | Explicit config is more predictable |

## 3. Gap Analysis Summary

| # | Gap | Severity | Phase | Type |
|---|-----|----------|-------|------|
| 1 | Resource ops fail on HTTP/SSE/WS | High | Phase 1 | Bug |
| 2 | Dual _parse_server_config drift | High | Phase 1 | Bug |
| 3 | No reload rate limiting | Medium | Phase 1 | Defect |
| 4 | SSE stored responses dead code | Low | Phase 2 | Engineering |
| 5 | WsMCPBridge sequential-only | Medium | Phase 2 | Defect |
| 6 | Non-daemon thread blocks exit | Medium | Phase 2 | Defect |
| 7 | Agent-scoped MCP leaks to shared session | Medium | Phase 3 | Isolation |
| 8 | ToolAvailability stale after connect | Low | Phase 3 | Engineering |
| 9 | No MCP health metrics API | Low | Phase 3 | Observability |
| 10 | _apply_loading_mode mutates via setattr | Low | Phase 3 | Engineering |

## 4. Design Decisions

### 4.1 Phase 1: Defect and Bug Fixes

#### #1: Resource Tools for HTTP/SSE/WS Bridges

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

**Problem**: `agent/mcp/config.py:153-225` and
`agent/session/mcp_integration.py:405-454` are independent
implementations with diverging field names (`type` vs `transport`,
`timeout` vs `timeout_seconds`).

**Decision**: Export `_parse_server_config` from `config.py` as the
single source of truth.  Remove the duplicate in `mcp_integration.py`.
All callers use the same function.

#### #3: Rate Limit Tool Reloads

**Problem**: Watchdog and push-based `list_changed` notifications
trigger `discover_tools()` + `_refresh_tool_map()` with no backpressure.
A misbehaving server can cause thundering-herd reloads.

**Decision**: Add a simple cooldown: no more than 1 tool map refresh
per server per 10 seconds.  Track last-refresh timestamp in
`SyncMCPToolManager._last_refresh: dict[str, float]`.

### 4.2 Phase 2: Transport Completeness

#### #4: Remove Dead SSE Response Storage

**Problem**: `_sse_responses` dict on `SseMCPBridge` stores server-pushed
RPC responses but no caller retrieves them.  `_rpc_call` on the HTTP path
doesn't check it.

**Decision**: Remove `_sse_responses` dict and associated write logic.
SSE notification handling (which IS used for `list_changed`) remains.

#### #5: WsMCPBridge Concurrent Requests

**Problem**: `WsMCPBridge._rpc_call()` sends then immediately waits for
response — strictly sequential.  Two concurrent tool calls on one
WebSocket connection would interleave incorrectly.

**Decision**: Document the limitation.  `WsMCPBridge._rpc_call()` now
acquires an `asyncio.Lock` per bridge instance and raises a clear error
if concurrent calls arrive before the lock is released.  No response
routing by ID needed — the lock is simpler and correct for the
request-response protocol.

#### #6: Daemon Thread for SyncMCPToolManager

**Problem**: `daemon=False` on the event-loop thread blocks process
exit if `close_all()` not called.

**Decision**: Change to `daemon=True`.  The thread's purpose is
watchdog health checks and reconnection — none of which are critical
to graceful process shutdown.  `close_all()` still called from
`__exit__` for explicit cleanup.

### 4.3 Phase 3: Engineering Cleanup

#### #7: Agent-Scoped MCP Isolation

**Problem**: Agent-scoped MCP tools are added to the shared session
`_tools` list and `_tool_map`.  Other agents running concurrently can
potentially see leaked tools.

**Decision**: Store agent-scoped tools in a per-session dictionary
keyed by agent_name in `MCPToolIntegration`.  `_mcp_tool_names_for_spec()`
resolves inline tools from this dict rather than the shared pool.
Cleanup removes the agent entry entirely.

#### #8: Stale ToolAvailability After Connect

**Problem**: `_sync_mcp_capabilities()` marks failed-server tools as
UNAVAILABLE once at startup.  If a server reconnects later (via
watchdog or tool-call retry), the UNAVAILABLE mark is never cleared.

**Decision**: In `_replace_server_tools`, call
`_tool_availability_guard.mark_available(name)` for each newly-loaded
tool from a reconnected server.

#### #9: Health Metrics Counter

**Problem**: No call latency, error rate, or reconnect frequency
counters are maintained at the MCP layer.

**Decision**: Add three counters to `SyncMCPToolManager`:
`_call_count`, `_call_error_count`, `_reconnect_count`.
Expose via property for the architecture inspector (existing Phase 6
Capability Index integration).

#### #10: Loading Mode Mutation Cleanup

**Problem**: `_apply_loading_mode()` sets `props.always_load` and
`props.is_deferred` directly on `MCPToolProps` dataclass.  The dataclass
is not frozen, so this works but bypasses encapsulation.

**Decision**: Add `set_always_load(value: bool)` and
`set_is_deferred(value: bool)` methods to `MCPToolProps`.
`_apply_loading_mode()` and `BuiltTool` setters call these methods.

## 5. Migration Phases

### Phase 1: Defect and Bug Fixes (3 items)

- #1: Resource tools for HTTP/SSE/WS
- #2: Unify config parsing
- #3: Rate limit tool reloads

### Phase 2: Transport Completeness (3 items)

- #4: Remove dead SSE response storage
- #5: WsMCPBridge concurrent request handling
- #6: Daemon thread for SyncMCPToolManager

### Phase 3: Engineering Cleanup (4 items)

- #7: Agent-scoped MCP isolation
- #8: Stale ToolAvailability
- #9: Health metrics
- #10: Loading mode cleanup

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

### Phase 2

- [ ] `_sse_responses` dict removed from SseMCPBridge
- [ ] SSE notification handling unchanged (`list_changed` still works)
- [ ] WsMCPBridge._rpc_call acquires asyncio.Lock
- [ ] Concurrent WsMCPBridge calls raise clear error
- [ ] SyncMCPToolManager thread changed to daemon=True
- [ ] Process exit no longer blocked by unclosed MCP manager

### Phase 3

- [ ] Agent-scoped tools stored in per-agent dict on MCPToolIntegration
- [ ] `_mcp_tool_names_for_spec()` resolves from per-agent dict
- [ ] Cleanup removes agent entry entirely
- [ ] `_replace_server_tools` calls mark_available for each loaded tool
- [ ] `_call_count`, `_call_error_count`, `_reconnect_count` counters added
- [ ] `set_always_load()` / `set_is_deferred()` methods on MCPToolProps
- [ ] `_apply_loading_mode()` and BuiltTool setters use new methods

### Final Cross-Phase

- [ ] MCP tool calls produce identical results before and after
- [ ] All 4 transports work correctly (stdio, HTTP, SSE, WS)
- [ ] Resource tools raise clear errors on HTTP/SSE/WS (no RuntimeError)
- [ ] Hot reload rate limited (1 per server per 10s)
- [ ] Agent-scoped MCP tools isolated from other agents
- [ ] Process exits cleanly with daemon thread
