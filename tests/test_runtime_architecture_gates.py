"""Mechanical boundaries for the Runtime/Hook/EventBus redesign."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_text(*roots: str) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for root in roots
        for path in (ROOT / root).rglob("*.py")
    )


def test_removed_event_bypasses_do_not_return() -> None:
    text = _python_text("agent", "server")
    assert "_persisted_event" not in text
    assert "publish_raw" not in text


def test_server_does_not_reach_through_runtime_private_state() -> None:
    text = _python_text("server")
    assert "_runtime._" not in text
    assert "runtime._store" not in text


def test_team_topology_is_not_a_runtime_capability() -> None:
    text = _python_text("agent", "server")
    assert "AgentTopology.TEAM" not in text
    assert "team_enabled" not in text
    assert "team_approved" not in text


def test_outbox_relay_is_composed_not_left_as_placeholder() -> None:
    text = (ROOT / "server/services/agent_service.py").read_text(encoding="utf-8")
    assert "self._outbox_relay = OutboxRelay(" in text
