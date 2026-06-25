"""
tests/test_prompts.py

Prompt regression tests.
"""

from __future__ import annotations


def test_analysis_prompt_keeps_evidence_within_allowed_files():
    """Read-only analysis prompts prevent citing memory or unread files as proof."""
    from agent.prompt import build_task_prompt

    prompt = build_task_prompt(
        "只阅读 agent/core.py 和 agent/event_log.py，说明 Action.thought 是否仍会写入内部日志。",
        repo_path=".",
        intent="analysis",
    )

    assert "If the user limits which files may be read, use only those files as evidence" in prompt
    assert "Do not cite memory, prior knowledge, or files you did not read in this round as proof" in prompt
    assert "If the allowed files are insufficient to prove a claim" in prompt
