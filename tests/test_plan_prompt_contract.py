from prompts.builder import get_plan_mode_injection


def test_plan_prompt_treats_runtime_as_capability_fact_source() -> None:
    prompt = get_plan_mode_injection()

    assert "Runtime-provided tool definitions are the only source of truth" in prompt
    assert "read-only\ndelegation" in prompt
    assert "`task`" in prompt
    assert "You can use:" not in prompt


def test_plan_prompt_distinguishes_delegation_from_plan_execution() -> None:
    prompt = get_plan_mode_injection()

    assert "happen NOW" in prompt
    assert "analysis-only subagents" in prompt
    assert "Wait for\ntheir results and synthesize them" in prompt
    assert "plan how the answer will be obtained after approval" not in prompt
