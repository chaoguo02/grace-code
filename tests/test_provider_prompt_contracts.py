from llm.anthropic_backend import _merge_anthropic_system
from llm.base import LLMToolSchema
from llm.openai_backend import _build_tool_description_for_text


def test_anthropic_merges_all_plain_system_messages() -> None:
    assert _merge_anthropic_system(["core", "runtime", "skills"]) == (
        "core\n\nruntime\n\nskills"
    )


def test_anthropic_preserves_structured_system_blocks_when_merging() -> None:
    cached = {
        "type": "text",
        "text": "cached core",
        "cache_control": {"type": "ephemeral"},
    }

    merged = _merge_anthropic_system([[cached], "runtime"])

    assert merged == [
        cached,
        {"type": "text", "text": "runtime"},
    ]


def test_text_fallback_includes_full_visible_tool_contract() -> None:
    prompt = _build_tool_description_for_text([
        LLMToolSchema(
            name="Read",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            prompt_contract=("Read the file before editing it.",),
        ),
        LLMToolSchema(
            name="mcp__hidden__lookup",
            description="Deferred lookup",
            parameters={"type": "object"},
            deferred=True,
        ),
    ])

    assert '"required": ["path"]' in prompt
    assert "Read the file before editing it." in prompt
    assert "mcp__hidden__lookup" not in prompt
