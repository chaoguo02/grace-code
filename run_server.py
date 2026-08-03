"""G29: Single Native startup — no old EventBus, no dual relay.

Startup order: migration → components → owner lease → relay → API admission.
Shutdown order: stop admission → relay drain → gateway close → DB close.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from server.main import create_app, validate_bind_host

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

    # G29: Single Native startup
    db_path = os.path.join(repo, ".grace", "grace.db")

    from composition.runtime_composition import assemble
    from composition.application_components import ApplicationLifecycle

    print("  Assembling Native object graph...")
    # T24+T25: Pass tool_registry when available (AgentService._registry)
    # Default None = test/fake mode.
    components = assemble(db_path)
    lifecycle = ApplicationLifecycle(components)

    # Migration already done by assemble (SqliteOutboxStore.install)
    # Start relay
    lifecycle.start()
    print(f"  Native relay started (owner={components.lease.owner_id})")

    # Create FastAPI app with native components
    app = create_app(native_components=components)

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        lifecycle.stop()
        print("  Native relay stopped")
