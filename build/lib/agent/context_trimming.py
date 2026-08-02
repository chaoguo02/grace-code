"""agent/context_trimming.py

Context trimming pipeline — Budget → Snip → MicroCompact → Collapse.
从 agent/core.py 提取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from context.history import ConversationHistory

logger = __import__("logging").getLogger(__name__)

# Tool Result Budget constants
_TOOL_RESULT_BUDGETS: dict[str, float] = {
    "Bash": 30_000, "shell": 30_000,
    "Grep": 20_000, "grep": 20_000,
    "Glob": 100_000, "glob": 100_000,
    "WebFetch": 100_000, "web_fetch": 100_000,
    "WebSearch": 50_000, "web_search": 50_000,
    "Write": 100_000, "write": 100_000,
    "Edit": 100_000, "edit": 100_000,
    "Read": float("inf"), "read": float("inf"),
    "file_view": float("inf"),
}
_TOOL_RESULT_DEFAULT_BUDGET: float = 30_000
_TOOL_RESULT_PREVIEW_CHARS: int = 2_000
_TOOL_RESULT_AGGREGATE_MAX: int = 200_000


class _ToolResultBudgetState:
    """Stable replacement decisions for tool results (CC: ContentReplacementState)."""
    def __init__(self) -> None:
        self._decisions: dict[str, str] = {}

    def get_stub(self, key: str) -> str | None:
        return self._decisions.get(key)

    def set_stub(self, key: str, content: str) -> None:
        self._decisions[key] = content


@dataclass
class ContextTrimmingState:
    """Cross-turn state owned by the context trimming pipeline."""

    tool_budget: _ToolResultBudgetState = field(
        default_factory=_ToolResultBudgetState,
    )
    collapse_store: Any = None


@dataclass(frozen=True)
class ContextTrimResult:
    """Observable result of one pre-provider trimming pass."""

    tokens_freed: int = 0


def prepare_history_for_turn(
    history: "ConversationHistory",
    compactor: Any,
    *,
    step: int,
    enabled: bool,
    history_budget: int,
    state: ContextTrimmingState,
    planner: Any = None,
) -> ContextTrimResult:
    """Run the ordered, stateful pre-provider context reduction pipeline."""
    from context.manager import (
        ContextBudget,
        ContextPlanner,
        ContextPlanningStage,
        ContextReduction,
        ContextSnapshot,
    )
    from context.token_budget import estimate_tokens

    context_planner = planner or ContextPlanner()
    history_dicts = history.to_dicts()
    plan = context_planner.plan(
        ContextSnapshot(
            message_count=len(history_dicts),
            estimated_tokens=sum(
                estimate_tokens(str(message.get("content", "")))
                for message in history_dicts
            ),
            step=step,
        ),
        ContextBudget(
            history_tokens=history_budget,
            enabled=enabled,
        ),
        stage=ContextPlanningStage.PREPARE,
    )
    if not plan.reductions:
        return ContextTrimResult()

    tokens_freed = 0
    if plan.includes(ContextReduction.TOOL_RESULT_BUDGET):
        tokens_freed += _apply_tool_result_budget(
            history,
            budget_state=state.tool_budget,
        )
    if plan.includes(ContextReduction.SNIP):
        tokens_freed += _snip_history(history)
    if plan.includes(ContextReduction.MICRO_COMPACT):
        tokens_freed += _micro_compact(history)
    if tokens_freed > 0:
        logger.debug(
            "Pre-LLM trimming freed ~%d tokens total",
            tokens_freed,
        )

    if plan.includes(ContextReduction.COLLAPSE):
        collapse_freed, state.collapse_store = _apply_context_collapse(
            history,
            compactor,
            collapse_store=state.collapse_store,
        )
        if collapse_freed > 0:
            tokens_freed += collapse_freed
            logger.debug(
                "ContextCollapse freed ~%d tokens",
                collapse_freed,
            )
    return ContextTrimResult(tokens_freed=tokens_freed)


def _tool_result_key(msg) -> str:
    tid = getattr(msg, "tool_call_id", None)
    if tid:
        return str(tid)
    return f"{id(msg)}:{getattr(msg, 'tool_name', '')}"


def _snip_history(history: "ConversationHistory") -> int:
    """Remove low-value turns via SnipCompactor. Returns tokens freed."""
    from context.compaction import SnipCompactor
    dicts = history.to_dicts()
    snipper = SnipCompactor()
    kept = snipper.snip(dicts)
    if len(kept) == len(dicts):
        return 0
    restored = type(history).from_dicts(kept, max_messages=history._max)
    history._messages.clear()
    history._messages.extend(restored._messages)
    return snipper.tokens_freed


def _apply_tool_result_budget(
    history: "ConversationHistory",
    *,
    budget_state: _ToolResultBudgetState | None = None,
) -> int:
    """CC: applyToolResultBudget — per-tool caps + aggregate cap."""
    from context.token_budget import estimate_tokens
    candidates: list[tuple[int, str, str, float]] = []
    total_chars = 0
    freed = 0

    for msg_idx, msg in enumerate(history._messages):
        if msg.role != "tool":
            continue
        content = str(msg.content or "")
        if not content:
            continue
        tool_name = getattr(msg, "tool_name", "") or ""
        budget = _TOOL_RESULT_BUDGETS.get(tool_name, _TOOL_RESULT_DEFAULT_BUDGET)
        key = _tool_result_key(msg)

        if budget_state is not None:
            cached = budget_state.get_stub(key)
            if cached is not None:
                if cached != content:
                    freed += estimate_tokens(content) - estimate_tokens(cached)
                    msg.content = cached
                    content = cached
                total_chars += len(content)
                candidates.append((msg_idx, key, content, budget))
                continue

        if len(content) > budget:
            preview = content[:_TOOL_RESULT_PREVIEW_CHARS]
            replacement = (
                f"{preview}\n\n"
                f"[... truncated {len(content) - _TOOL_RESULT_PREVIEW_CHARS} chars. "
                f"Use a more specific query for the remaining content.]"
            )
            before = estimate_tokens(content)
            after = estimate_tokens(replacement)
            freed += max(0, before - after)
            msg.content = replacement
            if budget_state is not None:
                budget_state.set_stub(key, replacement)
            total_chars += len(replacement)
            candidates.append((msg_idx, key, replacement, budget))
            continue

        if budget_state is not None:
            budget_state.set_stub(key, content)
        total_chars += len(content)
        candidates.append((msg_idx, key, content, budget))

    if total_chars <= _TOOL_RESULT_AGGREGATE_MAX:
        return freed

    compressible = [(idx, key, c_len, content)
                    for idx, key, content, budget in candidates
                    if budget < 1e18 and len(content) > _TOOL_RESULT_PREVIEW_CHARS]
    compressible.sort(key=lambda x: -x[2])

    for idx, key, c_len, content in compressible:
        if total_chars <= _TOOL_RESULT_AGGREGATE_MAX:
            break
        msg = history._messages[idx]
        preview = content[:_TOOL_RESULT_PREVIEW_CHARS]
        replacement = (
            f"{preview}\n\n"
            f"[... truncated {c_len - _TOOL_RESULT_PREVIEW_CHARS} chars. "
            f"Aggregate tool result budget reached.]"
        )
        before = estimate_tokens(content)
        after = estimate_tokens(replacement)
        freed += max(0, before - after)
        msg.content = replacement
        if budget_state is not None:
            budget_state.set_stub(key, replacement)
        total_chars -= c_len - len(replacement)

    return freed


def _apply_context_collapse(
    history: "ConversationHistory",
    compactor: Any,
    *,
    collapse_store: Any = None,
) -> tuple[int, Any]:
    """CC: Context Collapse — read-time projection."""
    from context.collapse import ContextCollapser, CollapseStore, project_view
    from context.token_budget import estimate_tokens
    store = collapse_store or CollapseStore()
    collapser = ContextCollapser()
    dicts = history.to_dicts()
    start, end = collapser.pick_range(dicts, store)
    if end <= start:
        return 0, store
    range_msgs = dicts[start:end]
    try:
        compaction = compactor.summarize(
            range_msgs,
            max_tokens=600,
            task_context="context collapse",
        )
        if not compaction.text:
            return 0, store
    except Exception:
        logger.debug("Context collapse summarization failed", exc_info=True)
        return 0, store
    from context.collapse import CollapseEntry
    store.add(CollapseEntry(
        start=start,
        end=end,
        summary=compaction.text,
    ))
    if compaction.truncated:
        logger.warning(
            "Context collapse summary truncated for source range %s",
            compaction.source_range,
        )
    projected = project_view(dicts, store)
    before = estimate_tokens(" ".join(str(m.get("content", "")) for m in dicts))
    after = estimate_tokens(" ".join(str(m.get("content", "")) for m in projected))
    return max(0, before - after), store


def _micro_compact(history: "ConversationHistory") -> int:
    """CC: microCompact — clear old tool output content (zero API calls)."""
    from context.compaction import MicroCompactor
    from context.token_budget import estimate_tokens
    dicts = history.to_dicts()
    before = sum(estimate_tokens(str(d.get("content", ""))) for d in dicts)
    mc = MicroCompactor(keep_recent=5)
    kept = mc.compact(dicts)
    after = sum(estimate_tokens(str(d.get("content", ""))) for d in kept)
    restored = type(history).from_dicts(kept, max_messages=history._max)
    history._messages.clear()
    history._messages.extend(restored._messages)
    return max(0, before - after)
