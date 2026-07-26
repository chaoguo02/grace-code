"""
FastAPI app factory and CLI entry point for the Grace Code Web MVP.

Usage:
    # Start the web server
    python -m server.main --repo /path/to/repo

    # With custom model
    python -m server.main --repo . --model gpt-4 --port 8080

    # Without opening browser
    python -m server.main --repo . --no-browser

The server exposes:
    - ``POST /api/sessions`` — Create a new agent session
    - ``GET  /api/sessions`` — List all sessions
    - ``GET  /api/sessions/{id}`` — Get session details
    - ``GET  /api/sessions/{id}/messages`` — Get session messages
    - ``GET  /api/sessions/{id}/events`` — Get execution events
    - ``POST /api/sessions/{id}/chat`` — **Execute ReAct agent loop** (core)
    - ``POST /api/sessions/{id}/cancel`` — Cancel a running session
    - ``WS   /api/ws/sessions/{id}`` — Real-time event streaming
    - ``GET  /`` — Web UI (static HTML)
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import re as _re
import sys
import time as _time
import webbrowser
from collections import defaultdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# Ensure project root is on sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from server.services.agent_service import AgentService
from server.services.event_bus import EventBus

logger = logging.getLogger(__name__)


def validate_bind_host(host: str, *, allow_remote: bool) -> bool:
    """Enforce the unauthenticated server's explicit bind contract."""
    normalized = host.strip().strip("[]").lower()
    is_loopback = normalized == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback and not allow_remote:
        raise ValueError(
            "Refusing non-loopback bind without --allow-remote. "
            "The Web API has no authentication or per-user resource isolation."
        )
    return not is_loopback


# ─── Rate Limit Middleware (P1-26) ───────────────────────────────────────────


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-session token-bucket rate limiter.

    Chat endpoints (/messages): 10 requests per 60s per session.
    Other endpoints: 60 requests per 60s per client IP.
    Returns 429 + Retry-After header when the limit is exceeded.
    """

    _WINDOW: float = 60.0
    _CHAT_LIMIT: int = 10
    _GENERAL_LIMIT: int = 300  # Web UI makes many read requests on startup

    def __init__(self, app):
        super().__init__(app)
        self._buckets: dict[str, tuple[float, int]] = {}

    async def dispatch(self, request, call_next):
        path = request.url.path
        is_chat = "/messages" in path and request.method == "POST"

        if is_chat:
            m = _re.match(r"/api/sessions/([a-f0-9]+)/messages", path)
            key = m.group(1) if m else (request.client.host if request.client else "unknown")
            limit = self._CHAT_LIMIT
        else:
            key = request.client.host if request.client else "unknown"
            limit = self._GENERAL_LIMIT

        now = _time.time()
        entry = self._buckets.get(key)
        if entry is None or now - entry[0] > self._WINDOW:
            self._buckets[key] = (now, 1)
        else:
            window_start, count = entry
            if count >= limit:
                retry_after = int(self._WINDOW - (now - window_start)) + 1
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded ({limit} per {int(self._WINDOW)}s)"},
                    headers={"Retry-After": str(retry_after)},
                )
            self._buckets[key] = (window_start, count + 1)

        # Periodic cleanup to prevent unbounded memory growth
        if len(self._buckets) > 10_000:
            self._buckets = {
                k: v for k, v in self._buckets.items()
                if now - v[0] <= self._WINDOW
            }

        return await call_next(request)


# ─── App factory ────────────────────────────────────────────────────────────


def create_app(service: AgentService) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        service: Initialised AgentService singleton holding the
            SessionRuntime and all sub-services.

    Returns:
        FastAPI app with all routes mounted.
    """

    # ── Lifespan handler (startup / shutdown) ──────────────────────────
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Startup: no-op; AgentService is fully initialized before app creation.
        yield
        # Shutdown: release resources
        await service.shutdown()

    app = FastAPI(
        title="Grace Code Web MVP",
        version="0.1.0",
        description=(
            "Web interface for the Grace Code ReAct agent. "
            "Provides session management, chat execution, "
            "and real-time event streaming via WebSocket."
        ),
        lifespan=_lifespan,
    )

    app.add_middleware(RateLimitMiddleware)

    # Store service reference in app.state for dependency injection
    app.state.service = service

    # ── Dependency: get_service ───────────────────────────────────────────
    def get_service(request: Request) -> AgentService:
        return request.app.state.service

    # ── Register API routers ──────────────────────────────────────────────
    from server.routers.sessions import create_sessions_router
    from server.routers.approvals import create_approvals_router
    from server.routers.websocket import create_websocket_router
    from server.routers.config import create_config_router
    from server.routers.attachments import create_attachments_router
    from server.routers.stats import create_stats_router
    from server.routers.diffs import create_diffs_router
    from server.routers.memory import create_memory_router
    from server.routers.plans import create_plans_router
    from server.routers.reviews import create_reviews_router

    app.include_router(create_sessions_router(get_service))
    app.include_router(create_approvals_router(get_service))
    app.include_router(create_websocket_router(service))
    app.include_router(create_config_router(get_service))
    app.include_router(create_attachments_router(get_service))
    app.include_router(create_stats_router(get_service))
    app.include_router(create_diffs_router(get_service))
    app.include_router(create_memory_router(get_service))
    app.include_router(create_plans_router(get_service))
    app.include_router(create_reviews_router(get_service))

    # ── GET /api/skills ──────────────────────────────────────────────────

    @app.get("/api/skills", tags=["skills"])
    async def list_skills() -> list[dict]:
        """List all discovered skills for the frontend slash-command menu."""
        skill_registry = getattr(service._registry, "_skill_registry", None)
        if skill_registry is None:
            return []
        return [
            {
                "name": name,
                "display_name": m.display_name,
                "description": m.description[:200],
                "user_invocable": m.user_can_invoke,
            }
            for name, m in skill_registry.list_skill_entries()
        ]

    # ── Static / built frontend ─────────────────────────────────────────
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Serve built React app from web/dist/
    web_dist = Path(__file__).parent.parent / "web" / "dist"
    react_index = web_dist / "index.html" if web_dist.is_dir() else None
    if react_index and react_index.is_file():
        app.mount("/assets", StaticFiles(directory=str(web_dist / "assets")), name="assets")

        @app.get("/", include_in_schema=False)
        async def root() -> HTMLResponse:
            return HTMLResponse(
                content=react_index.read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-store"},
            )
    else:
        # Fallback: vanilla static index
        index_html = static_dir / "index.html"

        @app.get("/", include_in_schema=False)
        async def root() -> HTMLResponse:
            if index_html.exists():
                return HTMLResponse(
                    content=index_html.read_text(encoding="utf-8"),
                    headers={"Cache-Control": "no-store"},
                )
            return HTMLResponse(
                content="<h1>Grace Code Web MVP</h1><p>Frontend not found.</p>",
            )

    # ── Storage stats ────────────────────────────────────────────────────
    @app.get("/api/storage/stats", tags=["storage"])
    async def storage_stats(request: Request) -> dict:
        """Return storage backend statistics.

        **Response (200):**
        - ``backend`` (string): Backend type (``"sqlite"`` / ``"redis"``).
        - ``total_sessions`` (int): Total session count.
        - ``total_messages`` (int): Total message count across all sessions.
        - ``db_size_bytes`` (int|null): Database file size in bytes.
        """
        svc: AgentService = request.app.state.service
        stats = svc._storage.get_stats()
        # Count memories from MemoryStore
        memory_count = 0
        try:
            store = getattr(svc, "_memory_store", None)
            if store is not None:
                summaries = store.list_memories()
                memory_count = len(summaries)
        except Exception:
            pass
        return {
            "backend": stats.backend,
            "total_sessions": stats.total_sessions,
            "total_messages": stats.total_messages,
            "total_memories": memory_count,
            "db_size_bytes": stats.db_size_bytes,
        }

    return app


