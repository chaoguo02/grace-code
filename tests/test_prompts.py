"""
tests/test_prompts.py

Prompt regression tests.
"""

from __future__ import annotations


def test_analysis_prompt_keeps_evidence_within_allowed_files():
    """Read-only analysis prompts prevent citing memory or unread files as proof."""
    from prompts.builder import build_task_prompt

    prompt = build_task_prompt(
        "只阅读 agent/core.py 和 agent/event_log.py，说明 Action.thought 是否仍会写入内部日志。",
        repo_path=".",
        intent="analysis",
    )

    assert "If the user limits which files may be read, use only those files as evidence" in prompt
    assert "Do not cite memory, prior knowledge, or files you did not read in this round as proof" in prompt
    assert "If the allowed files are insufficient to prove a claim" in prompt
    assert "cite recorded evidence ids" in prompt


def test_analysis_prompt_teaches_phased_broad_analysis():
    """Broad read-only analysis prompts teach phased information gathering."""
    from prompts.builder import build_task_prompt

    prompt = build_task_prompt(
        "梳理当前 tools、MCP 和 skills 的架构、主要问题和优化路线图。不要改代码。",
        repo_path=".",
        intent="analysis",
    )

    assert "## Broad Analysis Strategy" in prompt
    assert "Do not bulk-read every file in a directory by default" in prompt
    assert "submit a compact read plan" in prompt
    assert "small per-file read budget" in prompt
    assert "Prefer abstraction and wiring files before leaf implementation files" in prompt
    assert "After reading 3-5 key files, synthesize what you know before reading more" in prompt
    assert "Read additional implementation files only to verify a specific claim" in prompt
    assert "evidence_list" in prompt
