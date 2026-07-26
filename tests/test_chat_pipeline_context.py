from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from server.services.chat_pipeline import (
    ChatPipeline,
    ChatPipelinePorts,
    ChatRequest,
    PreparedChatRun,
)
from server.services.session_service import SessionService
from hooks.events import HookEvent
from hooks.protocol import DispatchResult, HookControl


class _MetadataStorage:
    def __init__(self) -> None:
        self.record = SimpleNamespace(metadata={})
        self.updates: list[tuple[str, dict]] = []

    def get_session(self, session_id: str):
        return self.record if session_id == "session-1" else None

    def update_metadata(self, session_id: str, metadata: dict) -> bool:
        self.updates.append((session_id, dict(metadata)))
        self.record.metadata = dict(metadata)
        return True


def _pipeline_ports(**overrides) -> ChatPipelinePorts:
    values = {
        "runtime": SimpleNamespace(hook_dispatcher=None),
        "session_service": SimpleNamespace(),
        "backend": None,
        "config": SimpleNamespace(),
        "effective_llm_config": {},
        "repo_path": ".",
        "build_confirm_callback": lambda _session_id: None,
        "reload_rules": lambda: None,
        "loaded_rules": lambda: [],
        "accumulate_session_stats": lambda _session_id, _result: None,
        "compact_session_async": lambda _session_id: None,
    }
    values.update(overrides)
    return ChatPipelinePorts(**values)


def test_session_service_claims_changed_summary_once(tmp_path):
    summary_dir = tmp_path / ".grace"
    summary_dir.mkdir()
    (summary_dir / "session_summary.md").write_text(
        "# Session Summary\n\nImportant context\n\nMore detail", encoding="utf-8",
    )
    storage = _MetadataStorage()
    service = SessionService(storage)

    first = service.claim_session_context("session-1", str(tmp_path))
    second = service.claim_session_context("session-1", str(tmp_path))

    assert first == (
        "[PREVIOUS SESSION CONTEXT]\n"
        "# Session Summary\n\nImportant context\n\nMore detail"
    )
    assert second is None
    assert len(storage.updates) == 1
    assert storage.updates[0][0] == "session-1"
    assert storage.updates[0][1]["session_context_hash"]


def test_chat_request_is_immutable():
    request = ChatRequest(session_id="s", prompt="hello")

    with pytest.raises(FrozenInstanceError):
        request.prompt = "changed"


def test_mention_resolution_returns_value_without_mutating_request(tmp_path):
    (tmp_path / "note.txt").write_text("hello from file", encoding="utf-8")
    request = ChatRequest(
        session_id="s",
        prompt="Review @note.txt",
        repo_path=str(tmp_path),
    )
    pipeline = ChatPipeline(_pipeline_ports())

    resolved = pipeline.resolve_mentions(request)

    assert "[FILE: note.txt" in resolved
    assert "hello from file" in resolved
    assert request.prompt == "Review @note.txt"


def test_user_prompt_submit_hook_is_blockable():
    class _Dispatcher:
        def dispatch(self, event, context):
            assert event is HookEvent.USER_PROMPT_SUBMIT
            return DispatchResult(
                control=HookControl.BLOCK,
                reason="prompt rejected",
            )

    ports = _pipeline_ports(
        runtime=SimpleNamespace(hook_dispatcher=_Dispatcher()),
    )
    pipeline = ChatPipeline(ports)

    with pytest.raises(PermissionError, match="prompt rejected"):
        pipeline.submit_user_prompt(ChatRequest(session_id="s", prompt="no"))


def test_user_prompt_context_is_typed_until_render_boundary():
    class _Dispatcher:
        def dispatch(self, event, context):
            return DispatchResult(additional_context="project-specific context")

    ports = _pipeline_ports(
        runtime=SimpleNamespace(hook_dispatcher=_Dispatcher()),
    )
    pipeline = ChatPipeline(ports)
    submitted = pipeline.submit_user_prompt(
        ChatRequest(session_id="s", prompt="hello"),
    )

    assert submitted.text == "hello"
    assert submitted.attachments[0].text == "project-specific context"


def test_background_pipeline_dispatches_prompt_hook_before_mentions(
    monkeypatch,
):
    events: list[str] = []

    class _Dispatcher:
        def dispatch(self, event, context):
            events.append(event.value)
            return DispatchResult()

    class _ImmediateThread:
        def __init__(self, *, target, daemon):
            self._target = target

        def start(self):
            self._target()

    pipeline = ChatPipeline(_pipeline_ports(
        runtime=SimpleNamespace(
            hook_dispatcher=_Dispatcher(),
            release_session=lambda _session_id: None,
            release_backend_for_session=lambda _session_id: None,
        ),
    ))

    def _resolve(request, prompt=None):
        assert events == [HookEvent.USER_PROMPT_SUBMIT.value]
        events.append("resolve_mentions")
        return prompt or request.prompt

    monkeypatch.setattr(
        "server.services.chat_pipeline.threading.Thread",
        _ImmediateThread,
    )
    monkeypatch.setattr(pipeline, "resolve_mentions", _resolve)
    monkeypatch.setattr(pipeline, "apply_model_switch", lambda request: None)
    monkeypatch.setattr(
        pipeline,
        "inject_session_context",
        lambda request: None,
    )
    monkeypatch.setattr(
        pipeline,
        "build_callbacks",
        lambda request: (None, None),
    )
    monkeypatch.setattr(
        pipeline,
        "execute",
        lambda prepared: SimpleNamespace(),
    )
    monkeypatch.setattr(pipeline, "finish", lambda request, result: None)

    pipeline.run_in_background(ChatRequest(session_id="s", prompt="hello"))

    assert events == [
        HookEvent.USER_PROMPT_SUBMIT.value,
        "resolve_mentions",
    ]


def test_skill_execution_persists_command_but_runs_rendered_instructions():
    calls = []

    class _Runtime:
        hook_dispatcher = None

        def pop_pending_effort(self, _session_id):
            return None

        def pop_pending_thinking(self, _session_id):
            return None

        def run_session(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace()

    pipeline = ChatPipeline(_pipeline_ports(runtime=_Runtime()))
    request = ChatRequest(
        session_id="s",
        prompt="[USER-INVOKED SKILL: review]\n\nInspect the diff.",
        display_prompt="/review auth",
    )

    pipeline.execute(PreparedChatRun(
        request=request,
        resolved_prompt=request.prompt,
    ))

    assert calls[0]["task_description"] == request.prompt
    assert len(calls[0]["messages"]) == 1
    assert calls[0]["messages"][0].role == "user"
    assert calls[0]["messages"][0].content == "/review auth"
