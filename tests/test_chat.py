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
    from agent.v2 import agent_factory as af_module

    captured = {}

    class _FakeAssembly:
        def __init__(self, agent, spec=None, contract=None, agent_cfg=None):
            self.agent = agent
            self.spec = spec
            self.contract = contract
            self.agent_cfg = agent_cfg

    def fake_create(*, agent_name, backend, base_registry, root_agent_config, **kwargs):
        captured["config"] = root_agent_config
        return _FakeAssembly(agent=_DummyAgent(root_agent_config))

    monkeypatch.setattr(af_module.AgentFactory, "create", staticmethod(fake_create))

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
