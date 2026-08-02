"""
G33: Context Assembler — builds immutable ContextSnapshot for Runtime.

Responsibilities: token budget, summarization, RAG, tool result truncation.
Runtime receives only an immutable snapshot — no live DB access.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Immutable snapshot of conversation context for one turn."""
    messages: tuple[dict, ...] = ()
    system_prompt: str = ""
    project_instructions: str = ""
    token_budget: int = 200_000
    estimated_tokens: int = 0


class ContextAssembler:
    """Builds ContextSnapshot from session state.  Runtime never queries DB."""

    MAX_TOKENS = 200_000

    def __init__(self, budget: int = MAX_TOKENS) -> None:
        self._budget = budget

    def assemble(self, messages: list[dict], system_prompt: str = "",
                 project_instructions: str = "") -> ContextSnapshot:
        """Build immutable snapshot from conversation history."""
        truncated = self._truncate_to_budget(messages, self._budget)
        return ContextSnapshot(
            messages=tuple(truncated),
            system_prompt=system_prompt,
            project_instructions=project_instructions,
            token_budget=self._budget,
            estimated_tokens=self._estimate_tokens(truncated),
        )

    def child_context(self, task_description: str,
                      budget: int = 50_000) -> ContextSnapshot:
        """Fresh context for child tasks — no parent history."""
        return ContextSnapshot(
            messages=({"role": "system", "content": task_description},),
            token_budget=budget,
            estimated_tokens=len(task_description) // 4,
        )

    def _truncate_to_budget(self, messages: list[dict],
                            budget: int) -> list[dict]:
        """Simple budget truncation (keep most recent messages)."""
        est = 0
        kept = []
        for m in reversed(messages):
            msg_tokens = len(str(m.get("content", ""))) // 4 + 10
            if est + msg_tokens > budget:
                break
            est += msg_tokens
            kept.append(m)
        return list(reversed(kept))

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        return sum(len(str(m.get("content", ""))) // 4 + 10 for m in messages)
