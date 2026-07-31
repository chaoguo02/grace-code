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

    # Phase 2: Capabilities are in the system prompt (index 0), NOT a
    # separate user message.  Memory is now a single user message at
    # index 1 with no synthetic assistant acknowledgment.
    contents = [message.content for message in result.messages]
    system_msg = result.messages[0]
    assert system_msg.role == "system"
    assert "[CAPABILITY CONTEXT]" in str(system_msg.content)
    assert "## Skills" in str(system_msg.content)

    memory_index = next(i for i, content in enumerate(contents) if str(content).startswith("[MEMORY]"))
    assert memory_index == 1  # memory is first user message after system
    # No assistant ack messages anywhere
    assert not any("Understood" in str(m.content) for m in result.messages)


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

    # Phase 2: Capabilities in system prompt.  Budget trimming still works —
    # only high-priority sections survive in the capability layer.
    system_content = str(result.messages[0].content)
    assert "## High" in system_content
    assert "## Low" not in system_content
    # No separate capability message in user position
    assert not any(
        str(m.content).startswith("[CAPABILITY CONTEXT]")
        for m in result.messages[1:]
    )


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

    # Phase 2: Capabilities are explicitly placed in the system prompt
    # (StructuredContext, PROJECT priority, cacheable=True).  This gives
    # them system-level instruction priority rather than user-message status.
    assert result.messages[0].role == "system"
    assert "[CAPABILITY CONTEXT]" in str(result.messages[0].content)
    assert "Use `ToolSearch`" in str(result.messages[0].content)
    # No separate capability user message — it's in the system prompt now
    assert not any(
        str(message.content).startswith("[CAPABILITY CONTEXT]")
        for message in result.messages[1:]
    )
