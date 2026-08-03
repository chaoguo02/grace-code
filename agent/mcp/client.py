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

    P0_2: Streamable HTTP migration.
      - stdio      -> MCPToolBridge (SDK stdio_client)
      - http       -> StreamableHttpBridge (SDK streamable_http_client)
      - sse        -> ValueError -- ambiguous; use 'http' for streamable
      - ws         -> WsMCPBridge (non-standard, isolated)
    """
    if config.type == "stdio":
        return MCPToolBridge(config)
    if config.type == "http":
        # P0_2: map 'http' to streamable -- SDK handles the protocol
        return StreamableHttpBridge(config)
    if config.type in ("sse", "sse-legacy"):
        raise ValueError(
            "Transport 'sse' is ambiguous and deprecated. "
            "Use 'http' for MCP 2025-06-18 Streamable HTTP (SDK), "
            "or 'sse-legacy' for the deprecated HTTP+SSE (2024-11-05) pattern."
        )
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

        env = dict(os.environ)
        if self.config.env:
            env.update(self.config.env)
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
        """Read a specific MCP resource by URI (resources/read).

        P0_2: Preserve blob (base64), mimeType, and annotations.
        """
        import base64 as _base64
        self._require_session()
        try:
            result = await self._session.read_resource(uri)
            contents: list[dict[str, Any]] = []
            for c in getattr(result, "contents", []) or []:
                entry: dict[str, Any] = {
                    "uri": str(getattr(c, "uri", uri)),
                    "mimeType": str(getattr(c, "mimeType", "")),
                }
                text = getattr(c, "text", None)
                blob = getattr(c, "blob", None)
                if text is not None:
                    entry["text"] = str(text)
                if blob is not None:
                    try:
                        entry["blob"] = _base64.b64encode(blob).decode("ascii")
                    except Exception:
                        entry["blob"] = None
                annotations = getattr(c, "annotations", None)
                if annotations is not None:
                    entry["annotations"] = dict(annotations) if hasattr(annotations, "__iter__") else str(annotations)
                contents.append(entry)
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
        """Best-effort terminate -> wait -> kill for SDK-owned subprocesses."""
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
# Streamable HTTP Bridge -- SDK-driven, spec-compliant (P0_2)
# ---------------------------------------------------------------------------

class StreamableHttpBridge(MCPToolBridge):
    """MCP Streamable HTTP transport -- uses official SDK streamable_http_client.

    Aligns with MCP 2025-06-18 Streamable HTTP spec.
    SDK handles: Mcp-Session-Id header, MCP-Protocol-Version negotiation,
    notifications/initialized, SSE stream reading, JSON-RPC id matching.

    This class only manages: transport lifecycle (owner task), fingerprint,
    connection state -- same responsibilities as the stdio MCPToolBridge.
    """

    @property
    def transport_type(self) -> str:
        return "streamable"

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._get_session_id: Any = None

    async def connect(self) -> list[MCPToolInfo]:
        if self._connected:
            return self.tools
        if not HAS_MCP:
            raise MCPNotInstalledError(
                "Install the optional 'mcp' dependency to use MCP Streamable HTTP bridge"
            )
        if self._owner_task is not None and not self._owner_task.done():
            await asyncio.wait_for(self._ready_future, timeout=self.config.timeout_seconds or 30)
            return self._ready_future.result() if self._ready_future else []

        self._close_requested = asyncio.Event()
        self._ready_future = asyncio.Future()
        self._owner_task = asyncio.ensure_future(self._run_owned_session())
        return await asyncio.wait_for(
            self._ready_future,
            timeout=self.config.timeout_seconds or 30,
        )

    async def _run_owned_session(self) -> None:
        """Owner task: enter SDK context managers, initialize, discover tools.

        Same pattern as MCPToolBridge._run_owned_session but uses
        streamable_http_client instead of stdio_client.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        url = self.config.url
        if not url:
            raise ValueError("MCPServerConfig.url is required for streamable transport")

        try:
            transport_cm = streamable_http_client(
                url=url,
                terminate_on_close=True,
            )
            read_stream, write_stream, get_session_id = await transport_cm.__aenter__()
            self._transport_cm = transport_cm
            self._get_session_id = get_session_id

            session_cm = ClientSession(read_stream, write_stream)
            session = await session_cm.__aenter__()
            self._session_cm = session_cm
            self._session = session

            # SDK handles: initialize -> initialized notification -> negotiate version
            init_result = await session.initialize()

            # Compute fingerprints
            self._fingerprint = _make_fingerprint(
                getattr(init_result, "protocolVersion", "unknown"),
                str(getattr(init_result, "serverInfo", {})),
                url,
            )
            self._launch_fingerprint = _launch_fingerprint(self.config)

            # Discover tools
            tools = await self.discover_tools()

            # Register for tool change notifications
            try:
                if hasattr(session, "list_tools"):
                    self._connected = True
                    self._ready_future.set_result(tools)
            except Exception:
                self._connected = True
                self._ready_future.set_result(tools)

            # Wait until close is requested
            await self._close_requested.wait()

        except Exception as exc:
            if self._ready_future is not None and not self._ready_future.done():
                self._ready_future.set_exception(exc)
            raise
        finally:
            self._connected = False
            await self._close_contexts()

    async def discover_tools(self) -> list[MCPToolInfo]:
        if self._session is None:
            return []
        result = await self._session.list_tools()
        tools: list[MCPToolInfo] = []
        for t in result.tools:
            tools.append(MCPToolInfo(
                server_name=self.config.name,
                name=t.name,
                description=getattr(t, "description", "") or "",
                input_schema=getattr(t, "inputSchema", {}) or {},
            ))
        self._tools = tools
        return tools

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPCallResult:
        if self._session is None:
            raise MCPToolCallError(tool_name, "Session not connected")
        try:
            coro = self._session.call_tool(tool_name, arguments)
            result = await asyncio.wait_for(coro, timeout=self.config.timeout_seconds or 30)
            content = getattr(result, "content", []) or []
            is_error = getattr(result, "isError", False)
            text_parts = [
                (c.text if hasattr(c, "text") else str(c))
                for c in content
            ]
            return MCPCallResult(
                content=text_parts,
                is_error=is_error,
            )
        except asyncio.TimeoutError:
            raise MCPToolCallError(tool_name, "Tool call timed out")
        except Exception as exc:
            self._connected = False
            raise MCPToolCallError(tool_name, str(exc)) from exc

    async def list_resources(self) -> list[dict]:
        if self._session is None:
            return []
        try:
            result = await self._session.list_resources()
            resources: list[dict] = []
            for r in getattr(result, "resources", []) or []:
                resources.append({
                    "uri": getattr(r, "uri", ""),
                    "name": getattr(r, "name", ""),
                    "description": getattr(r, "description", None),
                    "mimeType": getattr(r, "mimeType", None),
                })
            return resources
        except Exception:
            return []

    async def read_resource(self, uri: str) -> dict:
        if self._session is None:
            return {}
        try:
            result = await self._session.read_resource(uri)
            contents = getattr(result, "contents", []) or []
            text_parts = [
                (c.text if hasattr(c, "text") else str(c))
                for c in contents
            ]
            return {"uri": uri, "text": "\n".join(text_parts)}
        except Exception:
            return {}

    async def list_prompts(self) -> list[dict]:
        if self._session is None:
            return []
        try:
            result = await self._session.list_prompts()
            prompts: list[dict] = []
            for p in getattr(result, "prompts", []) or []:
                prompts.append({
                    "name": getattr(p, "name", ""),
                    "description": getattr(p, "description", None),
                })
            return prompts
        except Exception:
            return []

    async def get_prompt(self, name: str, arguments: dict | None = None) -> dict:
        if self._session is None:
            return {}
        try:
            result = await self._session.get_prompt(name, arguments=arguments or {})
            msgs = getattr(result, "messages", []) or []
            return {
                "name": name,
                "messages": [{"role": getattr(m, "role", ""), "content": str(getattr(m, "content", ""))} for m in msgs],
            }
        except Exception:
            return {}

