# Plan: Add MCP tool pool registry and sync bridge

## Scope

Add the missing runtime MCP integration helpers that mirror Claude Code's `tools.ts` layer:

- tool pool assembly
- deferred tool detection
- API schema generation
- sync/async MCP manager for synchronous callers

Keep compatibility with the already implemented runtime package:

- Do **not** introduce a second `ConcreteTool` class in `runtime/mcp/tool_adapter.py`.
- Continue using `runtime.tool.ConcreteTool` returned by `build_tool()`.
- Keep MCP tools fail-closed by default for concurrency/read-only/destructive semantics.

## Design adjustment from the proposed sketch

The proposed `SyncMCPToolManager` uses a fresh event loop per operation. That is risky with MCP sessions because the SDK session/streams are bound to the loop they were created on. The existing legacy `tools/mcp_client.py` already solves this with a persistent background event loop.

I will use the same safer pattern:

- one dedicated background event loop per `SyncMCPToolManager`
- `asyncio.run_coroutine_threadsafe(...)` for connect/call/close
- all MCP session operations stay on the same loop

## Files to add

### `runtime/mcp/registry.py`

Functions:

- `assemble_tool_pool(built_in_tools, mcp_tools, deny_rules=None)`
  - filter denied MCP tools
  - sort built-ins and MCP tools separately by name
  - built-ins first
  - deduplicate with built-ins winning
- `is_deferred_tool(tool)`
  - `always_load` metadata/property disables deferral
  - MCP tools defer by default
  - `should_defer` metadata/property defers built-ins
- `tools_to_api_schemas(tools)`
  - use `tool.to_api_definition()` if available
  - append `defer_loading: true` for deferred tools
- `find_tool(tools, name)`
- `filter_mcp_tools(tools)`
- `filter_built_in_tools(tools)`
- `_is_denied(tool_name, deny_rules)`

Because `runtime.tool.ConcreteTool` does not currently expose `is_mcp`, `always_load`, or `should_defer`, these helpers will read from attributes if present, then fallback to `tool.metadata` if present. For adapted MCP tools, I will attach these attributes dynamically in the adapter.

### `runtime/mcp/sync_bridge.py`

Class:

- `SyncMCPToolManager`

Methods:

- `load_and_discover(server_configs) -> list[ConcreteTool]`
  - fail-open per server: log and continue if one server fails
  - create an `MCPToolBridge` per config
  - connect and adapt discovered tools with `mcp_tool_to_runtime_tool(...)`
- `call_tool(namespaced_name, args) -> MCPCallResult`
  - parse `mcp__server__tool`
  - call the already connected bridge on the manager's background loop
  - return error `MCPCallResult` if server is not connected
- `close_all()`
  - close all bridges on the background loop
  - stop and join background thread
- context manager support
- `_parse_namespaced_name(...)`

Important detail: because runtime names use slugified server/tool names but `MCPToolBridge.call_tool()` needs the original MCP tool name, the manager will maintain a mapping:

- runtime tool name -> `(server_name, original_tool_name)`

## Files to update

### `runtime/mcp/tool_adapter.py`

- Keep `mcp_tool_to_runtime_tool(...)` as the main adapter.
- Attach metadata/attributes to returned runtime `ConcreteTool`:
  - `is_mcp = True`
  - `always_load = ...`
  - `should_defer = False`
  - `metadata = {mcp_server, mcp_tool_name, ...}`
- Add optional `always_load` parameter.
- Do **not** flip concurrency safe to true; keep fail-closed false.

### `runtime/mcp/types.py`

- Add optional `metadata: dict[str, Any]` to `MCPToolInfo`.
- Keep existing constructor-compatible defaults so current tests do not break.

### `runtime/mcp/client.py`

- Preserve existing behavior.
- Populate `MCPToolInfo.metadata` from MCP tool `_meta` if available.

### `runtime/mcp/__init__.py` and `runtime/__init__.py`

Export:

- `assemble_tool_pool`
- `is_deferred_tool`
- `tools_to_api_schemas`
- `find_tool`
- `filter_mcp_tools`
- `filter_built_in_tools`
- `SyncMCPToolManager`

## Tests to add

### `tests/test_runtime_mcp_registry.py`

Cover:

- tool pool merge ordering
- built-in wins on duplicate name
- deny glob filtering
- MCP defers by default
- `always_load` disables deferral
- built-in `should_defer` defers
- API schema includes `defer_loading` only for deferred tools
- find/filter helpers

### `tests/test_runtime_mcp_sync_bridge.py`

Use the existing `tests/fixtures/fake_mcp_server.py` and skip if `mcp` SDK is missing.

Cover:

- `load_and_discover()` returns adapted runtime tools
- runtime names include `mcp__fake_test__echo` etc.
- adapted tools carry `is_mcp` and input schema
- `call_tool()` synchronous call succeeds
- add typed return succeeds
- nonexistent server returns error result
- context manager clears bridges
- name parser valid/invalid cases

## Verification

Run:

```powershell
pytest -q tests/test_runtime_mcp_registry.py tests/test_runtime_mcp_sync_bridge.py
pytest -q tests/test_runtime_mcp.py tests/test_runtime_mcp_config.py tests/test_runtime_mcp_integration.py tests/test_runtime_mcp_registry.py tests/test_runtime_mcp_sync_bridge.py
pytest -q
```
