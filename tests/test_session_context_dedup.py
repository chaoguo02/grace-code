from __future__ import annotations

from agent.session.models import SessionMode
from agent.session.session_store import SessionStore
from llm.base import LLMMessage


def test_context_loader_collapses_legacy_adjacent_duplicate_user_prompts(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session = store.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="legacy duplicate",
    )
    store.append_message(session.id, LLMMessage(role="user", content="你好"))
    store.append_message(session.id, LLMMessage(role="user", content="你好"))
    store.append_message(
        session.id,
        LLMMessage(role="assistant", content="你好，有什么可以帮你？"),
    )
    store.append_message(session.id, LLMMessage(role="user", content="你好"))

    context = store.list_messages_for_context(session.id)

    assert [(message.role, message.content) for message in context] == [
        ("user", "你好"),
        ("assistant", "你好，有什么可以帮你？"),
        ("user", "你好"),
    ]
