"""
E2E smoke test — starts server, creates session, sends message,
verifies WS events arrive and agent completes.

Run with: python -m pytest tests/test_e2e_smoke.py -v
Requires server running on localhost:8765.
"""

import json
import time
import pytest
import requests
import websocket  # pip install websocket-client


BASE = "http://localhost:8765"
WS_BASE = "ws://localhost:8765"


@pytest.fixture(scope="module")
def session_id():
    """Create a session, yield its ID, clean up after."""
    r = requests.post(f"{BASE}/api/sessions", json={
        "agent_name": "build",
        "repo_path": ".",
        "title": "Smoke Test",
    })
    assert r.status_code == 200 or r.status_code == 201, r.text
    sid = r.json()["session_id"]
    yield sid
    # Cleanup
    try:
        requests.delete(f"{BASE}/api/sessions/{sid}")
    except Exception:
        pass


class TestSmokeE2E:
    """End-to-end: server → session → chat → WS events → completion."""

    def test_server_health(self):
        """Server should respond to root."""
        r = requests.get(f"{BASE}/")
        assert r.status_code == 200

    def test_create_session_returns_id(self, session_id):
        """Session should have 12-char hex ID."""
        assert len(session_id) == 12

    def test_chat_returns_202(self, session_id):
        """Chat endpoint should accept a message."""
        r = requests.post(f"{BASE}/api/sessions/{session_id}/messages", json={
            "prompt": "Count from 1 to 3, then respond with 'Done'.",
        })
        assert r.status_code == 202

    def test_ws_receives_events(self, session_id):
        """WebSocket should receive status events within 60s."""
        ws = websocket.create_connection(
            f"{WS_BASE}/api/ws/sessions/{session_id}",
            timeout=10,
        )
        events = []
        start = time.time()
        completed = False

        try:
            while time.time() - start < 60:
                try:
                    ws.settimeout(5)
                    raw = ws.recv()
                    ev = json.loads(raw)
                    events.append(ev)
                    if ev.get("type") == "status" and ev.get("status") == "running":
                        pass  # agent started
                    if ev.get("type") == "status" and ev.get("status") == "completed":
                        completed = True
                        break
                    if ev.get("type") == "status" and ev.get("status") == "failed":
                        pytest.fail(f"Agent failed: {ev.get('error', 'unknown')}")
                except websocket.WebSocketTimeoutException:
                    continue
        finally:
            ws.close()

        assert completed, f"Agent did not complete within 60s. Events: {len(events)}"
        assert len(events) > 0, "No WS events received"

    def test_messages_after_completion(self, session_id):
        """Session messages should be retrievable after completion."""
        time.sleep(2)  # brief wait for persistence
        r = requests.get(f"{BASE}/api/sessions/{session_id}/messages")
        assert r.status_code == 200
        msgs = r.json()
        assert len(msgs) > 0, "No messages persisted"