# ─── CLI entry point ────────────────────────────────────────────────────────


def main() -> None:
    """Parse CLI arguments, create service, start uvicorn server."""
    parser = argparse.ArgumentParser(
        prog="grace-code-server",
        description="Grace Code Web MVP — FastAPI server for the ReAct agent.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "Allow a non-loopback bind. WARNING: no authentication or "
            "per-user resource isolation is implemented."
        ),
    )
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--repo", default=".", help="Repository path for the agent to work on")
    parser.add_argument("--config", default=None, help="Path to config YAML file")
    parser.add_argument("--model", default=None, help="LLM model override")
    parser.add_argument("--provider", default=None, help="LLM provider override")
    parser.add_argument("--api-key", default=None, help="API key override")
    parser.add_argument("--base-url", default=None, help="LLM base URL override")
    parser.add_argument("--max-steps", type=int, default=None, help="Max agent steps")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()
    try:
        remote_bind = validate_bind_host(
            args.host,
            allow_remote=args.allow_remote,
        )
    except ValueError as exc:
        parser.error(str(exc))

    # Logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if remote_bind:
        logger.warning(
            "REMOTE BIND ENABLED on %s: authentication and per-user "
            "resource isolation are not implemented.",
            args.host,
        )

    # Resolve repo path
    repo_path = str(Path(args.repo).expanduser().resolve())
    if not Path(repo_path).is_dir():
        print(f"Error: repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Grace Code Web MVP")
    print(f"  repo    : {repo_path}")
    print(f"  model   : {args.model or '(from config)'}")
    print(f"  provider: {args.provider or '(from config)'}")

    # Create EventBus
    event_bus = EventBus(repo_path=repo_path)

    # Create AgentService
    service = AgentService(
        repo_path=repo_path,
        config_path=args.config,
        event_bus=event_bus,
        model=args.model,
        provider=args.provider,
        api_key=args.api_key,
        base_url=args.base_url,
        max_steps=args.max_steps,
    )

    # Ensure root session exists
    root_id = service.ensure_root_session()
    print(f"  root    : {root_id}")

    # Create FastAPI app
    app = create_app(service)

    # Auto-open browser
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    print(f"\n  Server: {url}")
    print(f"  Docs  : {url}/docs")
    print()

    # Start uvicorn on a loop whose exception handler suppresses Windows
    # ProactorEventLoop noise (ConnectionResetError, ConnectionAbortedError
    # in _ProactorBasePipeTransport._call_connection_lost() when a browser
    # hard-closes a WebSocket).  Harmless, but asyncio logs ERROR by default.
    log_level: str | int = "info" if args.verbose else "warning"
    import asyncio as _asyncio
    _config = uvicorn.Config(app, host=args.host, port=args.port, log_level=log_level)
    _server = uvicorn.Server(_config)
    _loop = _asyncio.new_event_loop()
    def _quiet_exc(_l, ctx):
        exc = ctx.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return
        _l.default_exception_handler(ctx)
    _loop.set_exception_handler(_quiet_exc)
    _asyncio.set_event_loop(_loop)
    _loop.run_until_complete(_server.serve())


if __name__ == "__main__":
    main()
