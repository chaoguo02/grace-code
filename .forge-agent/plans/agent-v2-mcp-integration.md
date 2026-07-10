# Plan: Integrate runtime MCP tools into agent/v2

## Context discovered

- `agent/v2` does not have `agent.py`; the real v2 entry point is `agent/v2/runtime.py` (`SessionRuntime`).
- v2 currently uses the legacy synchronous `tools.base.ToolRegistry` / `BaseTool` API, not `runtime.tool.ToolRegistry` / `ConcreteTool` directly.
- `runtime/mcp` now exposes `SyncMCPToolManager.load_and_discover(server_configs)`, which returns runtime `ConcreteTool` instances. It does **not** expose `connect_stdio`, `connect_remote`, `discover_all_tools`, `refresh_tools`, `shutdown`, or `adapt_mcp_tools`.
- The existing CLI still has a legacy MCP path through `tools/mcp_client.py`; this should not be expanded further for v2.
- `AgentSpec.allowed_tools` is static, so v2 policy filtering must explicitly allow MCP tool names after discovery, otherwise MCP tools registered in the base registry will be hidden.

## Implementation approach

### 1. Add `agent/v2/mcp_integration.py`

Create an integration layer matching the actual codebase rather than the sketch API.

Contents:

- `MCPRuntimeToolProxy(BaseTool)`
  - Wraps a runtime `ConcreteTool` as a legacy synchronous `BaseTool`.
  - Exposes:
    - `name` from runtime tool name
    - `description` from `tool.to_api_definition()["description"]` where available
    - `parameters_schema` from runtime `input_schema`
    - `risk_level = RiskLevel.MEDIUM` by default, because MCP tool semantics are fail-closed/unknown in the runtime adapter.
    - `execute(params)` calls the runtime tool via `asyncio.run(...)` and converts `runtime.tool.ToolResult` to `tools.base.ToolResult`.
  - Carries MCP marker attributes (`is_mcp`, `always_load`, `should_defer`, `metadata`) so existing helper/filter logic can still identify MCP tools.

- `MCPToolIntegration`
  - Constructor accepts either:
    - `server_configs: list[runtime.mcp.MCPServerConfig]`, or
    - raw legacy `mcp_servers` config dict from `config.schema.AppConfig`.
  - Uses existing runtime config parsing conventions:
    - Support current top-level `mcp_servers` shape used by `config/schema.py` and `entry/cli.py`.
    - Convert only stdio servers to `runtime.mcp.MCPServerConfig`, matching current `runtime/mcp/config.py` support.
  - `initialize()`:
    - Creates `SyncMCPToolManager`.
    - Calls `load_and_discover(server_configs)` once.
    - Wraps returned runtime tools in `MCPRuntimeToolProxy`.
    - Fail-open if no servers are configured.
  - `get_tool_pool(builtin_tools)`:
    - Applies optional allow/deny tool glob filtering to MCP tools.
    - Calls `runtime.mcp.assemble_tool_pool(builtin_tools, mcp_tools, deny_rules=...)`.
    - Built-ins win on duplicate names.
  - `register_into(registry)`:
    - Registers discovered MCP proxies into an existing `tools.base.ToolRegistry`, skipping duplicate names.
  - `tool_names` property for policy integration.
  - `shutdown()` calls `SyncMCPToolManager.close_all()`.
  - Context manager support.

I will **not** implement SSE/HTTP or hot refresh in this pass because the current runtime MCP client only supports stdio and the manager does not expose refresh/status APIs yet.

### 2. Update `agent/v2/runtime.py`

Add optional MCP integration to `SessionRuntime`:

- `mcp_integration=None` constructor parameter.
- Store as `self._mcp_integration`.
- In `_build_registry_for_session(...)`, extend allowed tool names for agents that should see MCP tools:
  - `build`: include all discovered MCP tools.
  - `general`: include all discovered MCP tools.
  - `plan` / `explore`: do not include MCP tools by default, because MCP tools are not reliably read-only.
- Preserve existing plan-mode behavior and `TaskToolV2` registration.

This avoids mutating global static `AgentSpec` definitions and keeps MCP visibility session/runtime scoped.

### 3. Update `entry/cli.py` v2 wiring

Modify the CLI registry setup path to use the new integration for v2 runtime:

- Keep legacy MCP behavior for non-v2 paths if needed.
- For v2:
  - Build base registry with built-in tools as today.
  - Initialize `MCPToolIntegration` from `cfg.mcp_servers`.
  - Register MCP proxy tools into the base registry.
  - Pass `mcp_integration` into `SessionRuntime`.
  - Ensure shutdown/cleanup calls `mcp_integration.shutdown()` after v2 run completes, preferably in a `finally` block.

If the existing shared `build_registry(...)` function eagerly registers legacy MCP tools, I will avoid double registration by either:

- moving MCP registration out of the shared builder and explicitly selecting legacy/new behavior at call sites, or
- adding a flag to skip legacy MCP registration for v2.

I will choose the smallest safe diff after reading the exact surrounding CLI code during implementation.

### 4. Tests

Add `tests/test_agent_v2_mcp_integration.py` covering:

- Empty config initializes without manager/tools.
- Raw `mcp_servers` dict converts to runtime `MCPServerConfig`.
- Deny glob filters MCP tools.
- Built-in duplicate wins when assembling pool.
- Runtime `ConcreteTool` proxy executes and converts success/error results.
- `SessionRuntime._build_registry_for_session` exposes MCP tools to `build`/`general` but not `plan`/`explore`.

Use mocks/fake runtime tools for most tests to avoid spawning MCP subprocesses. Existing runtime MCP integration tests already cover real stdio server behavior.

### 5. Verification

Run:

```powershell
pytest -q tests/test_agent_v2_mcp_integration.py
pytest -q tests/test_v2_runtime.py tests/test_v2_e2e_behavioral.py
pytest -q tests/test_runtime_mcp.py tests/test_runtime_mcp_config.py tests/test_runtime_mcp_integration.py tests/test_runtime_mcp_registry.py tests/test_runtime_mcp_sync_bridge.py
pytest -q
```

## Non-goals for this pass

- No SSE/HTTP MCP transport until `runtime.mcp.client` supports it.
- No hot refresh/status APIs until `SyncMCPToolManager` exposes stable primitives for those.
- No change to the runtime `ConcreteTool` class.
- No duplicate `ConcreteTool` implementation.
