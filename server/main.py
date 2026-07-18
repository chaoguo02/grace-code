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
import logging
import os
import sys
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# Ensure project root is on sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from server.services.agent_service import AgentService
from server.services.event_bus import EventBus

logger = logging.getLogger(__name__)


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
        # Startup: nothing special (service is already initialised)
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

    app.include_router(create_sessions_router(get_service))
    app.include_router(create_approvals_router(get_service))
    app.include_router(create_websocket_router(service))
    app.include_router(create_config_router(get_service))
    app.include_router(create_attachments_router(get_service))

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
        return {
            "backend": stats.backend,
            "total_sessions": stats.total_sessions,
            "total_messages": stats.total_messages,
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

    # Logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
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

    # Start uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose else "warning")


if __name__ == "__main__":
    main()
