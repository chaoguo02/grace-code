"""Synchronous wrapper around async MCP tool bridges."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any

from runtime.mcp.client import MCPCallResult, MCPToolBridge, create_mcp_bridge
from runtime.mcp.tool_adapter import mcp_tool_to_runtime_tool
from runtime.mcp.types import MCPServerConfig

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

    def __init__(self, *, default_policy: ExecutionPolicy | None = None) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="runtime-mcp-tools", daemon=True)
        self._thread.start()
        self._bridges: dict[str, MCPToolBridge] = {}
        self._tool_map: dict[str, tuple[str, str]] = {}
        self._default_policy = default_policy or ExecutionPolicy()
        self._closed = False

    @property
    def bridges(self) -> dict[str, MCPToolBridge]:
        return dict(self._bridges)

    def __enter__(self) -> "SyncMCPToolManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close_all()

    def load_and_discover(self, server_configs: list[MCPServerConfig]) -> list[Any]:
        """Connect configured servers and return adapted runtime tools."""
        self._ensure_open()
        runtime_tools: list[Any] = []
        for config in server_configs:
            bridge = create_mcp_bridge(config)
            try:
                tools = self._run_coro(bridge.connect())
            except Exception as exc:  # pragma: no cover - exact SDK failures vary
                logger.warning("Failed to connect MCP server %s: %s", config.name, exc)
                try:
                    self._run_coro(bridge.close())
                except Exception:
                    logger.debug("Failed to close MCP server %s after connect failure", config.name, exc_info=True)
                continue

            self._bridges[config.name] = bridge
            for tool_info in tools:
                runtime_tool = mcp_tool_to_runtime_tool(bridge, tool_info)
                runtime_tools.append(runtime_tool)
                self._tool_map[runtime_tool.name] = (config.name, tool_info.name)
            # MCP Resources: also register resource list/read tools
            from runtime.mcp.tool_adapter import create_resource_list_tool, create_resource_read_tool
            resource_list_tool = create_resource_list_tool(bridge)
            runtime_tools.append(resource_list_tool)
            resource_read_tool = create_resource_read_tool(bridge)
            runtime_tools.append(resource_read_tool)
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

    def close_all(self) -> None:
        """Close all bridges and stop the background event loop."""
        if self._closed:
            return
        for bridge in list(self._bridges.values()):
            try:
                self._run_coro(bridge.close())
            except Exception:
                logger.debug("Failed to close MCP bridge", exc_info=True)
        self._bridges.clear()
        self._tool_map.clear()
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
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

    def _parse_namespaced_name(self, namespaced_name: str) -> tuple[str, str] | None:
        parts = namespaced_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp" or not parts[1] or not parts[2]:
            return None
        return parts[1], parts[2]

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SyncMCPToolManager is closed")

    # ── MCP-04: Automatic reconnection ──────────────────────────────

    async def _reconnect(self, name: str, bridge: MCPToolBridge) -> bool:
        """Attempt reconnection with exponential backoff. Returns True on success."""
        for i in range(self.MAX_RECONNECT_ATTEMPTS):
            delay = self.RECONNECT_BASE_DELAY * (2 ** i)
            logger.info(
                "MCP reconnect attempt %d/%d for '%s' in %.1fs",
                i + 1, self.MAX_RECONNECT_ATTEMPTS, name, delay,
            )
            await asyncio.sleep(delay)
            try:
                tools = await bridge.connect()
                self._refresh_tool_map(name, bridge, tools)
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

    def _refresh_tool_map(
        self, server_name: str, bridge: MCPToolBridge, tools: list[Any],
    ) -> None:
        """Update the tool map for a reconnected bridge."""
        # Remove old entries for this server
        stale = [k for k, v in self._tool_map.items() if v[0] == server_name]
        for k in stale:
            del self._tool_map[k]
        # Register new tools
        for tool_info in tools:
            runtime_tool = mcp_tool_to_runtime_tool(bridge, tool_info)
            self._tool_map[runtime_tool.name] = (server_name, tool_info.name)


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
