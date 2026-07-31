"""Request-isolation tests for prompt rendering."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from capabilities.render import build_tool_contract_rules
from prompts.builder import PromptRenderer
from llm.base import LLMToolSchema


def test_prompt_renderers_keep_project_overrides_isolated(tmp_path) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    prompt_a = project_a / ".grace" / "prompts"
    prompt_b = project_b / ".grace" / "prompts"
    prompt_a.mkdir(parents=True)
    prompt_b.mkdir(parents=True)
    (prompt_a / "task.md").write_text(
        "PROJECT_A::{description}",
        encoding="utf-8",
    )
    (prompt_b / "task.md").write_text(
        "PROJECT_B::{description}",
        encoding="utf-8",
    )

    renderer_a = PromptRenderer(project_dir=str(project_a))
    renderer_b = PromptRenderer(project_dir=str(project_b))

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(
            renderer_a.task,
            "alpha",
            str(project_a),
        )
        future_b = pool.submit(
            renderer_b.task,
            "beta",
            str(project_b),
        )

    assert future_a.result() == "PROJECT_A::alpha"
    assert future_b.result() == "PROJECT_B::beta"


def test_tool_prompt_contract_is_declarative_not_name_based() -> None:
    custom = LLMToolSchema(
        name="CustomRunner",
        description="run custom tasks",
        parameters={"type": "object"},
        prompt_contract=("Pass every argument separately.",),
    )
    misleading_name = LLMToolSchema(
        name="shell",
        description="not actually a shell",
        parameters={"type": "object"},
    )

    rendered = build_tool_contract_rules(
        [custom, misleading_name],
    )

    assert "**CustomRunner**: Pass every argument separately." in rendered
    assert "**shell tool**" not in rendered
