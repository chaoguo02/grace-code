# Plan: MCP timeout/retry, deferred proxy, and warning cleanup

## Context discovered

- `runtime/mcp/sync_bridge.py` already has a persistent background event loop and `call_tool(namespaced_name, args)`.
- Current manager state is `_bridges` and `_tool_map`, not `_clients` / `_tool_name_map`.
- `MCPToolBridge.call_tool()` already has SDK-level timeout via `asyncio.wait_for(config.timeout_seconds)`, but the sync side currently waits with `future.result()` without a synchronous timeout.
- `runtime.tool.ConcreteTool` constructor is internal and does not accept `name=...`, `description=...`, `input_schema=...`; deferred proxy should be built with `build_tool()` instead of subclassing `ConcreteTool`.
- `runtime.mcp.tool_adapter.mcp_tool_to_runtime_tool()` currently calls `bridge.call_tool(...)` directly, not the sync manager.
- `pyproject.toml` already pins `requests>=2.31.0`; the warning is likely environment-specific. A test-only filter in pytest config is the least invasive cleanup.

## Implementation approach

### 1. Add timeout/retry policy to `runtime/mcp/sync_bridge.py`

Add:

- `ExecutionPolicy` dataclass:
  - `timeout: float = 30.0`
  - `max_retries: int = 2`
  - `backoff_base: float = 0.5`
  - `backoff_factor: float = 2.0`
  - `backoff_max: float = 10.0`
  - `retryable_exceptions = (TimeoutError, ConnectionError, OSError)`
  - deterministic-friendly jitter using `random.uniform(...)` in production code; tests assert ranges, not exact values.
- `MCPToolTimeoutError`
- `MCPToolExhaustedError`

Update `SyncMCPToolManager`:

- Constructor accepts `default_policy: ExecutionPolicy | None = None`.
- Add `execute_tool(runtime_tool_name, arguments, *, policy=None, idempotent=True) -> MCPCallResult`.
- Add `_execute_once(runtime_tool_name, arguments, *, timeout, attempt=0)`:
  - validates runtime name and connected bridge
  - submits `bridge.call_tool(original_tool_name, arguments)` via `asyncio.run_coroutine_threadsafe`
  - waits with `future.result(timeout=timeout)`
  - on sync timeout: cancel future and raise `MCPToolTimeoutError`
- Keep existing `call_tool(...)` as the compatibility API, but implement it by calling `execute_tool(...)` and converting `MCPToolTimeoutError` / `MCPToolExhaustedError` to error `MCPCallResult` instead of raising.

Retry behavior:

- Retry only exceptions included in `policy.retryable_exceptions`.
- `idempotent=False` uses a single attempt and wraps retryable failure in `MCPToolExhaustedError`.
- Non-retryable exceptions still propagate from `execute_tool(...)`; `call_tool(...)` catches them into error results to preserve prior no-throw behavior.

### 2. Add deferred proxy support to `runtime/mcp/tool_adapter.py`

Add a factory function rather than subclassing `ConcreteTool`:

- `deferred_mcp_tool(...)` returning a runtime `ConcreteTool` built with `build_tool()`.
- Attach:
  - `is_mcp = True`
  - `always_load = False` for deferred tools
  - `should_defer = True`
  - `metadata` with server/tool/deferred state
  - `is_connected()` method or `is_connected` attribute-compatible callable where practical
  - `ensure_connected()` method
  - `execute(arguments)` sync helper for tests and sync callers
  - `to_api_schema()` helper for `_meta`, while existing runtime API still uses `to_api_definition()`.

Implementation details:

- Use a `threading.Lock` and a mutable state dict for double-checked first-call connection.
- `call_fn` awaits/uses `ensure_connected()` before delegating to `execute_fn`.
- If `execute_fn` returns `MCPCallResult`, convert it to `runtime.tool.ToolResult` using existing rendering/metadata conventions.
- Add `adapt_mcp_tools(tool_infos, *, manager, defer=False)` as a compatibility helper:
  - If `defer=False`, return existing connected tools where the manager has bridge access or create direct manager-backed tools.
  - If `defer=True`, return deferred tools whose sync `execute_fn` calls `manager.execute_tool(runtime_name, args)`.

Because current discovery requires connecting first, this deferred layer will provide first-call execution gating for proxy tools, not true no-network schema discovery. True no-connect discovery would require static config schemas or remote catalog support, which the current runtime MCP client does not have.

### 3. Clean pytest warning noise

Update `[tool.pytest.ini_options].filterwarnings` in `pyproject.toml` with a targeted filter for `requests.exceptions.RequestsDependencyWarning`.

I will not add global runtime warning suppression because hiding SSL/dependency warnings in production code is riskier than a test-only filter.

### 4. Tests

Add/extend tests:

- `tests/test_runtime_mcp_sync_bridge_timeout.py`
  - default policy values
  - backoff range
  - success first attempt
  - retry then success
  - exhausted retries
  - non-idempotent no retry
  - non-retryable exception raises from `execute_tool()`
  - `call_tool()` converts timeout/exhaustion into error `MCPCallResult`
- `tests/test_runtime_mcp_deferred_proxy.py`
  - starts disconnected/deferred
  - first call triggers connect once
  - second call skips connect
  - connect failure cached
  - thread safety connect-once
  - API schema `_meta` without requiring connection

Use fake functions/mocks for unit tests; keep existing subprocess MCP tests unchanged.

### 5. Verification

Run:

```powershell
pytest -q tests/test_runtime_mcp_sync_bridge_timeout.py tests/test_runtime_mcp_deferred_proxy.py
pytest -q tests/test_runtime_mcp.py tests/test_runtime_mcp_config.py tests/test_runtime_mcp_integration.py tests/test_runtime_mcp_registry.py tests/test_runtime_mcp_sync_bridge.py
pytest -q tests/test_agent_v2_mcp_integration.py
pytest -q
```

## Non-goals

- No true zero-connect MCP discovery, because current MCP SDK discovery requires a live session.
- No SSE/HTTP transport changes.
- No production-wide warning suppression.
- No breaking changes to existing `call_tool()` behavior.
