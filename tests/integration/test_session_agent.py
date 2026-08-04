"""Phase E: SessionAgent — CC QueryEngine, 消费 aiterate async generator.

AC:
- SessionAgent.submit_message runs one turn, stores outcome
- mutable_messages preserves history across turns (CC mutableMessages)
- TTL timeout ends the session
- registry get/add/remove works
"""

from __future__ import annotations

import asyncio

import pytest


def _make_ports(llm):
    """Build RuntimePorts with fake llm + tools."""
    from runtime_core.ports import RuntimePorts, HookGateResult

    class _Hooks:
        def check(self, event_type, hook_input, tool_name=""):
            return HookGateResult(allowed=True)
    class _Events:
        def publish(self, *a, **kw): pass
    class _Clock:
        import time as _t
        def now(self): return _t.monotonic()
        def deadline(self, s): return _t.monotonic() + s
    class _Token:
        def record(self, *a, **kw): pass
    class _Tools:
        async def aexecute(self, name, params, invocation_id=""):
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=name)

    return RuntimePorts(
        llm=llm, tools=_Tools(), hooks=_Hooks(),
        live_events=_Events(), clock=_Clock(), token_usage=_Token(),
    )


async def test_session_agent_single_turn():
    """submit_message 跑一轮, outcome 产生, mutable_messages 有历史。"""
    from server.services.session_agent import SessionAgent
    from runtime_core.model_actions import AssistantText

    class _LLM:
        async def ainvoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="hello back", stop_reason="end_turn")

    agent = SessionAgent("s1", _make_ports(_LLM()))
    await agent.start()
    await agent.submit_message("hello")
    assert agent._latest_outcome is not None
    assert any("hello" in str(m) for m in agent._mutable_messages)


async def test_session_agent_preserves_history():
    """两轮 → mutable_messages 含完整历史 (CC mutableMessages)。"""
    from server.services.session_agent import SessionAgent
    from runtime_core.model_actions import AssistantText

    class _LLM:
        async def ainvoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="ok", stop_reason="end_turn")

    agent = SessionAgent("s1", _make_ports(_LLM()))
    await agent.start()
    await agent.submit_message("first")
    first_len = len(agent._mutable_messages)
    await agent.submit_message("second")
    assert len(agent._mutable_messages) > first_len
    assert any("first" in str(m) for m in agent._mutable_messages)
    assert any("second" in str(m) for m in agent._mutable_messages)


async def test_session_agent_ttl_timeout():
    """TTL 内无输入 → 会话结束。"""
    from server.services.session_agent import SessionAgent
    from runtime_core.model_actions import AssistantText

    class _LLM:
        async def ainvoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="ok", stop_reason="end_turn")

    agent = SessionAgent("s1", _make_ports(_LLM()))
    agent.TTL_SECONDS = 0.1
    await agent.start()
    await asyncio.sleep(0.3)
    assert not agent.is_alive  # TTL 超时退出


def test_registry_get_add_remove():
    """registry 增删查。"""
    from server.services.session_agent import SessionAgentRegistry, SessionAgent

    class _StubAgent:
        session_id = "s1"

    reg = SessionAgentRegistry()
    agent = _StubAgent()
    reg.add(agent)
    assert reg.get("s1") is agent
    reg.remove("s1")
    assert reg.get("s1") is None
