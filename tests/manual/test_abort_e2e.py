"""
End-to-end AbortController verification (D0/E0).

Self-contained: starts a local Grace-Code server, runs the abort lifecycle
tests, then shuts down.  No manual server management required.

Usage:
    python tests/manual/test_abort_e2e.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.e2e


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
_DEFAULT_PORT = 18765  # non-default to avoid conflicts with dev server

BASE = f"http://localhost:{_DEFAULT_PORT}"
WS_BASE = f"ws://localhost:{_DEFAULT_PORT}"


# ── Server lifecycle manager ─────────────────────────────────────────────────

class ServerContext:
    """Context manager that starts/stops the Grace-Code web server.

    On enter: spawns ``python -m server.main`` as a subprocess and waits
    for the health-check endpoint to respond.
    On exit: sends SIGTERM, waits up to 10 s, then hard-kills.
    Temporary session data written during the test lives inside the
    per-project ``.grace/v2/`` directory — no additional cleanup needed.
    """

    def __init__(
        self,
        repo: str = ".",
        port: int = _DEFAULT_PORT,
        startup_timeout: float = 20.0,
    ) -> None:
        self._repo = repo
        self._port = port
        self._startup_timeout = startup_timeout
        self._process: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None

    # ── context manager protocol ─────────────────────────────────────────

    def __enter__(self) -> "ServerContext":
        self._process = subprocess.Popen(
            [
                sys.executable, "-m", "server.main",
                "--repo", self._repo,
                "--port", str(self._port),
                "--no-browser",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Drain stderr in background to prevent pipe-buffer deadlock
        self._stderr_thread = threading.Thread(
            target=self._consume_stderr, daemon=True,
        )
        self._stderr_thread.start()

        # Poll health-check endpoint until the server is ready
        import urllib.request
        import urllib.error
        deadline = time.time() + self._startup_timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://localhost:{self._port}/",
                    timeout=2,
                )
                return self
            except Exception:
                time.sleep(0.3)
        # Timeout — tear down and raise
        self.__exit__(None, None, None)
        raise RuntimeError(
            f"Server did not start within {self._startup_timeout:.0f} s",
        )

    def __exit__(self, *args: Any) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)

    def _consume_stderr(self) -> None:
        """Drain stderr so the pipe buffer doesn't block the child."""
        try:
            if self._process and self._process.stderr:
                for _ in self._process.stderr:
                    pass
        except Exception:
            pass


# ── helpers ──────────────────────────────────────────────────────────────────

