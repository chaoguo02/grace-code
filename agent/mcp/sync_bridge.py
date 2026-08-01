"""Synchronous wrapper around async MCP tool bridges."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import (
    CancelledError as FutureCancelledError,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass
from typing import Any

from agent.mcp.client import MCPCallResult, MCPToolBridge, create_mcp_bridge
from agent.mcp.tool_adapter import mcp_tool_to_runtime_tool
from agent.mcp.types import MCPServerConfig

logger = logging.getLogger(__name__)


# ── Per-transport defaults (Claude Code alignment) ──
# These are used when the server config does not specify explicit values.
DEFAULT_EXECUTION_TIMEOUT = 30.0       # total timeout per attempt
DEFAULT_IDLE_TIMEOUT_STDIO = 1800.0    # 30 min idle for local processes
DEFAULT_IDLE_TIMEOUT_HTTP = 300.0      #  5 min idle for remote servers


@dataclass(frozen=True)
class ExecutionPolicy:
    """Synchronous MCP tool execution policy.

    timeout:     total wall-clock timeout per attempt (including retries).
    idle_timeout: max idle time waiting for a single future result.
                  None means no idle check (backward compatible).
                  For stdio servers this should be very long (30 min);
                  for HTTP servers this should be shorter (5 min).
    """

    timeout: float = DEFAULT_EXECUTION_TIMEOUT
    idle_timeout: float | None = None
    max_retries: int = 2
    backoff_base: float = 0.5
    backoff_factor: float = 2.0
    backoff_max: float = 10.0
    retryable_exceptions: tuple[type[BaseException], ...] = (
        TimeoutError,
        ConnectionError,
        OSError,
    )

    def get_backoff(self, attempt: int) -> float:
        delay = min(self.backoff_base * (self.backoff_factor ** attempt), self.backoff_max)
        jitter = delay * 0.1 * random.uniform(-1.0, 1.0)
        return max(0.0, delay + jitter)


class MCPToolTimeoutError(TimeoutError):
    """Raised when a synchronous MCP tool call times out."""

    def __init__(self, tool_name: str, timeout: float, attempt: int) -> None:
        self.tool_name = tool_name
        self.timeout = timeout
        self.attempt = attempt
        super().__init__(
            f"MCP tool '{tool_name}' timed out after {timeout:.1f}s (attempt {attempt + 1})"
        )


class MCPToolExhaustedError(RuntimeError):
    """Raised when retryable MCP tool failures exhaust all attempts."""

    def __init__(self, tool_name: str, attempts: int, last_error: BaseException) -> None:
        self.tool_name = tool_name
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"MCP tool '{tool_name}' failed after {attempts} attempt(s): {last_error}")


class SyncMCPToolManager:
    """Manage MCP bridges on a persistent background event loop.

    MCP-04: Automatic reconnection with exponential backoff.
    When a bridge disconnects (ConnectionError during tool call), the manager
    attempts up to MAX_RECONNECT_ATTEMPTS reconnections with exponential delay.
    """

    MAX_RECONNECT_ATTEMPTS = 5
    RECONNECT_BASE_DELAY = 1.0  # seconds

    def __init__(
        self,
        *,
        default_policy: ExecutionPolicy | None = None,
        health_interval_seconds: float = 30.0,
        restart_limit: int = 5,
        restart_window_seconds: float = 60.0,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="runtime-mcp-tools",
            daemon=False,
        )
        self._thread.start()
        self._bridges: dict[str, MCPToolBridge] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}
        self._server_tools: dict[str, list[str]] = {}
        self._failed_servers: dict[str, str] = {}
        self._default_policy = default_policy or ExecutionPolicy()
        self._closed = False
        self._health_interval = max(0.1, health_interval_seconds)
        self._restart_limit = max(1, restart_limit)
        self._restart_window = max(1.0, restart_window_seconds)
        self._restart_history: dict[str, deque[float]] = defaultdict(deque)
        self._inflight = 0
        self._inflight_condition = threading.Condition()
        self._watchdog_future = asyncio.run_coroutine_threadsafe(
            self._watchdog(),
            self._loop,
        )
        self._tools_changed_callback: Any = None

    @property
    def bridges(self) -> dict[str, MCPToolBridge]:
        return dict(self._bridges)

    @property
    def server_tools(self) -> dict[str, list[str]]:
        return {name: list(tools) for name, tools in self._server_tools.items()}

    @property
    def failed_servers(self) -> dict[str, str]:
        return dict(self._failed_servers)

    def set_tools_changed_callback(self, callback: Any) -> None:
        self._tools_changed_callback = callback

    def __enter__(self) -> "SyncMCPToolManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close_all()

    def load_and_discover(self, server_configs: list[MCPServerConfig]) -> list[Any]:
        """Connect configured servers and return adapted runtime tools."""
        self._ensure_open()
        runtime_tools: list[Any] = []
        for config in server_configs:
            self._configs[config.name] = config
            self._failed_servers.pop(config.name, None)
            bridge = create_mcp_bridge(config)
            bridge.set_tools_changed_callback(self._on_tools_changed)
            try:
                tools = self._run_coro(
                    bridge.connect(),
                    timeout=config.timeout_seconds,
                )
            except Exception as exc:  # pragma: no cover - exact SDK failures vary
                logger.warning("Failed to connect MCP server %s: %s", config.name, exc)
                self._failed_servers[config.name] = str(exc)
                bridge.mark_disconnected()
                self._bridges[config.name] = bridge
                try:
                    self._run_coro(bridge.close(), timeout=5.0)
                except Exception:
                    logger.debug("Failed to close MCP server %s after connect failure", config.name, exc_info=True)
                continue

            self._bridges[config.name] = bridge
            self._server_tools[config.name] = []
            for tool_info in tools:
                runtime_tool = mcp_tool_to_runtime_tool(self, tool_info)
                runtime_tools.append(runtime_tool)
                self._tool_map[runtime_tool.name] = (config.name, tool_info.name)
                self._server_tools[config.name].append(runtime_tool.name)
            # MCP Resources: also register resource list/read tools
            from agent.mcp.tool_adapter import create_resource_list_tool, create_resource_read_tool
            resource_list_tool = create_resource_list_tool(self, config.name)
            runtime_tools.append(resource_list_tool)
            self._tool_map[resource_list_tool.name] = (config.name, resource_list_tool.name)
            self._server_tools[config.name].append(resource_list_tool.name)
            resource_read_tool = create_resource_read_tool(self, config.name)
            runtime_tools.append(resource_read_tool)
            self._tool_map[resource_read_tool.name] = (config.name, resource_read_tool.name)
            self._server_tools[config.name].append(resource_read_tool.name)
        return runtime_tools

    def execute_tool(
        self,
        runtime_tool_name: str,
        arguments: dict[str, Any],
        *,
        policy: ExecutionPolicy | None = None,
        idempotent: bool = True,
    ) -> MCPCallResult:
        """Call a connected MCP tool with sync-side timeout and retry policy."""
        self._ensure_open()
        active_policy = policy or self._default_policy
        max_attempts = 1 if not idempotent else 1 + active_policy.max_retries
        last_error: BaseException | None = None

        with self._inflight_condition:
            self._inflight += 1
        try:
            for attempt in range(max_attempts):
                if attempt > 0:
                    backoff = active_policy.get_backoff(attempt - 1)
                    logger.info(
                        "Retrying MCP tool '%s' (attempt %d/%d) after %.2fs backoff",
                        runtime_tool_name,
                        attempt + 1,
                        max_attempts,
                        backoff,
                    )
                    time.sleep(backoff)

                try:
                    return self._execute_once(
                        runtime_tool_name, arguments,
                        timeout=active_policy.timeout,
                        idle_timeout=active_policy.idle_timeout,
                        attempt=attempt,
                    )
                except active_policy.retryable_exceptions as exc:
                    last_error = exc
                    logger.warning(
                        "MCP tool '%s' attempt %d/%d failed: %s",
                        runtime_tool_name,
                        attempt + 1,
                        max_attempts,
                        exc,
                    )
                    if not idempotent:
                        raise MCPToolExhaustedError(runtime_tool_name, 1, exc) from exc
                except Exception:
                    logger.exception("MCP tool '%s' failed with non-retryable error", runtime_tool_name)
                    raise
        finally:
            with self._inflight_condition:
                self._inflight -= 1
                self._inflight_condition.notify_all()

        assert last_error is not None
        raise MCPToolExhaustedError(runtime_tool_name, max_attempts, last_error)

    def call_tool(self, namespaced_name: str, args: dict[str, Any]) -> MCPCallResult:
        """Call a connected MCP tool by runtime namespaced name."""
        try:
            return self.execute_tool(namespaced_name, args)
        except (MCPToolTimeoutError, MCPToolExhaustedError) as exc:
            return _error_result(namespaced_name, str(exc))
        except Exception as exc:
            return _error_result(namespaced_name, str(exc))

    def list_resources(self, server_name: str) -> list[dict[str, Any]]:
        bridge = self._bridges.get(server_name)
        if bridge is None or not bridge.is_connected:
            raise ConnectionError(f"MCP server '{server_name}' is not connected")
        return self._run_coro(
            bridge.list_resources(),
            timeout=bridge.config.timeout_seconds,
        )

    def read_resource(
        self,
        server_name: str,
        uri: str,
    ) -> dict[str, Any]:
        bridge = self._bridges.get(server_name)
        if bridge is None or not bridge.is_connected:
            raise ConnectionError(f"MCP server '{server_name}' is not connected")
        return self._run_coro(
            bridge.read_resource(uri),
            timeout=bridge.config.timeout_seconds,
        )

    # ── Prompts (P0_2) ─────────────────────────────────────────────────

    def list_prompts(self, server_name: str) -> list[dict[str, Any]]:
        """List available prompts from a connected MCP server."""
        bridge = self._bridges.get(server_name)
        if bridge is None or not bridge.is_connected:
            raise ConnectionError(f"MCP server '{server_name}' is not connected")
        return self._run_coro(
            bridge.list_prompts(),
            timeout=bridge.config.timeout_seconds,
        )

    def get_prompt(
        self,
        server_name: str,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve a specific prompt from a connected MCP server."""
        bridge = self._bridges.get(server_name)
        if bridge is None or not bridge.is_connected:
            raise ConnectionError(f"MCP server '{server_name}' is not connected")
        return self._run_coro(
            bridge.get_prompt(name, arguments=arguments),
            timeout=bridge.config.timeout_seconds,
        )

    def close_server(self, server_name: str) -> None:
        """Close one connected server and remove its tool registrations."""
        bridge = self._bridges.pop(server_name, None)
        if bridge is not None:
            try:
                self._run_coro(bridge.close(), timeout=5.0)
            except Exception:
                logger.debug("Failed to close MCP server %s", server_name, exc_info=True)
        for tool_name, (mapped_server, _) in list(self._tool_map.items()):
            if mapped_server == server_name:
                del self._tool_map[tool_name]
        self._server_tools.pop(server_name, None)
        self._configs.pop(server_name, None)
        self._failed_servers.pop(server_name, None)

    def close_all(
        self,
        *,
        drain_timeout: float = 5.0,
        close_timeout: float = 5.0,
    ) -> None:
        """Drain calls, close transports, and stop the owned event-loop thread."""
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + max(0.0, drain_timeout)
        with self._inflight_condition:
            while self._inflight and time.monotonic() < deadline:
                self._inflight_condition.wait(
                    timeout=max(0.0, deadline - time.monotonic()),
                )
        if self._inflight:
            logger.error(
                "leaked_operation: %d MCP call(s) did not drain",
                self._inflight,
            )
        self._watchdog_future.cancel()
        try:
            self._watchdog_future.result(
                timeout=max(0.1, close_timeout),
            )
        except FutureCancelledError:
            pass
        except FutureTimeoutError:
            logger.error(
                "leaked_operation: MCP watchdog did not stop",
            )
        for server_name in list(self._bridges.keys()):
            bridge = self._bridges.pop(server_name)
            try:
                future = asyncio.run_coroutine_threadsafe(
                    bridge.close(),
                    self._loop,
                )
                future.result(timeout=max(0.1, close_timeout))
            except Exception:
                logger.error(
                    "leaked_operation: MCP bridge close failed for %s",
                    server_name,
                    exc_info=True,
                )
        self._bridges.clear()
        self._configs.clear()
        self._tool_map.clear()
        self._server_tools.clear()
        self._failed_servers.clear()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=max(0.1, close_timeout))
        if self._thread.is_alive():
            logger.error("leaked_operation: MCP event-loop thread did not stop")
            return
        self._loop.close()

    def _execute_once(
        self,
        runtime_tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        idle_timeout: float | None = None,
        attempt: int = 0,
    ) -> MCPCallResult:
        parsed = self._tool_map.get(runtime_tool_name)
        if parsed is None:
            parsed = self._parse_namespaced_name(runtime_tool_name)
            if parsed is None:
                raise KeyError(f"Invalid MCP tool name: {runtime_tool_name}")

        server_name, tool_name = parsed
        bridge = self._bridges.get(server_name)
        if bridge is None:
            raise ConnectionError(f"MCP server '{server_name}' is not connected")

        # MCP-04: attempt reconnection if bridge disconnected
        if not bridge.is_connected:
            logger.info("MCP server '%s' disconnected, attempting reconnect...", server_name)
            if hasattr(self, "_loop") and self._loop is not None:
                reconnected = self._run_coro(self._reconnect(server_name, bridge))
                if not reconnected:
                    raise ConnectionError(
                        f"MCP server '{server_name}' is not connected and reconnection failed"
                    )
            else:
                raise ConnectionError(
                    f"MCP server '{server_name}' is not connected (no event loop for reconnect)"
                )

        # Use idle_timeout from server config if available, else fall back to total timeout
        effective_timeout = idle_timeout if idle_timeout is not None else timeout

        future = asyncio.run_coroutine_threadsafe(bridge.call_tool(tool_name, arguments), self._loop)
        try:
            return future.result(timeout=effective_timeout)
        except (FutureTimeoutError, asyncio.TimeoutError) as exc:
            future.cancel()
            raise MCPToolTimeoutError(runtime_tool_name, effective_timeout, attempt) from exc
        except asyncio.CancelledError as exc:
            raise MCPToolTimeoutError(runtime_tool_name, effective_timeout, attempt) from exc
        except Exception as exc:
            bridge.mark_disconnected()
            raise ConnectionError(
                f"MCP transport failed for '{server_name}': {exc}",
            ) from exc

    def _parse_namespaced_name(self, namespaced_name: str) -> tuple[str, str] | None:
        parts = namespaced_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp" or not parts[1] or not parts[2]:
            return None
        return parts[1], parts[2]

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro, *, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SyncMCPToolManager is closed")

    # ── MCP-04: Automatic reconnection ──────────────────────────────

    async def _reconnect(self, name: str, bridge: MCPToolBridge) -> bool:
        """Respawn one ephemeral bridge under the shared sliding window."""
        config = self._configs.get(name)
        if config is None or not self._restart_allowed(name):
            self._failed_servers[name] = "restart window exhausted"
            return False
        old_fingerprint = bridge.fingerprint
        for i in range(self.MAX_RECONNECT_ATTEMPTS):
            if not self._restart_allowed(name):
                self._failed_servers[name] = "restart window exhausted"
                return False
            delay = min(
                self.RECONNECT_BASE_DELAY * (2 ** i),
                16.0,
            )
            logger.info(
                "MCP reconnect attempt %d/%d for '%s' in %.1fs",
                i + 1, self.MAX_RECONNECT_ATTEMPTS, name, delay,
            )
            await asyncio.sleep(delay)
            try:
                self._record_restart(name)
                try:
                    await asyncio.wait_for(bridge.close(), timeout=5.0)
                except Exception:
                    logger.warning(
                        "Old MCP bridge did not close cleanly: %s",
                        name,
                    )
                replacement = create_mcp_bridge(config)
                tools = await asyncio.wait_for(
                    replacement.connect(),
                    timeout=config.timeout_seconds,
                )
                replacement.set_tools_changed_callback(
                    self._on_tools_changed,
                )
                self._bridges[name] = replacement
                self._refresh_tool_map(name, replacement, tools)
                if (
                    old_fingerprint
                    and replacement.fingerprint != old_fingerprint
                ):
                    logger.info(
                        "MCP server fingerprint changed for %s; "
                        "tool cache was rebuilt",
                        name,
                    )
                self._failed_servers.pop(name, None)
                logger.info("MCP server '%s' reconnected successfully", name)
                return True
            except Exception as exc:
                logger.warning(
                    "MCP reconnect attempt %d/%d for '%s' failed: %s",
                    i + 1, self.MAX_RECONNECT_ATTEMPTS, name, exc,
                )
        logger.error(
            "MCP server '%s' failed to reconnect after %d attempts", name, self.MAX_RECONNECT_ATTEMPTS,
        )
        return False

    def _restart_allowed(self, name: str) -> bool:
        now = time.monotonic()
        history = self._restart_history[name]
        while history and now - history[0] > self._restart_window:
            history.popleft()
        return len(history) < self._restart_limit

    def _record_restart(self, name: str) -> None:
        self._restart_history[name].append(time.monotonic())

    async def _watchdog(self) -> None:
        """Health-check every server and restart failed transports."""
        while not self._closed:
            await asyncio.sleep(self._health_interval)
            if self._closed:
                return
            for name, bridge in list(self._bridges.items()):
                try:
                    if bridge.launch_fingerprint_changed():
                        logger.info(
                            "MCP launch fingerprint changed for %s; respawning",
                            name,
                        )
                        bridge.mark_disconnected()
                        await self._reconnect(name, bridge)
                        continue
                    tools = await asyncio.wait_for(
                        bridge.discover_tools(),
                        timeout=min(10.0, self._health_interval),
                    )
                    current = {
                        tool_name
                        for tool_name in self._server_tools.get(name, ())
                        if not tool_name.endswith(
                            ("__list_resources", "__read_resource"),
                        )
                    }
                    if {
                        tool.runtime_name for tool in tools
                    } != current:
                        self._refresh_tool_map(name, bridge, tools)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    bridge.mark_disconnected()
                    logger.warning(
                        "MCP watchdog marked %s unhealthy: %s",
                        name,
                        exc,
                    )
                    await self._reconnect(name, bridge)

    def _refresh_tool_map(
        self, server_name: str, bridge: MCPToolBridge, tools: list[Any],
    ) -> None:
        """Update the tool map for a reconnected bridge."""
        # Remove old entries for this server
        stale = [k for k, v in self._tool_map.items() if v[0] == server_name]
        for k in stale:
            del self._tool_map[k]
        self._server_tools[server_name] = []
        # Register new tools
        runtime_tools: list[Any] = []
        for tool_info in tools:
            runtime_tool = mcp_tool_to_runtime_tool(self, tool_info)
            runtime_tools.append(runtime_tool)
            self._tool_map[runtime_tool.name] = (server_name, tool_info.name)
            self._server_tools[server_name].append(runtime_tool.name)
        from agent.mcp.tool_adapter import (
            create_resource_list_tool,
            create_resource_read_tool,
        )
        for resource_tool in (
            create_resource_list_tool(self, server_name),
            create_resource_read_tool(self, server_name),
        ):
            runtime_tools.append(resource_tool)
            self._tool_map[resource_tool.name] = (
                server_name,
                resource_tool.name,
            )
            self._server_tools[server_name].append(resource_tool.name)
        if callable(self._tools_changed_callback):
            self._tools_changed_callback(server_name, runtime_tools)

    def _on_tools_changed(self, server_name: str, tools: list[Any]) -> None:
        bridge = self._bridges.get(server_name)
        if bridge is not None:
            self._refresh_tool_map(server_name, bridge, tools)


def _error_result(tool_name: str, message: str) -> MCPCallResult:
    return MCPCallResult(
        content=[{"text": message}],
        is_error=True,
        metadata={
            "mcp_tool": tool_name,
            "mcp_is_error": True,
            "mcp_error": message,
        },
    )
