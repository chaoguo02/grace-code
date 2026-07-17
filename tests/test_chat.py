"""
tests/test_chat.py

Chat session regression tests.
"""

from __future__ import annotations

import pytest

from agent.session.agent_factory import AgentFactory as _AF; resolve_task_intent = _AF.resolve_task_intent
from core.base import ToolRegistry
from agent.task import TaskIntent


class _DummyBackend:
    model_name = "dummy-model"


@pytest.mark.parametrize(
    ("mode", "expected"),
    (("v2-build", TaskIntent.EDIT), ("v2-plan", TaskIntent.ANALYSIS)),
)
def test_task_intent_is_declared_by_mode(mode, expected):
    assert resolve_task_intent(mode) is expected


def test_explicit_task_intent_overrides_mode():
    assert resolve_task_intent("v2-build", "analysis") is TaskIntent.ANALYSIS


def test_unknown_mode_has_no_guessed_intent():
    with pytest.raises(ValueError, match="No default task intent"):
        resolve_task_intent("auto")


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
    from agent.session import agent_factory as af_module

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


def test_chat_skill_fork_routes_through_session_runtime(monkeypatch, tmp_path):
    from config.schema import AppConfig
    import entry.chat as chat_module
    from agent.session import agent_factory as af_module
    from agent.session import runtime as runtime_module
    from agent.session.models import AgentRunResult, AgentRunStatus, ExecutionPlacement
    from executor.state_paths import STATE_HOME_ENV

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))

    class _FakeAssembly:
        def __init__(self, agent, spec=None, contract=None, agent_cfg=None):
            self.agent = agent
            self.spec = spec
            self.contract = contract
            self.agent_cfg = agent_cfg

    def fake_create(*, root_agent_config, **kwargs):
        return _FakeAssembly(agent=_DummyAgent(root_agent_config))

    monkeypatch.setattr(af_module.AgentFactory, "create", staticmethod(fake_create))

    captured: dict[str, object] = {}

    def fake_spawn(
        self,
        *,
        parent_session_id,
        request,
        budget_tokens,
        parent_max_steps,
        cancellation_token,
        parent_policy,
        origin,
        spawn_context=None,
    ):
        captured["parent_session_id"] = parent_session_id
        captured["request"] = request
        captured["budget_tokens"] = budget_tokens
        captured["parent_max_steps"] = parent_max_steps
        captured["parent_policy"] = parent_policy
        captured["origin"] = origin
        return AgentRunResult(
            agent_name=request.definition.name,
            session_id="child-1",
            status=AgentRunStatus.COMPLETED,
            summary="forked summary",
        )

    monkeypatch.setattr(runtime_module.SessionRuntime, "spawn_agent", fake_spawn)

    class _Meta:
        user_can_invoke = True
        context = "fork"
        agent = "explore"
        effort = ""
        model = ""

    class _SkillRegistry:
        def __init__(self):
            self.runtime = None

        def has_skill(self, name):
            return name == "inspect"

        def get_skill_meta(self, name):
            return _Meta()

        def load_and_render(self, name, args, *, runtime=None):
            self.runtime = runtime
            return "Inspect runtime isolation"

        def list_skills(self):
            return []

        def format_for_prompt(self):
            return ""

    session = chat_module.ChatSession(
        backend=_DummyBackend(),
        registry=ToolRegistry(),
        config=AppConfig(),
        repo_path=str(repo),
        log_dir=str(tmp_path / "logs"),
        renderer=_DummyRenderer(),
        skill_registry=_SkillRegistry(),
        runtime=object(),
    )

    assert session._handle_slash_skill("/inspect runtime") is None
    assert captured["request"].definition.name == "explore"
    assert captured["request"].execution_placement is ExecutionPlacement.FOREGROUND
    assert session._skill_registry.runtime is session._runtime
    history = session._shared_history.to_dicts()
    assert history[-1]["role"] == "assistant"
    assert "forked summary" in history[-1]["content"]


def test_chat_skill_fork_refuses_non_primary_chat_mode(monkeypatch, tmp_path):
    from config.schema import AppConfig
    import entry.chat as chat_module
    from agent.session import agent_factory as af_module
    from agent.session import runtime as runtime_module
    from executor.state_paths import STATE_HOME_ENV

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))

    class _FakeAssembly:
        def __init__(self, agent, spec=None, contract=None, agent_cfg=None):
            self.agent = agent
            self.spec = spec
            self.contract = contract
            self.agent_cfg = agent_cfg

    def fake_create(*, root_agent_config, **kwargs):
        return _FakeAssembly(agent=_DummyAgent(root_agent_config))

    monkeypatch.setattr(af_module.AgentFactory, "create", staticmethod(fake_create))

    called = {"spawn": 0}

    def fake_spawn(self, **kwargs):
        called["spawn"] += 1
        raise AssertionError("spawn_agent should not be called")

    monkeypatch.setattr(runtime_module.SessionRuntime, "spawn_agent", fake_spawn)

    class _Meta:
        user_can_invoke = True
        context = "fork"
        agent = "explore"
        effort = ""
        model = ""

    class _SkillRegistry:
        def __init__(self):
            self.runtime = None

        def has_skill(self, name):
            return name == "inspect"

        def get_skill_meta(self, name):
            return _Meta()

        def load_and_render(self, name, args, *, runtime=None):
            self.runtime = runtime
            return "Inspect runtime isolation"

        def list_skills(self):
            return []

        def format_for_prompt(self):
            return ""

    session = chat_module.ChatSession(
        backend=_DummyBackend(),
        registry=ToolRegistry(),
        config=AppConfig(),
        repo_path=str(repo),
        log_dir=str(tmp_path / "logs"),
        renderer=_DummyRenderer(),
        skill_registry=_SkillRegistry(),
        runtime=object(),
    )
    session.switch_mode("explore")

    assert session._handle_slash_skill("/inspect runtime") is None
    assert called["spawn"] == 0
    assert session._skill_registry.runtime is session._runtime
