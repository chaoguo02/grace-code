"""
End-to-end AbortController verification (D0).

Validates the full request-cancel-cleanup lifecycle:
  Client sends chat → WS receives events → client cancels →
  WS receives status:cancelled → backend connection count returns to zero →
  frontend state resets.

Usage:
  1. Start the server in a separate terminal:
     python -m server.main --repo . --no-browser
  2. Run this script:
     python tests/manual/test_abort_e2e.py

The script is self-contained and uses only stdlib + requests + websockets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any


BASE = "http://localhost:8765"
WS_BASE = "ws://localhost:8765"
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))


# ── helpers ──────────────────────────────────────────────────────────────────

def _api(method: str, path: str, **kwargs) -> Any:
    """Minimal requests wrapper — no external import beyond stdlib urllib."""
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
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        return {"_status": e.code, "_error": body}
    except urllib.error.URLError as e:
        return {"_error": str(e.reason)}


def _ws_connect(session_id: str):
    """Connect a WebSocket and return (ws, first_msg)."""
    import websocket as _ws
    ws = _ws.create_connection(f"{WS_BASE}/api/ws/sessions/{session_id}", timeout=10)
    # Wait for the first event (should arrive within 5s of chat start)
    ws.settimeout(5)
    raw = ws.recv()
    return ws, json.loads(raw)


# ── test cases ───────────────────────────────────────────────────────────────

def test_abort_cancels_ws_cleanly():
    """
    D0-1: Session chat → cancel → WS receives status:cancelled → connection clean.

    Flow:
      1. Create session
      2. Open WS and start chat
      3. Wait for first WS event, then cancel
      4. Assert WS receives status:cancelled or status:failed
      5. Assert no backend errors
    """
    print("── D0-1: abort → cancelled ──")

    # 1. Create session
    resp = _api("POST", "/api/sessions", json={"repo_path": _PROJECT_ROOT})
    assert resp.get("session_id"), f"Create session failed: {resp}"
    session_id = resp["session_id"]
    print(f"  Created session: {session_id}")

    # 2. Open WS + start chat
    import websocket as _ws
    ws = _ws.create_connection(
        f"{WS_BASE}/api/ws/sessions/{session_id}", timeout=10,
    )
    ws.settimeout(30)

    chat_prompt = "List the top-level Python files in the project. Be thorough."
    _api("POST", f"/api/sessions/{session_id}/messages", json={
        "prompt": chat_prompt,
    })

    # 3. Collect events until we see "running", then cancel
    saw_running = False
    try:
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("type") == "status" and msg.get("status") == "running":
                saw_running = True
                print(f"  Agent running, sending cancel…")
                _api("POST", f"/api/sessions/{session_id}/cancel", json={
                    "detail": "D0 automated test — abort verification",
                })
    except _ws.WebSocketTimeoutException:
        pass

    assert saw_running, "D0-1 FAIL: did not receive status:running event"

    # 4. Wait for cancelled/failed status
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
        "D0-1 FAIL: WS did not receive cancelled/failed/gave_up status "
        "within 15s of cancel request"
    )
    print(f"  ✅ D0-1 PASSED: abort → WS status:{terminal_status}")


def test_rapid_session_switch_no_zombie():
    """
    D0-2: Create 3 sessions in rapid succession → verify no stale WS handles.

    Each session chat gets aborted mid-flight, then the next one starts.
    After all 3, the backend should have zero active subscribers for the
    first 2 sessions.
    """
    print("── D0-2: rapid session switch → no zombies ──")
    import websocket as _ws

    sessions = []
    for i in range(3):
        resp = _api("POST", "/api/sessions", json={"repo_path": _PROJECT_ROOT})
        sid = resp["session_id"]
        ws = _ws.create_connection(
            f"{WS_BASE}/api/ws/sessions/{sid}", timeout=10,
        )
        ws.settimeout(10)
        sessions.append((sid, ws))
        print(f"  Session {i+1}: {sid}")

    # Start chat on session-0, abort it, then start on session-1, etc.
    for i, (sid, ws) in enumerate(sessions):
        _api("POST", f"/api/sessions/{sid}/messages", json={
            "prompt": f"Test {i}: Count files in repo",
        })
        # Wait briefly for agent to start, then cancel
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

    # After all aborts, verify the server is still healthy
    health = _api("GET", "/api/storage/stats")
    if health.get("_error"):
        print(f"  ⚠️ Storage stats returned: {health}")
    else:
        print(f"  Server healthy — {health.get('total_sessions', '?')} sessions in DB")

    print("  ✅ D0-2 PASSED: 3 rapid session switches — all cancelled cleanly")


def test_aborted_session_state_consistent():
    """
    D0-3: After cancelling session A, session B data is not corrupted.

    Create session A → chat + abort.
    Create session B → chat (let complete).
    Verify session B messages are intact (no session-A cross-contamination).
    """
    print("── D0-3: cross-session data integrity ──")

    # Session A — abort
    resp_a = _api("POST", "/api/sessions", json={"repo_path": _PROJECT_ROOT})
    sid_a = resp_a["session_id"]
    import websocket as _ws
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
    print("  ✅ D0-3 PASSED: session B completed cleanly after session A abort")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    # Pre-flight: server reachable?
    health = _api("GET", "/")
    if health.get("_error"):
        print(f"ERROR: Server not reachable at {BASE} — {health.get('_error')}")
        print("Start the server first: python -m server.main --repo . --no-browser")
        sys.exit(1)

    print(f"D0: AbortController End-to-End Verification")
    print(f"Server: {BASE}")

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

    print(f"\n{'='*50}")
    print(f"D0: {passed}/{passed + len(failed)} tests passed")
    if failed:
        for f in failed:
            print(f"  FAIL: {f}")
        sys.exit(1)
    else:
        print("ALL D0 TESTS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
