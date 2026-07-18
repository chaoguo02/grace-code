from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from prompts.builder import (
    build_task_prompt,
    consume_prompt_usage_metadata,
    get_prompt_usage_metadata,
    reset_prompt_usage,
    set_project_dir,
    set_prompt_config,
)
from config.schema import PromptConfig, load_config
from prompts.assembler import _LangfusePromptProvider


@pytest.fixture(autouse=True)
def reset_prompt_state() -> None:
    set_prompt_config(PromptConfig())
    set_project_dir(None)
    reset_prompt_usage()
    yield
    set_prompt_config(PromptConfig())
    set_project_dir(None)
    reset_prompt_usage()


def test_load_config_parses_prompt_management() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        config_path = Path(tmp_dir) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "llm:",
                    "  provider: openai",
                    "  model: gpt-4o-mini",
                    "prompts:",
                    "  source: hybrid",
                    "  label: staging",
                    "  version: 7",
                    "  namespace: bench",
                    "  cache_ttl_seconds: 42",
                    "  langfuse:",
                    "    public_key: pk-lf-test",
                    "    secret_key: sk-lf-test",
                    "    base_url: https://cloud.langfuse.com",
                ]
            ),
            encoding="utf-8",
        )

        config = load_config(config_path)

        assert config.prompts.source == "hybrid"
        assert config.prompts.label == "staging"
        assert config.prompts.version == 7
        assert config.prompts.namespace == "bench"
        assert config.prompts.cache_ttl_seconds == 42
        assert config.prompts.langfuse.public_key == "pk-lf-test"


def test_build_task_prompt_records_local_prompt_metadata() -> None:
    set_prompt_config(PromptConfig(source="local", namespace="forge"))

    prompt = build_task_prompt("Fix the bug", ".", intent="edit")
    metadata = get_prompt_usage_metadata()

    assert "Fix the bug" in prompt
    assert metadata == [
        {
            "source": "local",
            "path": "task.md",
            "prompt_name": "forge/task",
            "namespace": "forge",
        }
    ]
    assert consume_prompt_usage_metadata() == metadata
    assert get_prompt_usage_metadata() == []


def test_project_prompt_override_is_used() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        prompt_dir = Path(tmp_dir) / ".forge-agent" / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "task.md").write_text(
            "OVERRIDE TASK\nRepo={repo_path}\nBody={description}",
            encoding="utf-8",
        )

        set_prompt_config(PromptConfig(source="local", namespace="forge"))
        set_project_dir(tmp_dir)

        prompt = build_task_prompt("Inspect override", tmp_dir, intent="edit")

        assert "OVERRIDE TASK" in prompt
        assert f"Repo={tmp_dir}" in prompt
        assert "Body=Inspect override" in prompt


def test_hybrid_prompt_source_falls_back_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("langfuse offline")

    monkeypatch.setattr(_LangfusePromptProvider, "render", _raise)
    set_prompt_config(PromptConfig(source="hybrid", namespace="forge"))

    prompt = build_task_prompt("Fallback please", ".", intent="edit")
    metadata = get_prompt_usage_metadata()

    assert "Fallback please" in prompt
    assert metadata[0]["source"] == "local"
    assert metadata[0]["prompt_name"] == "forge/task"