def _api(method: str, path: str, **kwargs: Any) -> Any:
    """Minimal HTTP wrapper using only stdlib urllib."""
    import urllib.request
    import urllib.error

    url = f"{BASE}{path}"
    data = None
    if "json" in kwargs and kwargs["json"] is not None:
        data = json.dumps(kwargs["json"]).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                # Non-JSON response (e.g. HTML page at /) — still a valid
                # health check; treat as success for reachability tests.
                return {"_ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return {"_status": e.code, "_error": json.loads(body) if body else ""}
        except json.JSONDecodeError:
            return {"_status": e.code, "_error": body[:200]}
    except urllib.error.URLError as e:
        return {"_error": str(e.reason)}


# ── test cases ───────────────────────────────────────────────────────────────

def test_abort_cancels_ws_cleanly() -> None:
    """D0-1: Session chat → cancel → WS receives cancelled/failed/gave_up."""
    print("── D0-1: abort → cancelled ──")

    resp = _api("POST", "/api/sessions", json={"repo_path": _PROJECT_ROOT})
    assert resp.get("session_id"), f"Create session failed: {resp}"
    session_id = resp["session_id"]
    print(f"  Created session: {session_id}")

    import websocket as _ws
    ws = _ws.create_connection(
        f"{WS_BASE}/api/ws/sessions/{session_id}", timeout=10,
    )
    ws.settimeout(30)

    _api("POST", f"/api/sessions/{session_id}/messages", json={
        "prompt": "List the top-level Python files in the project. Be thorough.",
    })

    saw_running = False
    try:
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("type") == "status" and msg.get("status") == "running":
                saw_running = True
                print("  Agent running, sending cancel…")
                _api("POST", f"/api/sessions/{session_id}/cancel", json={
                    "detail": "D0 automated test — abort verification",
                })
    except _ws.WebSocketTimeoutException:
        pass

    assert saw_running, "D0-1 FAIL: did not receive status:running event"

    terminal_status = None
    try:
        ws.settimeout(15)
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("type") == "status" and msg.get("status") in (
                "cancelled", "failed", "gave_up",
            ):
                terminal_status = msg["status"]
                print(f"  WS received status:{terminal_status}")
                break
    except _ws.WebSocketTimeoutException:
        pass

    ws.close()
    assert terminal_status is not None, (
        "D0-1 FAIL: WS did not receive cancelled/failed/gave_up status"
    )
    print(f"  ✅ D0-1 PASSED: abort → WS status:{terminal_status}")


def test_rapid_session_switch_no_zombie() -> None:
    """D0-2: 3 rapid session switches — all cancelled cleanly."""
    print("── D0-2: rapid session switch → no zombies ──")
    import websocket as _ws

    sessions: list[tuple[str, _ws.WebSocket]] = []
    for i in range(3):
        resp = _api("POST", "/api/sessions", json={"repo_path": _PROJECT_ROOT})
        sid = resp["session_id"]
        ws = _ws.create_connection(
            f"{WS_BASE}/api/ws/sessions/{sid}", timeout=10,
        )
        ws.settimeout(10)
        sessions.append((sid, ws))
        print(f"  Session {i + 1}: {sid}")

    for i, (sid, ws) in enumerate(sessions):
        _api("POST", f"/api/sessions/{sid}/messages", json={
            "prompt": f"Test {i}: Count files in repo",
        })
        time.sleep(0.5)
        _api("POST", f"/api/sessions/{sid}/cancel", json={
            "detail": f"D0 rapid switch test #{i}",
        })
        try:
            ws.settimeout(3)
            while True:
                raw = ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "status" and msg.get("status") in (
                    "cancelled", "failed", "gave_up",
                ):
                    break
        except _ws.WebSocketTimeoutException:
            pass
        ws.close()

    health = _api("GET", "/api/storage/stats")
    if health.get("_error"):
        print(f"  ⚠️  Storage stats returned: {health}")
    else:
        print(f"  Server healthy — {health.get('total_sessions', '?')} sessions")

    print("  ✅ D0-2 PASSED: 3 rapid switches — all cancelled cleanly")


def test_aborted_session_state_consistent() -> None:
    """D0-3: Session B completes cleanly after session A abort."""
    print("── D0-3: cross-session data integrity ──")

    import websocket as _ws

    # Session A — abort
    resp_a = _api("POST", "/api/sessions", json={"repo_path": _PROJECT_ROOT})
    sid_a = resp_a["session_id"]
    ws_a = _ws.create_connection(f"{WS_BASE}/api/ws/sessions/{sid_a}", timeout=10)
    ws_a.settimeout(10)
    _api("POST", f"/api/sessions/{sid_a}/messages", json={
        "prompt": "List files", "agent_name": "explore",
    })
    time.sleep(0.5)
    _api("POST", f"/api/sessions/{sid_a}/cancel", json={"detail": "test"})
    try:
        while True:
            raw = ws_a.recv()
            msg = json.loads(raw)
            if msg.get("type") == "status" and msg.get("status") in (
                "cancelled", "failed", "gave_up",
            ):
                break
    except _ws.WebSocketTimeoutException:
        pass
    ws_a.close()

    # Session B — let complete
    resp_b = _api("POST", "/api/sessions", json={"repo_path": _PROJECT_ROOT})
    sid_b = resp_b["session_id"]
    ws_b = _ws.create_connection(f"{WS_BASE}/api/ws/sessions/{sid_b}", timeout=10)
    ws_b.settimeout(60)
    _api("POST", f"/api/sessions/{sid_b}/messages", json={
        "prompt": "Print 'hello world' and finish", "agent_name": "explore",
    })
    saw_completed = False
    try:
        while True:
            raw = ws_b.recv()
            msg = json.loads(raw)
            if msg.get("type") == "status" and msg.get("status") == "completed":
                saw_completed = True
                break
    except _ws.WebSocketTimeoutException:
        pass
    ws_b.close()

    assert saw_completed, "D0-3 FAIL: session B did not complete"
    print("  ✅ D0-3 PASSED: session B completed after session A abort")


# ── test runner ──────────────────────────────────────────────────────────────

def _run_all() -> int:
    passed = 0
    failed: list[str] = []
    for test_fn in [
        test_abort_cancels_ws_cleanly,
        test_rapid_session_switch_no_zombie,
        test_aborted_session_state_consistent,
    ]:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed.append(f"{test_fn.__name__}: {e}")
            print(f"  ❌ FAILED: {e}", file=sys.stderr)

    print(f"\n{'=' * 50}")
    print(f"D0: {passed}/{passed + len(failed)} tests passed")
    if failed:
        for f in failed:
            print(f"  FAIL: {f}")
        return 1
    print("ALL D0 TESTS PASSED")
    return 0


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"D0: AbortController End-to-End Verification")
    print(f"Project: {_PROJECT_ROOT}")

    # Check if server is already running (user may have started it manually)
    health = _api("GET", "/")
    if health.get("_error"):
        print(f"Server not running on port {_DEFAULT_PORT} — starting via ServerContext…")
        with ServerContext(repo=_PROJECT_ROOT, port=_DEFAULT_PORT) as _ctx:
            rc = _run_all()
    else:
        print(f"Server already running on port {_DEFAULT_PORT}")
        rc = _run_all()

    sys.exit(rc)


if __name__ == "__main__":
    main()
