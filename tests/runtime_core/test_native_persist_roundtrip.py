"""Phase 5: Native 跨轮持久化 — Target 测试。

StepLoop 产出的 assistant/tool 消息写 session_messages，下一轮 HTTP
重建 conversation 时保留工具历史（tool_use_id 关联不丢）。
"""

from __future__ import annotations

import pytest

from agent.session.models import SessionMode
from app.storage.sqlite import SqliteStorageBackend
from server.services.session_service import SessionService


class _FakeSessionService:
    def __init__(self, storage):
        self._storage = storage
        self.appended = []

    def append_message(self, session_id, message):
        self.appended.append(message)
        self._storage.append_message(session_id, message)

    def get_messages(self, session_id, limit=None):
        msgs = self._storage.list_messages(session_id)
        result = [
            {
                "role": m.role,
                "content": m.content,
                **({"tool_calls": m.tool_calls} if getattr(m, "tool_calls", None) else {}),
                **({"tool_call_id": m.tool_call_id} if getattr(m, "tool_call_id", None) else {}),
            }
            for m in msgs
        ]
        return result if limit is None else result[-limit:]


def _make_outcome_with_messages():
    """模拟 StepLoop 产出的 outcome.messages（assistant + tool 配对）。"""
    from runtime_core.outcome import RuntimeOutcome, RunStatus
    from core.eventing.identifiers import RunId
    return RuntimeOutcome(
        run_id=RunId("r1"), status=RunStatus.COMPLETED,
        steps_taken=2, tokens_used=10, summary="done",
        messages=(
            {"role": "assistant", "content": "",
             "tool_calls": [
                 {"id": "c1", "name": "Read", "params": {"path": "a.py"}},
                 {"id": "c2", "name": "Grep", "params": {"pattern": "x"}},
             ]},
            {"role": "tool", "tool_call_id": "c1", "content": "file content", "is_error": False},
            {"role": "tool", "tool_call_id": "c2", "content": "match", "is_error": False},
        ),
    )


def _make_pipeline(session_service):
    from server.services.chat_pipeline import ChatPipeline, ChatPipelinePorts
    from application.coordinators.run_coordinator import RunCoordinator
    # Phase 0a: coordinator is required.  Tests only use _persist_native_messages()
    # which does not call execute(), so a dummy coordinator suffices.
    _fake_coord = RunCoordinator.__new__(RunCoordinator)
    ports = ChatPipelinePorts(
        runtime=None, session_service=session_service,
        backend=None, config={}, effective_llm_config={},
        repo_path="/tmp",
        build_confirm_callback=lambda x: lambda: None,
        reload_rules=lambda: None, loaded_rules=lambda: [],
        accumulate_session_stats=lambda s, r: None,
        compact_session_async=lambda s: None,
        coordinator=_fake_coord,
    )
    return ChatPipeline(ports)


def test_native_persist_then_reload_roundtrip(tmp_path):
    """消息写库后读回，保留 tool_calls / tool_call_id。"""
    db = str(tmp_path / "persist.db")
    storage = SqliteStorageBackend(db)
    session = storage.create_session(
        agent_name="build", mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path), title="persist",
    )
    ss = _FakeSessionService(storage)

    pipeline = _make_pipeline(ss)
    outcome = _make_outcome_with_messages()

    # 持久化
    pipeline._persist_native_messages(session.id, outcome.messages)
    assert len(ss.appended) == 3, "应持久化 3 条消息（assistant + 2 tool）"

    # 读回（下一轮 conversation 重建）
    msgs = ss.get_messages(session.id)
    assert any(m.get("tool_calls") for m in msgs), "读回必须保留 tool_calls"
    assert any(m.get("tool_call_id") for m in msgs), "读回必须保留 tool_call_id"
    tool_ids = {m["tool_call_id"] for m in msgs if m.get("tool_call_id")}
    assert tool_ids == {"c1", "c2"}, f"tool_use_id 关联必须保真，got {tool_ids}"


def test_persist_skips_user_and_non_tool(tmp_path):
    """只持久化 assistant/tool 消息（user 由 submit 单独写库）。"""
    db = str(tmp_path / "persist2.db")
    storage = SqliteStorageBackend(db)
    session = storage.create_session(
        agent_name="build", mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path), title="persist2",
    )
    ss = _FakeSessionService(storage)
    pipeline = _make_pipeline(ss)

    pipeline._persist_native_messages(session.id, (
        {"role": "user", "content": "skip me"},
        {"role": "assistant", "content": "keep"},
    ))
    assert len(ss.appended) == 1, "user 消息不应被持久化（submit 已写）"
    assert ss.appended[0].role == "assistant"


def test_persist_empty_is_noop(tmp_path):
    db = str(tmp_path / "persist3.db")
    storage = SqliteStorageBackend(db)
    session = storage.create_session(
        agent_name="build", mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path), title="persist3",
    )
    ss = _FakeSessionService(storage)
    pipeline = _make_pipeline(ss)
    pipeline._persist_native_messages(session.id, ())
    assert ss.appended == []
