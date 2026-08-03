from __future__ import annotations

from types import SimpleNamespace
import threading

from core.types import PathAccess, ToolEffect, ToolMetadata
from hitl.permission_rule import PermissionRule
from server.services.agent_service import AgentService
from server.services.approval_broker import ApprovalBroker
from server.services.safety_service import SafetyService


class _Registry:
    tool_names = ("Read", "Edit", "Bash")
    metadata = {
        "Read": ToolMetadata(
            effects=frozenset({ToolEffect.READ_WORKSPACE}),
            path_access=PathAccess.READ,
        ),
        "Edit": ToolMetadata(
            effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
            path_access=PathAccess.WRITE,
            path_parameter="path",
        ),
        "Bash": ToolMetadata(
            effects=frozenset({ToolEffect.EXECUTE}),
            requires_user_interaction=True,
        ),
    }

    def metadata_for(self, name):
        return self.metadata[name]


class _SessionService:
    session = SimpleNamespace(
        id="session-1",
        parent_id=None,
        agent_name="build",
        agent_kind="primary",
        repo_path="D:/repo",
    )

    def get_session(self, session_id):
        return self.session if session_id == self.session.id else None


class _AgentRegistry:
    def get(self, name):
        return SimpleNamespace(permission_mode="default")


class _Runtime:
    _pending_perm_modes = {"session-1": "acceptEdits"}

    def get_approval_broker(self, session_id):
        return None


class _Storage:
    events = [
        {
            "type": "approval_required",
            "request_id": "req-new",
            "tool_name": "Edit",
            "params": {"path": "src/app.py"},
            "decision_reason": "Matched ask rule",
            "sequence": 1,
        },
        {
            "type": "approval_resolved",
            "request_id": "req-new",
            "tool_name": "Edit",
            "decision": "allow_once",
            "wait_ms": 125,
            "sequence": 2,
        },
        {
            "type": "approval_required",
            "request_id": "req-old",
            "tool_name": "Bash",
            "params": {"command": "git status"},
            "sequence": 3,
        },
    ]

    def list_trace_events(self, session_id, *, limit):
        return self.events


def _service():
    return SafetyService(SimpleNamespace(
        _loaded_rules=[
            PermissionRule.parse("Edit(src/**)", "ask", source="project"),
            PermissionRule.parse("shell(git push *)", "deny", source="user"),
        ],
        _registry=_Registry(),
        session_service=_SessionService(),
        _agent_registry=_AgentRegistry(),
        _runtime=_Runtime(),
        _storage=_Storage(),
    ))


def test_safety_snapshot_explains_rules_tools_and_session_history() -> None:
    snapshot = _service().get_snapshot("session-1")

    assert [layer["order"] for layer in snapshot["layers"]] == [
        "1", "2", "3", "4", "4.5", "5", "6",
    ]
    assert snapshot["rules"][0]["tier"] == "deny"
    tools = {tool["name"]: tool for tool in snapshot["tools"]}
    assert tools["Read"]["risk"] == "low"
    assert tools["Edit"]["risk"] == "high"
    assert tools["Bash"]["control"] == "always_interactive"
    assert snapshot["session"]["effective_next_mode"] == "acceptEdits"
    assert snapshot["session"]["approval_summary"]["allowed"] == 1
    assert snapshot["session"]["approval_summary"]["response_not_recorded"] == 1
    assert snapshot["session"]["approvals"][1]["target"] == "path: src/app.py"


def test_missing_session_is_rejected() -> None:
    try:
        _service().get_snapshot("missing")
    except ValueError as exc:
        assert "Unknown session" in str(exc)
    else:
        raise AssertionError("unknown sessions must be rejected")


def test_web_approval_callback_persists_resolved_decision() -> None:
    broker = ApprovalBroker("session-1")
    required = threading.Event()
    published = []

    class _EventBus:
        def publish_typed(self, session_id, event):
            published.append(event.to_dict())
            if event.to_dict()["type"] == "approval_required":
                required.set()

    fake_service = SimpleNamespace(
        _runtime=SimpleNamespace(
            ensure_approval_broker=lambda session_id: broker,
        ),
        _event_bus=_EventBus(),
    )
    callback = AgentService._build_web_confirm_callback(
        fake_service,
        "session-1",
    )
    result = []

    thread = threading.Thread(
        target=lambda: result.append(callback(SimpleNamespace(
            tool_name="Edit",
            params={"path": "src/app.py"},
            thought="update",
            decision_reason="ask rule",
            tool_use_id="tool-1",
        ))),
    )
    thread.start()
    assert required.wait(timeout=2)
    request = next(
        event for event in published
        if event["type"] == "approval_required"
    )
    from hitl.pipeline import PromptAction, PromptDecision
    assert broker.resolve(
        request["request_id"],
        PromptDecision(action=PromptAction.ALLOW_ONCE),
    )
    thread.join(timeout=2)

    resolved = next(
        event for event in published
        if event["type"] == "approval_resolved"
    )
    assert result[0].action is PromptAction.ALLOW_ONCE
    assert resolved["request_id"] == request["request_id"]
    assert resolved["decision"] == "allow_once"
    assert resolved["wait_ms"] >= 0
