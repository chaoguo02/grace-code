"""Quick start script for the Grace Code Web MVP server."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.services.agent_service import AgentService
from server.services.event_bus import EventBus
from server.main import create_app, validate_bind_host
import uvicorn

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18768)
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    try:
        validate_bind_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))

    repo = str(os.path.abspath(args.repo))
    print(f"Starting Grace Code Web MVP on {args.host}:{args.port}")
    print(f"  repo: {repo}")

    event_bus = EventBus()
    service = AgentService(repo_path=repo, event_bus=event_bus)
    service.ensure_root_session()
    app = create_app(service)

    # P19: Start native event pipeline when GRACE_RUNTIME_MODE=NATIVE
    _native_shutdown = None
    if os.environ.get("GRACE_RUNTIME_MODE") == "NATIVE":
        from composition.runtime_composition import start_native_pipeline
        db_path = os.path.join(repo, ".grace", "grace.db")
        if os.path.exists(db_path):
            _native = start_native_pipeline(db_path)
            _native_shutdown = _native["shutdown"]
            print(f"  Native event pipeline started (worker={_native['relay']._worker_id})")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        if _native_shutdown is not None:
            _native_shutdown()
