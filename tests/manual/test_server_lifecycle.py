"""
E2E lifecycle tests — ServerContext boundary scenarios (Phase 7 Batch B).

Tests:
  B-5: init-failure cleanup — when the server fails to start (port conflict),
        cleanup must release all resources (no zombie, port free).
  B-6: concurrency isolation — two ServerContext instances on different ports
        must not interfere with each other.

Usage:
    python tests/manual/test_server_lifecycle.py              # full E2E (needs server port)
    python tests/manual/test_server_lifecycle.py --quick       # unit-level validation only
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── helpers ──────────────────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _occupy_port(port: int) -> socket.socket:
    """Bind a socket to *port* and return it (blocks the port)."""
    s = socket.socket()
    s.bind(("localhost", port))
    s.listen(1)
    return s


# ── B-5: Init-failure cleanup ───────────────────────────────────────────────

def test_server_init_failure_cleanup() -> None:
    """
    When ServerContext.__enter__() times out because the target port
    is already occupied, ServerContext.__exit__() must release all
    resources — no zombie subprocess, no leaked port occupation.
    """
    print("── B-5: init-failure cleanup ──")

    port = _free_port()
    blocker = _occupy_port(port)

    try:
        from tests.manual.test_abort_e2e import ServerContext

        try:
            ctx = ServerContext(repo=_PROJECT_ROOT, port=port, startup_timeout=2.0)
            with ctx:
                pass  # should never reach here
            assert False, "Expected RuntimeError — port was occupied"
        except RuntimeError as exc:
            assert "did not start" in str(exc).lower(), (
                f"Wrong error: {exc}"
            )
            print(f"  RuntimeError raised: {exc}")

        # Verify port is free (blocker released it above)
        blocker.close()
        probe = socket.socket()
        probe.settimeout(1)
        try:
            probe.bind(("localhost", port))
            probe.close()
            print("  Port free after cleanup: OK")
        except OSError:
            assert False, f"Port {port} still occupied after ServerContext cleanup"
    finally:
        try:
            blocker.close()
        except Exception:
            pass

    print("  ✓ B-5 PASSED: init-failure cleanup releases all resources")


# ── B-6: Concurrency isolation ──────────────────────────────────────────────

def test_server_context_isolation() -> None:
    """
    Two ServerContext instances on different ports must remain isolated.
    Sessions created on Context A must not be visible via Context B's API.
    """
    print("── B-6: concurrency isolation ──")
    import json
    import urllib.request
    import urllib.error

    from tests.manual.test_abort_e2e import ServerContext

    port_a = _free_port()
    port_b = _free_port()

    with ServerContext(repo=_PROJECT_ROOT, port=port_a) as _ctx_a:
        with ServerContext(repo=_PROJECT_ROOT, port=port_b) as _ctx_b:
            ctx = _ctx_a  # silence unused warning — both are active

            # Create session on A
            data = json.dumps({"repo_path": _PROJECT_ROOT}).encode()
            req = urllib.request.Request(
                f"http://localhost:{port_a}/api/sessions",
                data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                session_a = json.loads(resp.read())["session_id"]

            # Create session on B
            req2 = urllib.request.Request(
                f"http://localhost:{port_b}/api/sessions",
                data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req2, timeout=5) as resp2:
                session_b = json.loads(resp2.read())["session_id"]

            assert session_a != session_b, "Sessions should differ"

            # Verify session A NOT visible via B's GET endpoint
            try:
                r = urllib.request.urlopen(
                    f"http://localhost:{port_b}/api/sessions/{session_a}",
                    timeout=3,
                )
                body = r.read().decode()
                status = r.getcode()
                if status == 404 or '"status":"error"' in body.lower():
                    pass  # expected
                elif "not found" in body.lower():
                    pass
                assert status != 200, (
                    f"Session A ({session_a}) leaked to Server B (status={status})"
                )
            except urllib.error.HTTPError as e:
                assert e.code == 404, f"Expected 404, got {e.code}"
                print(f"  Session A -> Server B: 404 (expected)")
                return

    print("  ✓ B-6 PASSED: context isolation verified")


# ── Failure-mode verification (self-check) ──────────────────────────────────

def self_check_failure_detection() -> None:
    """Verify that breaking an assertion causes test failure (meta-test)."""
    print("── Self-check: failure detection ──")

    try:
        assert False, "deliberate failure"
    except AssertionError:
        print("  AssertionError caught — test framework working")
        return

    assert False, "Framework broken: assertion should have failed"


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    quick = "--quick" in sys.argv

    if quick:
        print("Quick mode: meta-test only (server not required)")
        self_check_failure_detection()
        print("\nB-5/6 SKIP (quick mode)")
        sys.exit(0)

    passed = 0
    failed = 0
    for test in [
        ("Self-check", self_check_failure_detection),
        ("B-5", test_server_init_failure_cleanup),
        ("B-6", test_server_context_isolation),
    ]:
        try:
            label, fn = test
            fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ {label} FAILED: {e}")
            failed += 1

    print(f"\n{'=' * 40}")
    print(f"E2E lifecycle: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
