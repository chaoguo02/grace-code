"""
tests/test_chat.py

Chat session regression tests.
"""

from __future__ import annotations


class _DummyBackend:
    model_name = "dummy-model"


class _DummyRenderer:
    mode = "react"

    def stream_text(self, token: str) -> None:
        pass

    def stream_thought(self, token: str) -> None:
        raise AssertionError("thought stream should not be wired in chat mode")


class _DummyAgent:
    def __init__(self, config):
        self.config = config


def test_chat_session_does_not_expose_thought_stream(monkeypatch, tmp_path):
    """ChatSession keeps Action.thought internal instead of streaming it to UI."""
    from config.schema import AppConfig
    import entry.chat as chat_module

    captured = {}

    def fake_create_agent(mode, backend, registry, agent_config, **kwargs):
        captured["config"] = agent_config
        return _DummyAgent(agent_config)

    monkeypatch.setattr(chat_module, "create_agent", fake_create_agent)

    session = chat_module.ChatSession(
        backend=_DummyBackend(),
        registry=object(),
        config=AppConfig(),
        repo_path=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
        renderer=_DummyRenderer(),
    )

    assert session.agent.config.stream is True
    assert session.agent.config.stream_callback is not None
    assert session.agent.config.thought_callback is None
    assert captured["config"].thought_callback is None
