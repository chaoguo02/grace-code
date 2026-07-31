from __future__ import annotations

from capabilities.models import CapabilityKind, CapabilitySection
from context.history import ConversationHistory
from context.manager import ContextManager
from context.token_budget import TokenBudget
from llm.base import LLMMessage


def test_context_manager_injects_capabilities_before_long_term_context() -> None:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="hello"))
    sections = [
        CapabilitySection(
            title="Skills",
            content="## Available Skills\n- **review**: Review code",
            priority=20,
            token_estimate=10,
            kind_filter=CapabilityKind.SKILL,
        ),
    ]

    result = ContextManager().build_request_messages(
        history=history,
        token_budget=TokenBudget(total=20_000),
        system_core_text="system",
        long_term_context="[MEMORY]\nremember this",
        capability_sections=sections,
    )

    contents = [message.content for message in result.messages]
    capability_index = next(i for i, content in enumerate(contents) if str(content).startswith("[CAPABILITY CONTEXT]"))
    memory_index = next(i for i, content in enumerate(contents) if str(content).startswith("[MEMORY]"))
    assert capability_index < memory_index
    assert result.messages[capability_index + 1].role == "assistant"
    assert "available capabilities" in result.messages[capability_index + 1].content


def test_context_manager_trims_lower_priority_capability_sections() -> None:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="hello"))
    sections = [
        CapabilitySection(
            title="Low",
            content="low " * 2000,
            priority=90,
            token_estimate=10_000,
            kind_filter=CapabilityKind.AGENT,
        ),
        CapabilitySection(
            title="High",
            content="important",
            priority=1,
            token_estimate=2,
            kind_filter=CapabilityKind.MCP_SERVER,
        ),
    ]

    result = ContextManager().build_request_messages(
        history=history,
        token_budget=TokenBudget(total=8_000),
        system_core_text="system",
        capability_sections=sections,
        max_context_window=8_000,
    )

    capability_message = next(
        message.content for message in result.messages
        if str(message.content).startswith("[CAPABILITY CONTEXT]")
    )
    assert "## High" in capability_message
    assert "## Low" not in capability_message


def test_context_manager_does_not_put_capabilities_in_system_prompt() -> None:
    history = ConversationHistory()
    sections = [
        CapabilitySection(
            title="MCP Tool Discovery",
            content="Use `ToolSearch`",
            priority=10,
            token_estimate=2,
            kind_filter=CapabilityKind.MCP_TOOL,
        ),
    ]

    result = ContextManager().build_request_messages(
        history=history,
        token_budget=TokenBudget(total=20_000),
        system_core_text="system",
        capability_sections=sections,
    )

    assert result.messages[0].role == "system"
    assert "CAPABILITY CONTEXT" not in str(result.messages[0].content)
    assert any(
        str(message.content).startswith("[CAPABILITY CONTEXT]")
        for message in result.messages[1:]
    )