# WebSocket Bridge (MCP-03)
# ---------------------------------------------------------------------------

class WsMCPBridge(MCPToolBridge):
    """MCP WebSocket transport -- persistent bidirectional connection.

    Non-standard MCP transport.  Requires the 'websockets' package.
    JSON-RPC messages flow bidirectionally over a single WebSocket.

    P0_2: Inherits directly from MCPToolBridge.  Self-contained JSON-RPC
    implementation (no dependency on deprecated HttpMCPBridge).
    """

    JSONRPC_VERSION = "2.0"

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._tools: list[MCPToolInfo] = []
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._next_id = 0
        self._id_lock = asyncio.Lock()
        # P0_2: single-reader + id dispatch for concurrent safety
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    @property
    def transport_type(self) -> str:
        return "ws-custom"

    def _dispatch_notification(self, method: str, params: dict[str, Any]) -> None:
        """Handle server-to-client notifications over WebSocket."""
        if method == "notifications/tools/list_changed":
            if self._on_tools_changed is not None:
                self._on_tools_changed(self.config.name)
        elif method.startswith("notifications/"):
            pass  # Reserved for future notification types

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

        # P0_2: start single background reader before any RPC calls
        self._reader_task = asyncio.ensure_future(self._read_loop())

        try:
            await self._initialize()
            self._tools = await self.discover_tools()
            self._connected = True
            return self.tools
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        # Cancel reader task
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        # Resolve all pending futures with error
        for _fut in self._pending.values():
            if not _fut.done():
                _fut.set_exception(RuntimeError("WebSocket connection closed"))
        self._pending.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._connected = False

    async def _read_loop(self) -> None:
        """Single background reader -- dispatches responses by id, notifications by method."""
        import json as _json
        while self._ws is not None:
            try:
                raw = await self._ws.recv()
            except Exception:
                break
            try:
                data: dict[str, Any] = _json.loads(raw)
            except Exception:
                continue

            msg_id = data.get("id")
            msg_method = data.get("method")

            if msg_id is not None and not msg_method:
                # JSON-RPC response -- route to pending future
                fut = self._pending.pop(msg_id, None)
                if fut is not None and not fut.done():
                    if "error" in data:
                        fut.set_exception(MCPToolCallError(
                            str(data["error"].get("message", "RPC error")),
                        ))
                    else:
                        fut.set_result(data.get("result", {}))
            elif msg_method:
                # Notification -- dispatch to handler
                self._dispatch_notification(msg_method, data.get("params", {}))

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
        fut: asyncio.Future = asyncio.Future()
        self._pending[request_id] = fut
        try:
            await asyncio.wait_for(
                self._ws.send(_json.dumps(body)),
                timeout=self.config.timeout_seconds,
            )
            result = await asyncio.wait_for(fut, timeout=self.config.timeout_seconds)
            return result
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise MCPToolCallError(
                f"MCP WS call '{method}' to '{self.config.name}' timed out"
            ) from exc
        except Exception as exc:
            raise MCPToolCallError(
                f"MCP WS request to '{self.config.name}' failed: {exc}"
            ) from exc

    # ── JSON-RPC methods (self-contained, not inherited) ──────────────

    async def _initialize(self) -> None:
        result = await self._rpc_call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "grace-code", "version": "1.0"},
        })
        self._session_id = result.get("sessionId")

    async def discover_tools(self) -> list[MCPToolInfo]:
        result = await self._rpc_call("tools/list", {})
        tools: list[MCPToolInfo] = []
        for tool in result.get("tools") or []:
            tools.append(MCPToolInfo(
                server_name=self.config.name,
                name=str(tool.get("name", "")),
                description=str(tool.get("description", "") or f"MCP tool"),
                input_schema=tool.get("inputSchema") or {"type": "object", "properties": {}},
            ))
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        try:
            result = await asyncio.wait_for(
                self._rpc_call("tools/call", {"name": tool_name, "arguments": arguments}),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MCPToolCallError(f"MCP WS tool '{tool_name}' timed out") from exc
        except Exception as exc:
            self._connected = False
            raise MCPToolCallError(f"MCP WS transport failure: {exc}") from exc
        return MCPCallResult(
            content=list(result.get("content", []) or []),
            is_error=bool(result.get("isError", False)),
        )

    async def list_resources(self) -> list[dict]:
        try:
            result = await self._rpc_call("resources/list", {})
            return list(result.get("resources", []) or [])
        except Exception:
            return []

    async def read_resource(self, uri: str) -> dict:
        try:
            result = await self._rpc_call("resources/read", {"uri": uri})
            contents = result.get("contents", []) or []
            text = "\n".join(c.get("text", "") for c in contents if c.get("text"))
            return {"uri": uri, "text": text}
        except Exception:
            return {}
