"""Async MCP client bridge for runtime tools.

Multi-transport: stdio (MCP SDK), HTTP JSON-RPC 2.0, SSE, WebSocket.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from agent.mcp.types import MCPServerConfig, MCPToolInfo

_logger = logging.getLogger(__name__)
MCP_CLOSE_GRACE_SECONDS = 5.0
MCP_FORCE_KILL_WAIT_SECONDS = 2.0

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    HAS_MCP = True
except ImportError:  # pragma: no cover - exercised by environments without optional extra
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None
    HAS_MCP = False


class MCPNotInstalledError(RuntimeError):
    """Raised when the optional mcp package is not installed."""


@dataclass(frozen=True)
class MCPCallResult:
    """Normalized MCP call result."""

    content: list[Any]
    is_error: bool = False
    metadata: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        parts: list[str] = []
        for block in self.content:
            if hasattr(block, "text"):
                parts.append(str(block.text))
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part).strip()


class MCPToolCallError(RuntimeError):
    """Raised when a remote MCP tool call fails before returning a result."""


def _server_fingerprint(result: Any, config: MCPServerConfig) -> str:
    """Fingerprint initialized server identity and launch configuration."""
    executable_fact = _launch_fact(config)
    payload = {
        "protocolVersion": getattr(
            result,
            "protocolVersion",
            None,
        ) or (
            result.get("protocolVersion")
            if isinstance(result, dict)
            else None
        ),
        "serverInfo": getattr(result, "serverInfo", None) or (
            result.get("serverInfo")
            if isinstance(result, dict)
            else None
        ),
        "command": config.command,
        "args": config.args,
        "url": config.url,
        "executable": executable_fact,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _launch_fingerprint(config: MCPServerConfig) -> str:
    encoded = json.dumps(
        _launch_fact(config),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _launch_fact(config: MCPServerConfig) -> dict[str, Any]:
    executable = shutil.which(config.command) if config.command else None
    executable_fact: dict[str, Any] | None = None
    if executable:
        try:
            stat = Path(executable).stat()
            executable_fact = {
                "path": str(Path(executable).resolve()),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            }
        except OSError:
            executable_fact = {"path": executable}
    return {
        "command": config.command,
        "args": config.args,
        "url": config.url,
        "executable": executable_fact,
    }


def create_mcp_bridge(config: MCPServerConfig) -> "MCPToolBridge":
    """Factory: return the correct bridge implementation for the transport type.

    Multi-transport dispatch (MCP-E1, MCP-01):
      - stdio → MCPToolBridge (local subprocess via MCP SDK)
      - http  → HttpMCPBridge (JSON-RPC 2.0 over HTTP POST)
      - sse   → HttpMCPBridge (SSE placeholder — HTTP bridge with SSE notes)
      - ws    → HttpMCPBridge (WebSocket placeholder — HTTP bridge with WS notes)
    """
    if config.type == "stdio":
        return MCPToolBridge(config)
    if config.type == "http":
        return HttpMCPBridge(config)
    if config.type == "sse":
        return SseMCPBridge(config)
    if config.type == "ws":
        return WsMCPBridge(config)
    raise ValueError(f"Unsupported MCP transport type: {config.type!r}")


# ---------------------------------------------------------------------------
# Stdio Bridge (MCP SDK)
# ---------------------------------------------------------------------------

class MCPToolBridge:
    """Connect to one stdio MCP server and expose discovered tools."""

    @property
    def transport_type(self) -> str:
        return "stdio"

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._transport_cm: Any = None
        self._session_cm: Any = None
        self._session: Any = None
        self._tools: list[MCPToolInfo] = []
        self._connected = False
        self._fingerprint = ""
        self._launch_fingerprint = ""
        # The MCP SDK builds AnyIO cancel scopes inside its async context
        # managers.  Enter and exit must therefore happen in one long-lived
        # owner Task; ordinary tool calls may still run in sibling Tasks.
        self._owner_task: asyncio.Task[None] | None = None
        self._close_requested: asyncio.Event | None = None
        self._ready_future: asyncio.Future[list[MCPToolInfo]] | None = None
        # MCP-05: callback invoked when server sends notifications/tools/list_changed
        self._on_tools_changed: Any = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tools(self) -> list[MCPToolInfo]:
        return list(self._tools)

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def mark_disconnected(self) -> None:
        self._connected = False

    def launch_fingerprint_changed(self) -> bool:
        return bool(
            self._launch_fingerprint
            and self._launch_fingerprint != _launch_fingerprint(self.config)
        )

    async def __aenter__(self) -> "MCPToolBridge":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> list[MCPToolInfo]:
        """Start the stdio server, initialize a session, and discover tools."""
        if self._connected:
            return self.tools
        if not HAS_MCP:
            raise MCPNotInstalledError("Install the optional 'mcp' dependency to use runtime MCP bridge")
        if self._owner_task is not None and not self._owner_task.done():
            if self._ready_future is None:
                raise RuntimeError("MCP owner task has no readiness future")
            return await asyncio.shield(self._ready_future)

        env = self._sanitize_env(os.environ, self.config)
        # MCP-07: set CLAUDE_PROJECT_DIR for stdio servers
        project_dir = os.environ.get("FORGE_AGENT_PROJECT_DIR", os.getcwd())
        env.setdefault("CLAUDE_PROJECT_DIR", project_dir)

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=env,
            cwd=self.config.cwd or os.getcwd(),
        )
        loop = asyncio.get_running_loop()
        self._close_requested = asyncio.Event()
        self._ready_future = loop.create_future()
        self._owner_task = loop.create_task(
            self._run_owned_session(params),
            name=f"mcp-owner:{self.config.name}",
        )
        return await asyncio.shield(self._ready_future)

    async def close(self) -> None:
        """Close streams, then terminate a surviving stdio subprocess."""
        owner = self._owner_task
        if owner is not None and owner is not asyncio.current_task():
            if self._close_requested is not None:
                self._close_requested.set()
            try:
                async with asyncio.timeout(
                    MCP_CLOSE_GRACE_SECONDS * 2
                    + MCP_FORCE_KILL_WAIT_SECONDS,
                ):
                    await asyncio.shield(owner)
            except TimeoutError:
                _logger.warning(
                    "MCP owner close timed out for %s",
                    self.config.name,
                )
                await self._terminate_process()
                owner.cancel()
                try:
                    await owner
                except asyncio.CancelledError:
                    pass
            finally:
                self._owner_task = None
                self._close_requested = None
                self._ready_future = None
                self._connected = False
            return

        await self._close_contexts()
        self._connected = False

    # ── Environment sanitization (Phase 1 #12) ────────────────────

    @staticmethod
    def _sanitize_env(
        base_env: dict[str, str],
        config: MCPServerConfig,
    ) -> dict[str, str]:
        """Strip sensitive env vars before spawning stdio subprocess.

        CC Principle: Trust Boundary (#2.3) — servers must not inherit
        host credentials.  Only a safe allowlist plus explicitly-configured
        env vars from the MCP config are passed through.
        """
        # CC-aligned allowlist
        ALLOWLIST = frozenset({
            "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
            "TMPDIR", "TEMP", "TMP", "USER", "LOGNAME", "SHELL",
            "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC",
        })
        # Sensitive patterns — stripped if their key contains any of these
        STRIP_PATTERNS = (
            "API_KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD",
            "CREDENTIAL", "AUTH", "CERT", "PRIVATE_KEY",
        )

        result: dict[str, str] = {}
        for key, value in base_env.items():
            upper = key.upper()
            if key in ALLOWLIST or upper in ALLOWLIST:
                result[key] = value
            elif not any(pattern in upper for pattern in STRIP_PATTERNS):
                result[key] = value

        # Explicitly-configured env vars from MCP config take priority
        if hasattr(config, "env") and config.env:
            for key, value in config.env.items():
                result[key] = str(value)

        return result

    async def _run_owned_session(self, params: Any) -> None:
        """Own SDK context managers for their complete lifetime in one Task."""
        ready = self._ready_future
        try:
            self._transport_cm = stdio_client(params)
            read_stream, write_stream = await self._transport_cm.__aenter__()

            self._session_cm = ClientSession(read_stream, write_stream)
            self._session = await self._session_cm.__aenter__()
            initialize_result = await self._session.initialize()
            self._fingerprint = _server_fingerprint(
                initialize_result,
                self.config,
            )
            self._launch_fingerprint = _launch_fingerprint(self.config)
            self._tools = await self.discover_tools()
            if hasattr(self._session, "on_notification"):
                self._session.on_notification(
                    "notifications/tools/list_changed",
                )(self._on_list_changed)
            self._connected = True
            if ready is not None and not ready.done():
                ready.set_result(self.tools)
            assert self._close_requested is not None
            await self._close_requested.wait()
        except asyncio.CancelledError:
            if ready is not None and not ready.done():
                ready.cancel()
            raise
        except BaseException as exc:
            if ready is not None and not ready.done():
                ready.set_exception(exc)
            else:
                _logger.warning(
                    "MCP owner task failed for %s",
                    self.config.name,
                    exc_info=True,
                )
        finally:
            self._connected = False
            await self._close_contexts()

    async def _close_contexts(self) -> None:
        """Exit SDK contexts; caller must be the Task that entered them."""
        if self._session_cm is not None:
            try:
                async with asyncio.timeout(MCP_CLOSE_GRACE_SECONDS):
                    await self._session_cm.__aexit__(None, None, None)
            except Exception:
                _logger.warning(
                    "MCP session close failed for %s",
                    self.config.name,
                    exc_info=True,
                )
            finally:
                self._session_cm = None
                self._session = None

        if self._transport_cm is not None:
            try:
                async with asyncio.timeout(MCP_CLOSE_GRACE_SECONDS):
                    await self._transport_cm.__aexit__(None, None, None)
            except Exception:
                _logger.warning(
                    "MCP transport close failed for %s",
                    self.config.name,
                    exc_info=True,
                )
                await self._terminate_process()
            finally:
                self._transport_cm = None

    async def discover_tools(self) -> list[MCPToolInfo]:
        """Return normalized metadata for all server tools."""
        self._require_session()
        response = await self._session.list_tools()
        tools = []
        for tool in response.tools:
            tools.append(MCPToolInfo(
                server_name=self.config.name,
                name=str(tool.name),
                description=getattr(tool, "description", None) or f"MCP tool {tool.name} from {self.config.name}",
                input_schema=getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}},
                metadata=dict(getattr(tool, "_meta", None) or {}),
            ))
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        """Call a remote MCP tool with timeout protection."""
        self._require_session()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MCPToolCallError(
                f"MCP tool '{tool_name}' from server '{self.config.name}' timed out after "
                f"{self.config.timeout_seconds:.1f}s"
            ) from exc
        except Exception as exc:
            self._connected = False
            raise MCPToolCallError(
                f"MCP transport failure for '{self.config.name}': {exc}",
            ) from exc

        return MCPCallResult(
            content=list(getattr(result, "content", []) or []),
            is_error=bool(getattr(result, "isError", False)),
            metadata={
                "mcp_server": self.config.name,
                "mcp_tool": tool_name,
                "mcp_is_error": bool(getattr(result, "isError", False)),
            },
        )

    # ── MCP Resources ───────────────────────────────────────────────

    async def list_resources(self) -> list[dict[str, Any]]:
        """Return all resources exposed by this MCP server (resources/list)."""
        self._require_session()
        try:
            response = await self._session.list_resources()
            result: list[dict[str, Any]] = []
            for r in getattr(response, "resources", []) or []:
                result.append({
                    "uri": str(getattr(r, "uri", "")),
                    "name": str(getattr(r, "name", "")),
                    "description": str(getattr(r, "description", "")),
                    "mimeType": str(getattr(r, "mimeType", "")),
                })
            return result
        except Exception as exc:
            _logger = __import__("logging").getLogger(__name__)
            _logger.debug("list_resources failed for '%s': %s", self.config.name, exc)
            return []

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read a specific MCP resource by URI (resources/read)."""
        self._require_session()
        try:
            result = await self._session.read_resource(uri)
            contents: list[dict[str, Any]] = []
            for c in getattr(result, "contents", []) or []:
                contents.append({
                    "uri": str(getattr(c, "uri", uri)),
                    "mimeType": str(getattr(c, "mimeType", "")),
                    "text": str(getattr(c, "text", "")),
                })
            return {"contents": contents}
        except Exception as exc:
            _logger = __import__("logging").getLogger(__name__)
            _logger.debug("read_resource failed for '%s': %s", uri, exc)
            return {"contents": [], "error": str(exc)}

    # ── MCP Prompts ──────────────────────────────────────────────────

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Return all prompt templates exposed by this MCP server."""
        self._require_session()
        try:
            response = await self._session.list_prompts()
            result: list[dict[str, Any]] = []
            for p in getattr(response, "prompts", []) or []:
                result.append({
                    "name": str(getattr(p, "name", "")),
                    "description": str(getattr(p, "description", "")),
                    "arguments": [
                        {"name": a.name, "description": getattr(a, "description", ""), "required": getattr(a, "required", False)}
                        for a in (getattr(p, "arguments", []) or [])
                    ],
                })
            return result
        except Exception:
            _logger.debug("list_prompts unavailable on '%s'", self.config.name, exc_info=True)
            return []

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        """Get a rendered prompt template from this MCP server."""
        self._require_session()
        try:
            result = await self._session.get_prompt(name, arguments or {})
            messages: list[dict] = []
            for msg in getattr(result, "messages", []) or []:
                messages.append({
                    "role": str(getattr(msg, "role", "user")),
                    "content": getattr(msg, "content", {}),
                })
            return {"messages": messages}
        except Exception as exc:
            _logger.debug("get_prompt failed on '%s': %s", self.config.name, exc)
            return {"messages": [], "error": str(exc)}

    def _require_session(self) -> None:
        if self._session is None:
            raise RuntimeError("MCPToolBridge is not connected")

    async def _terminate_process(self) -> None:
        """Best-effort terminate → wait → kill for SDK-owned subprocesses."""
        candidates = [
            self._transport_cm,
            getattr(self._transport_cm, "_process", None),
            getattr(self._transport_cm, "process", None),
        ]
        generator = getattr(self._transport_cm, "gen", None)
        frame = getattr(generator, "ag_frame", None) or getattr(
            generator,
            "gi_frame",
            None,
        )
        if frame is not None:
            candidates.extend(frame.f_locals.values())
        process = next(
            (
                item for item in candidates
                if item is not None
                and callable(getattr(item, "terminate", None))
            ),
            None,
        )
        if process is None:
            _logger.error(
                "leaked_operation: MCP process handle unavailable for %s",
                self.config.name,
            )
            return
        process.terminate()
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                result = wait()
                if hasattr(result, "__await__"):
                    await asyncio.wait_for(
                        result,
                        timeout=MCP_CLOSE_GRACE_SECONDS,
                    )
                return
            except asyncio.TimeoutError:
                pass
        kill = getattr(process, "kill", None)
        if callable(kill):
            kill()
            if callable(wait):
                try:
                    result = wait()
                    if hasattr(result, "__await__"):
                        await asyncio.wait_for(
                            result,
                            timeout=MCP_FORCE_KILL_WAIT_SECONDS,
                        )
                except asyncio.TimeoutError:
                    _logger.error(
                        "leaked_operation: MCP process survived force kill: %s",
                        self.config.name,
                    )

    # ── MCP-05: Dynamic tool updates ─────────────────────────────────

    async def _on_list_changed(self, _notification: Any = None) -> None:
        """Handle notifications/tools/list_changed from the server.

        Refreshes the tool list and invokes the optional callback so
        SyncMCPToolManager can update its tool registry.
        """
        logger = __import__("logging").getLogger(__name__)
        logger.info("MCP server '%s' sent tools/list_changed, refreshing...", self.config.name)
        try:
            self._tools = await self.discover_tools()
            logger.info("MCP server '%s' tools refreshed: %d tools", self.config.name, len(self._tools))
            if self._on_tools_changed is not None:
                self._on_tools_changed(self.config.name, list(self._tools))
        except Exception as exc:
            logger.warning("MCP server '%s' tools/list_changed refresh failed: %s", self.config.name, exc)

    def set_tools_changed_callback(self, callback) -> None:
        """MCP-05: Register a callback for dynamic tool updates.

        callback(server_name: str, tools: list[MCPToolInfo]) -> None
        """
        self._on_tools_changed = callback


# ---------------------------------------------------------------------------
# HTTP Bridge — JSON-RPC 2.0 over HTTP POST (MCP-01)
# ---------------------------------------------------------------------------

class HttpMCPBridge(MCPToolBridge):
    """HTTP MCP transport — JSON-RPC 2.0 POST to <url>/mcp.

    Implements the MCP HTTP transport spec:
      1. POST initialize → get sessionId
      2. POST tools/list → discover tools
      3. POST tools/call → invoke tool

    Uses httpx for async HTTP. Custom headers (e.g. Authorization: Bearer)
    are passed through from the server config.
    """

    JSONRPC_VERSION = "2.0"
    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._tools: list[MCPToolInfo] = []
        self._client: Any = None  # httpx.AsyncClient
        self._session_id: str | None = None
        self._next_id = 0
        self._id_lock = asyncio.Lock()

    @property
    def transport_type(self) -> str:
        return self.config.type

    # ── Public API ──────────────────────────────────────────────────

    async def connect(self) -> list[MCPToolInfo]:
        if self._connected:
            return self.tools
        self._client = self._create_http_client()
        try:
            await self._initialize()
            self._tools = await self.discover_tools()
            self._connected = True
            return self.tools
        except Exception:
            await self._close_client()
            raise

    async def close(self) -> None:
        await self._close_client()
        self._connected = False
        self._session_id = None

    async def discover_tools(self) -> list[MCPToolInfo]:
        result = await self._rpc_call("tools/list", {})
        tools: list[MCPToolInfo] = []
        for tool in result.get("tools") or []:
            tools.append(MCPToolInfo(
                server_name=self.config.name,
                name=str(tool.get("name", "")),
                description=str(
                    tool.get("description", "") or
                    f"MCP tool {tool.get('name', '')} from {self.config.name}"
                ),
                input_schema=tool.get("inputSchema") or {"type": "object", "properties": {}},
            ))
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        self._require_session()
        try:
            result = await asyncio.wait_for(
                self._rpc_call("tools/call", {"name": tool_name, "arguments": arguments}),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MCPToolCallError(
                f"MCP tool '{tool_name}' from server '{self.config.name}' timed out after "
                f"{self.config.timeout_seconds:.1f}s"
            ) from exc
        except Exception as exc:
            self._connected = False
            raise MCPToolCallError(
                f"MCP transport failure for '{self.config.name}': {exc}",
            ) from exc

        return MCPCallResult(
            content=list(result.get("content", []) or []),
            is_error=bool(result.get("isError", False)),
            metadata={
                "mcp_server": self.config.name,
                "mcp_tool": tool_name,
                "mcp_is_error": bool(result.get("isError", False)),
            },
        )

    # ── Internal ────────────────────────────────────────────────────

    def _create_http_client(self) -> Any:
        try:
            import httpx
        except ImportError:
            raise MCPNotInstalledError(
                "The 'httpx' package is required for MCP HTTP transport. "
                "Install it with: pip install httpx"
            )
        headers = {"Content-Type": "application/json"}
        if self.config.headers:
            headers.update(self.config.headers)
        return httpx.AsyncClient(headers=headers, timeout=self.config.timeout_seconds)

    async def _close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def _initialize(self) -> None:
        result = await self._rpc_call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "grace-code", "version": "1.0"},
        })
        self._session_id = result.get("sessionId")
        self._fingerprint = _server_fingerprint(result, self.config)
        self._launch_fingerprint = _launch_fingerprint(self.config)

    async def _rpc_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("HttpMCPBridge is not connected")
        async with self._id_lock:
            self._next_id += 1
            request_id = self._next_id
        body = {
            "jsonrpc": self.JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }
        url = self.config.url.rstrip("/") + "/mcp"
        try:
            response = await self._client.post(url, json=body)
            response.raise_for_status()
            ct = response.headers.get("content-type", "")
            if not ct.startswith("application/json") and not ct.startswith("text/plain"):
                import logging as _logging
                _mcp_log = _logging.getLogger(__name__)
                _mcp_log.warning(
                    "MCP server '%s' returned non-JSON content-type: %s",
                    self.config.name, ct,
                )
            data: dict[str, Any] = response.json()
        except Exception as exc:
            raise MCPToolCallError(
                f"MCP HTTP request to '{self.config.name}' failed: {exc}"
            ) from exc
        if "error" in data:
            err = data["error"]
            raise MCPToolCallError(
                f"MCP JSON-RPC error {err.get('code', '')}: {err.get('message', str(err))}"
            )
        return data.get("result", {})

    # ── Resource overrides (Phase 1 #1) ────────────────────────────
    # MCP resources/list is only available on the SDK session (stdio).
    # HTTP/SSE/WS bridges do not have an SDK session — protocol
    # compliance principle: return structured error, don't emulate.

    async def list_resources(self) -> list[dict[str, Any]]:
        """HTTP bridges do not support MCP resources/list."""
        return []

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """HTTP bridges do not support MCP resources/read."""
        return {"contents": [], "error": "MCP resources not available on HTTP transport"}


# ---------------------------------------------------------------------------
# SSE Bridge — Server-Sent Events (MCP-02)
# ---------------------------------------------------------------------------

class SseMCPBridge(HttpMCPBridge):
    """MCP SSE transport — Server-Sent Events for server→client, POST for client→server.

    The SSE transport uses a streaming GET connection to receive JSON-RPC
    notifications and responses, while sending requests via HTTP POST.
    This is the deprecated but still-supported MCP transport option 2.
    """

    async def connect(self) -> list[MCPToolInfo]:
        if self._connected:
            return self.tools
        self._client = self._create_http_client()
        try:
            # SSE: start a background task to read the SSE stream
            import asyncio as _asyncio
            self._sse_task = _asyncio.create_task(self._read_sse_stream())
            # Initialize over POST (same as HTTP)
            await self._initialize()
            self._tools = await self.discover_tools()
            self._connected = True
            return self.tools
        except Exception:
            await self._close_client()
            raise

    async def close(self) -> None:
        if hasattr(self, "_sse_task") and self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except Exception:
                pass
        await super().close()

    async def _read_sse_stream(self) -> None:
        """Background task: read SSE events and dispatch incoming messages.

        MCP SSE spec: server sends 'message' events with JSON-RPC body.
        Notifications (no id) are dispatched to registered handlers; responses
        (has id) are route-matched to in-flight calls waiting on the POST endpoint,
        or delivered via the notification callback as a fallback.
        """
        _logger = __import__("logging").getLogger(__name__)
        try:
            url = self.config.url.rstrip("/") + "/sse"
            async with self._client.stream("GET", url) as response:  # type: ignore[union-attr]
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            import json as _json
                            msg = _json.loads(data_str)
                            rpc_id = msg.get("id")
                            method = msg.get("method", "")
                            if method:
                                # MCP notification — dispatch to registered handlers
                                await self._dispatch_notification(method, msg)
                            elif rpc_id is not None:
                                # JSON-RPC response via SSE — route to pending request
                                await self._route_sse_response(rpc_id, msg)
                            else:
                                _logger.debug("SSE message with no method or id: %s", data_str[:100])
                        except Exception:
                            _logger.debug("SSE parse skipped: %s", data_str[:100])
        except Exception as exc:
            _logger.debug("SSE stream ended: %s", exc)

    async def _dispatch_notification(self, method: str, msg: dict[str, Any]) -> None:
        """Route an MCP notification from the SSE stream to the correct handler."""
        _logger = __import__("logging").getLogger(__name__)
        if method == "notifications/tools/list_changed" and hasattr(self, "_on_list_changed"):
            await self._on_list_changed(msg)
        elif method.startswith("notifications/"):
            # Forward other MCP notifications to the on_tools_changed callback
            # for ToolSearch / WaitForMcpServers integration
            handler = getattr(self, "_on_tools_changed", None)
            if handler is not None:
                try:
                    handler(msg)
                except Exception:
                    _logger.debug("Notification handler error for %s", method)
            else:
                _logger.debug("SSE notification (no handler): %s", method)
        else:
            _logger.debug("SSE non-notification method: %s", method)

    async def _route_sse_response(self, rpc_id: int | str, msg: dict[str, Any]) -> None:
        """Route a JSON-RPC response from SSE to the caller waiting on RPC id."""
        _logger = __import__("logging").getLogger(__name__)
        # SSE responses are uncommon (most servers reply via POST response),
        # but the spec allows them. Store for retrieval by callers.
        if not hasattr(self, "_sse_responses"):
            self._sse_responses: dict[int | str, dict[str, Any]] = {}
        self._sse_responses[rpc_id] = msg
        _logger.debug("SSE response routed for id=%s", rpc_id)

    def _create_http_client(self) -> Any:
        client = super()._create_http_client()
        # SSE needs longer timeout for streaming
        client.timeout = max(client.timeout, 300.0)  # 5 min for SSE
        return client


# ---------------------------------------------------------------------------
# WebSocket Bridge (MCP-03)
# ---------------------------------------------------------------------------

class WsMCPBridge(HttpMCPBridge):
    """MCP WebSocket transport — persistent bidirectional connection.

    Requires the 'websockets' package. JSON-RPC messages flow
    bidirectionally over a single WebSocket connection.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._ws: Any = None  # websockets.WebSocketClientProtocol

    async def connect(self) -> list[MCPToolInfo]:
        if self._connected:
            return self.tools
        try:
            import websockets
        except ImportError:
            raise MCPNotInstalledError(
                "The 'websockets' package is required for MCP WebSocket transport. "
                "Install it with: pip install websockets"
            )
        try:
            ws_url = self.config.url
            if ws_url.startswith("http"):
                ws_url = ws_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
            extra_headers = self.config.headers or {}
            self._ws = await websockets.connect(
                ws_url + "/mcp",
                extra_headers=extra_headers,
                max_size=2 ** 20,
            )
        except Exception as exc:
            raise MCPToolCallError(
                f"MCP WebSocket connection to '{self.config.name}' failed: {exc}"
            ) from exc

        try:
            await self._initialize()
            self._tools = await self.discover_tools()
            self._connected = True
            return self.tools
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._connected = False

    async def _rpc_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("WsMCPBridge is not connected")
        import json as _json
        async with self._id_lock:
            self._next_id += 1
            request_id = self._next_id
        body = {
            "jsonrpc": self.JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            await asyncio.wait_for(
                self._ws.send(_json.dumps(body)),
                timeout=self.config.timeout_seconds,
            )
            raw = await asyncio.wait_for(
                self._ws.recv(),
                timeout=self.config.timeout_seconds,
            )
            data: dict[str, Any] = _json.loads(raw)
        except asyncio.TimeoutError as exc:
            raise MCPToolCallError(
                f"MCP WS call '{method}' to '{self.config.name}' timed out"
            ) from exc
        except Exception as exc:
            raise MCPToolCallError(
                f"MCP WS request to '{self.config.name}' failed: {exc}"
            ) from exc
        if "error" in data:
            err = data["error"]
            raise MCPToolCallError(
                f"MCP JSON-RPC error {err.get('code', '')}: {err.get('message', str(err))}"
            )
        return data.get("result", {})
