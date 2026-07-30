"""core/token_estimator.py

Token 估算工具 — 子代理执行前估算所需 token 预算。

保留给规划、诊断和 UI 预估使用。运行时 Token 上限由
TaskContract/ExecutionBudget 单路控制；ResourceGovernor 不再根据该估算
执行第二次预算准入。
"""

from __future__ import annotations

_CHARS_PER_TOKEN_ENGLISH = 3.5


def estimate_tokens(text: str) -> int:
    """Rough token count estimate from text length.

    Uses a conservative heuristic: assumes mixed content, ~3 chars/token.
    For exact counts, use the backend's token counter.
    """
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN_ENGLISH))

