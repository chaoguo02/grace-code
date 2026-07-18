"""
mcp/transport.py

MCP transport implementations — stdio subprocess + streamable HTTP.

CC reference: services/mcp/ supports 8 transport types.
We implement the two primary types:
  - StdioTransport: spawns a local subprocess, communicates via stdin/stdout
  - HttpTransport: streamable HTTP (POST + optional SSE)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# ── Abstract transport ───────────────────────────────────────────────────


class McpTransport(ABC):
    """Abstract transport for MCP client-server communication."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the transport connection."""
        ...

    @abstractmethod
    async def send(self, message: bytes) -> None:
        """Send a raw message to the server."""
        ...

    @abstractmethod
    async def receive(self) -> bytes:
        """Receive a raw message from the server."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the transport connection."""
        ...


# ── Stdio transport ──────────────────────────────────────────────────────


class StdioTransport(McpTransport):
    """Subprocess-based transport — spawns server as a child process.

    This is the default MCP transport (CC: StdioClientTransport).
    The server process communicates via stdin/stdout JSON-RPC.
    Stderr is captured for logging.
    """

    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None) -> None:
        self._command = command
        self._args = args or []
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return

        full_env = dict(os.environ)
        if self._env:
            full_env.update(self._env)

        logger.info("MCP stdio: spawning %s %s", self._command, " ".join(self._args))
        self._process = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
        self._connected = True

    async def send(self, message: bytes) -> None:
        if not self._process or not self._process.stdin:
            raise McpTransportError("Stdio transport not connected")
        self._process.stdin.write(message + b"\n")
        await self._process.stdin.drain()

    async def receive(self) -> bytes:
        if not self._process or not self._process.stdout:
            raise McpTransportError("Stdio transport not connected")
        line = await self._process.stdout.readline()
        if not line:
            raise McpTransportError("Stdio transport: server closed stdout")
        return line.rstrip(b"\r\n")

    async def close(self) -> None:
        if self._process:
            try:
                self._process.stdin.close()
            except Exception:
                pass
            try:
                self._process.kill()
            except Exception:
                pass
            await self._process.wait()
            self._process = None
        self._connected = False


# ── HTTP transport ───────────────────────────────────────────────────────


class HttpTransport(McpTransport):
    """Streamable HTTP transport (CC: StreamableHTTPTransport).

    Uses HTTP POST for requests.  Optimized for remote MCP servers.
    Does NOT implement SSE streaming — responses are synchronous.

    CC reference: 2025-03-26 spec recommendation replacing legacy SSE transport.
    """

    def __init__(self, url: str,
                 headers: dict[str, str] | None = None,
                 timeout: float = 30.0) -> None:
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._timeout = timeout
        self._connected = False
        self._session: Any = None  # httpx.AsyncClient (lazy init)
        self._pending_message: bytes | None = None

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            import httpx
            self._session = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    **self._headers,
                },
            )
        except ImportError:
            raise McpTransportError(
                "HTTP transport requires httpx. Install with: pip install httpx"
            )
        self._connected = True
        logger.info("MCP http: connected to %s", self._url)

    async def send(self, message: bytes) -> None:
        if not self._session:
            raise McpTransportError("HTTP transport not connected")
        # Send immediately — CC pattern: each request is independent
        self._pending_message = message

    async def receive(self) -> bytes:
        if not self._session:
            raise McpTransportError("HTTP transport not connected")
        message = self._pending_message
        self._pending_message = None  # consume
        if message is None:
            raise McpTransportError("No pending message to send — call send() before receive()")

        response = await self._session.post(self._url, content=message)
        response.raise_for_status()
        return response.content

    async def close(self) -> None:
        if self._session:
            await self._session.aclose()
            self._session = None
        self._connected = False


# ── Error ─────────────────────────────────────────────────────────────────


class McpTransportError(Exception):
    """Transport-level error (connection failed, process died, etc.)."""
