"""
Context compression pipeline — six-layer funnel from cheap to expensive.

Aligned with Claude Code's query loop compaction path:
applyToolResultBudget → snipCompact → microcompact → contextCollapse
→ autocompact → blockingCheck.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 200_000
COMPACT_MAX_OUTPUT_TOKENS = 20_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000

DEFAULT_MAX_RESULT_CHARS = 50_000
TOOL_RESULT_PREVIEW_CHARS = 2_000
MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

TOOL_RESULT_BUDGETS: dict[str, float] = {
    "shell": 30_000,
    "bash": 30_000,
    "search_text": 20_000,
    "grep": 20_000,
    "find_files": 100_000,
    "glob": 100_000,
    "web_fetch": 100_000,
    "file_edit": 100_000,
    "file_write": 100_000,
    "edit": 100_000,
    "write": 100_000,
    "file_read": math.inf,
    "file_view": math.inf,
    "read": math.inf,
    "task": math.inf,
}


@dataclass
class CompressionResult:
    """Compression pipeline output."""

    messages: list[Any]
    tokens_freed: int = 0
    layers_applied: list[str] = field(default_factory=list)
    was_compacted: bool = False
    compaction_result: Any = None


@dataclass
class AutoCompactTrackingState:
    """Cross-turn autocompact circuit-breaker state."""

    consecutive_failures: int = 0
    compacted: bool = False
    turn_counter: int = 0
    turn_id: str = ""


@dataclass
class ContentReplacementDecision:
    """Stable per-tool-result budget decision."""

    replaced: bool
    content: str


@dataclass
class ContentReplacementState:
    """Stable replacement decisions keyed by tool_use_id."""

    decisions: dict[str, ContentReplacementDecision] = field(default_factory=dict)


@dataclass
class _ToolResultCandidate:
    index: int
    key: str
    tool_name: str
    content: str
    char_count: int
    budget: float
    fresh: bool


def apply_tool_result_budget(
    messages: list[Any],
    max_chars: int | None = None,
    preview_chars: int = TOOL_RESULT_PREVIEW_CHARS,
    *,
    replacement_state: ContentReplacementState | None = None,
    tool_budgets: Mapping[str, float] | None = None,
    max_total_chars: int = MAX_TOOL_RESULTS_PER_MESSAGE_CHARS,
) -> tuple[list[Any], int]:
    """Apply per-tool and aggregate tool_result budgets with stable decisions."""
    budgets = tool_budgets or TOOL_RESULT_BUDGETS
    default_budget = float(max_chars if max_chars is not None else DEFAULT_MAX_RESULT_CHARS)
    result = list(messages)
    tokens_freed = 0
    candidates: list[_ToolResultCandidate] = []

    for index, msg in enumerate(result):
        if not _is_tool_result_message(msg):
            continue
        content = _get_tool_result_content(msg)
        if not isinstance(content, str):
            continue
        tool_name = _get_tool_result_tool_name(msg)
        key = _get_tool_result_key(msg, index=index, tool_name=tool_name)
        budget = float(budgets.get(tool_name, default_budget))
        decision = replacement_state.decisions.get(key) if replacement_state is not None else None
        if decision is not None:
            result[index] = _set_tool_result_content(msg, decision.content)
            tokens_freed += max(0, len(content) - len(decision.content)) // 4
            candidates.append(_ToolResultCandidate(
                index=index,
                key=key,
                tool_name=tool_name,
                content=decision.content,
                char_count=len(decision.content),
                budget=budget,
                fresh=False,
            ))
            continue

        if len(content) > budget:
            replacement = _make_tool_result_preview(content, preview_chars=preview_chars)
            result[index] = _set_tool_result_content(msg, replacement)
            tokens_freed += max(0, len(content) - preview_chars) // 4
            if replacement_state is not None:
                replacement_state.decisions[key] = ContentReplacementDecision(
                    replaced=True,
                    content=replacement,
                )
            candidates.append(_ToolResultCandidate(
                index=index,
                key=key,
                tool_name=tool_name,
                content=replacement,
                char_count=len(replacement),
                budget=budget,
                fresh=False,
            ))
            continue

        if replacement_state is not None:
            replacement_state.decisions[key] = ContentReplacementDecision(
                replaced=False,
                content=content,
            )
        candidates.append(_ToolResultCandidate(
            index=index,
            key=key,
            tool_name=tool_name,
            content=content,
            char_count=len(content),
            budget=budget,
            fresh=True,
        ))

    total_chars = sum(candidate.char_count for candidate in candidates)
    if total_chars <= max_total_chars:
        return result, tokens_freed

    replaceable = [candidate for candidate in candidates if candidate.fresh]
    replaceable.sort(key=lambda c: (math.isinf(c.budget), -c.char_count))

    for candidate in replaceable:
        if total_chars <= max_total_chars:
            break
        if candidate.char_count <= preview_chars:
            continue
        current_content = _get_tool_result_content(result[candidate.index])
        if not isinstance(current_content, str):
            continue
        replacement = _make_tool_result_preview(current_content, preview_chars=preview_chars)
        result[candidate.index] = _set_tool_result_content(result[candidate.index], replacement)
        freed_chars = max(0, len(current_content) - len(replacement))
        tokens_freed += freed_chars // 4
        total_chars -= freed_chars
        if replacement_state is not None:
            replacement_state.decisions[candidate.key] = ContentReplacementDecision(
                replaced=True,
                content=replacement,
            )

    return result, tokens_freed


def snip_compact(
    messages: list[Any],
    target_token_reduction: int = 10_000,
    preserve_recent_turns: int = 5,
) -> tuple[list[Any], int]:
    """Snip older middle history while preserving system and recent turns."""
    del target_token_reduction

    if len(messages) <= preserve_recent_turns * 2 + 1:
        return messages, 0

    head = messages[:1]
    tail = messages[-(preserve_recent_turns * 2):]
    snipped = messages[1:-preserve_recent_turns * 2]

    if not snipped:
        return messages, 0

    chars_freed = sum(len(str(_get_message_content(m))) for m in snipped)
    tokens_freed = chars_freed // 4
    snip_marker = {
        "role": "user",
        "content": f"[... {len(snipped)} earlier messages snipped to save "
        f"~{tokens_freed} tokens. Key context has been preserved "
        f"in the conversation below.]",
    }

    return [*head, snip_marker, *tail], tokens_freed


def microcompact(
    messages: list[Any],
    stale_turn_threshold: int = 10,
) -> tuple[list[Any], int]:
    """Clear stale read-only tool results."""
    tokens_freed = 0
    result = []
    total_turns = _count_assistant_turns(messages)

    for i, msg in enumerate(messages):
        turn_index = _get_turn_index(messages, i)
        turns_ago = total_turns - turn_index

        if (
            _is_tool_result_message(msg)
            and turns_ago > stale_turn_threshold
            and _is_readonly_tool_result(msg)
        ):
            original_len = len(str(_get_tool_result_content(msg)))
            cleared_msg = _set_tool_result_content(
                msg,
                "[content cleared by microcompact — result was from a "
                "read-only tool and is no longer recent]",
            )
            tokens_freed += original_len // 4
            result.append(cleared_msg)
        else:
            result.append(msg)

    return result, tokens_freed


async def context_collapse(
    messages: list[Any],
    collapse_threshold: int = 20,
) -> tuple[list[Any], int]:
    """Create a read-only collapsed projection of middle history."""
    if len(messages) < collapse_threshold:
        return messages, 0

    head = messages[:3]
    tail = messages[-3:]
    middle = messages[3:-3]

    if len(middle) < 4:
        return messages, 0

    tool_names = []
    for msg in middle:
        if _is_assistant_with_tool_use(msg):
            for block in _get_tool_use_blocks(msg):
                name = block.get("name", "unknown") if isinstance(block, dict) else getattr(block, "name", "unknown")
                tool_names.append(name)

    chars_freed = sum(len(str(_get_message_content(m))) for m in middle)
    tokens_freed = chars_freed // 4
    collapse_marker = {
        "role": "user",
        "content": f"[... {len(middle)} messages collapsed. "
        f"Tools used: {', '.join(tool_names[:10])}"
        f"{'...' if len(tool_names) > 10 else ''}. "
        f"~{tokens_freed} tokens freed.]",
    }

    return [*head, collapse_marker, *tail], tokens_freed


async def autocompact(
    messages: list[Any],
    *,
    call_model_for_summary: Callable | None = None,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    tracking: AutoCompactTrackingState | None = None,
) -> tuple[CompressionResult, int]:
    """Summarize the conversation when it approaches the context limit."""
    if tracking and tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        logger.warning(
            "autocompact: circuit breaker tripped after %d consecutive failures",
            tracking.consecutive_failures,
        )
        return CompressionResult(messages=messages), 0

    estimated_tokens = _estimate_tokens(messages)
    threshold = context_window - COMPACT_MAX_OUTPUT_TOKENS - 13_000

    if estimated_tokens < threshold:
        return CompressionResult(messages=messages), 0

    if call_model_for_summary is None:
        logger.warning("autocompact: no summary model provided, skipping")
        return CompressionResult(messages=messages), 0

    try:
        summary = await call_model_for_summary(messages)
        compacted_messages = _build_post_compact_messages(summary, messages)

        if tracking:
            tracking.consecutive_failures = 0
            tracking.compacted = True

        return CompressionResult(
            messages=compacted_messages,
            was_compacted=True,
            compaction_result=summary,
            layers_applied=["autocompact"],
        ), max(0, estimated_tokens - _estimate_tokens(compacted_messages))

    except Exception as exc:
        logger.error("autocompact failed: %s", exc)
        if tracking:
            tracking.consecutive_failures += 1
            if tracking.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
                logger.warning(
                    "autocompact: circuit breaker tripped after %d consecutive failures",
                    tracking.consecutive_failures,
                )
        return CompressionResult(messages=messages), 0


async def compress_messages(
    messages: list[Any],
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    call_model_for_summary: Callable | None = None,
    autocompact_tracking: AutoCompactTrackingState | None = None,
    content_replacement_state: ContentReplacementState | None = None,
    enable_budget: bool = True,
    enable_snip: bool = True,
    enable_microcompact: bool = True,
    enable_collapse: bool = True,
    enable_autocompact: bool = True,
) -> CompressionResult:
    """Run the full six-layer compression pipeline."""
    result_messages = list(messages)
    total_freed = 0
    layers_applied: list[str] = []

    if enable_budget:
        result_messages, freed = apply_tool_result_budget(
            result_messages,
            replacement_state=content_replacement_state,
        )
        total_freed += freed
        if freed > 0:
            layers_applied.append("budget")

    if enable_snip:
        result_messages, freed = snip_compact(result_messages)
        total_freed += freed
        if freed > 0:
            layers_applied.append("snip")

    if enable_microcompact:
        result_messages, freed = microcompact(result_messages)
        total_freed += freed
        if freed > 0:
            layers_applied.append("microcompact")

    if enable_collapse:
        result_messages, freed = await context_collapse(result_messages)
        total_freed += freed
        if freed > 0:
            layers_applied.append("collapse")

    if enable_autocompact:
        compact_result, freed = await autocompact(
            result_messages,
            call_model_for_summary=call_model_for_summary,
            context_window=context_window,
            tracking=autocompact_tracking,
        )
        result_messages = compact_result.messages
        total_freed += freed
        if compact_result.was_compacted:
            layers_applied.append("autocompact")

    final_tokens = _estimate_tokens(result_messages)
    hard_limit = context_window - MANUAL_COMPACT_BUFFER_TOKENS
    if final_tokens > hard_limit:
        logger.error(
            "Blocking limit: %d tokens exceeds hard limit %d. Refusing to send API request.",
            final_tokens,
            hard_limit,
        )
        return CompressionResult(
            messages=result_messages,
            tokens_freed=total_freed,
            layers_applied=[*layers_applied, "blocking_limit"],
        )

    return CompressionResult(
        messages=result_messages,
        tokens_freed=total_freed,
        layers_applied=layers_applied,
    )


def _estimate_tokens(messages: list[Any]) -> int:
    total_chars = sum(len(str(_get_message_content(m))) for m in messages)
    return total_chars // 4


def _is_tool_result_message(msg: Any) -> bool:
    if isinstance(msg, dict):
        content = msg.get("content", [])
        if isinstance(content, list):
            return any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            )
    return False


def _is_assistant_with_tool_use(msg: Any) -> bool:
    return isinstance(msg, dict) and msg.get("role") == "assistant"


def _get_tool_use_blocks(msg: Any) -> list:
    if isinstance(msg, dict):
        content = msg.get("content", [])
        if isinstance(content, list):
            return [
                block for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
    return []


def _get_tool_result_content(msg: Any) -> Any:
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return block.get("content", "")
        return content
    return ""


def _get_tool_result_tool_name(msg: Any) -> str:
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return str(block.get("tool_name") or block.get("name") or "")
    return ""


def _get_tool_result_key(msg: Any, *, index: int, tool_name: str) -> str:
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id:
                        return str(tool_use_id)
    return f"{index}:{tool_name}"


def _make_tool_result_preview(content: str, *, preview_chars: int) -> str:
    preview = content[:preview_chars]
    return (
        f"{preview}\n\n"
        f"[... truncated {len(content) - preview_chars} chars. "
        f"Use the tool again with more specific parameters "
        f"to get the remaining content.]"
    )


def _set_tool_result_content(msg: Any, new_content: str) -> dict:
    msg_copy = dict(msg)
    if isinstance(msg_copy.get("content"), list):
        new_blocks = []
        for block in msg_copy["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                new_blocks.append({**block, "content": new_content})
            else:
                new_blocks.append(block)
        msg_copy["content"] = new_blocks
    else:
        msg_copy["content"] = new_content
    return msg_copy


def _get_message_content(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content", "")
    return getattr(msg, "content", "")


def _is_readonly_tool_result(msg: Any) -> bool:
    readonly_tools = {"read_file", "grep", "glob", "list_dir", "web_search", "read", "search"}
    if isinstance(msg, dict):
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_name = block.get("tool_name", "")
                    return tool_name in readonly_tools
    return False


def _count_assistant_turns(messages: list[Any]) -> int:
    return sum(1 for msg in messages if isinstance(msg, dict) and msg.get("role") == "assistant")


def _get_turn_index(messages: list[Any], msg_index: int) -> int:
    turn = 0
    for i in range(msg_index + 1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "assistant":
            turn += 1
    return turn


def _build_post_compact_messages(summary: str, original: list[Any]) -> list[Any]:
    system_msgs = [
        msg for msg in original
        if isinstance(msg, dict) and msg.get("role") == "system"
    ]
    compacted = {
        "role": "user",
        "content": f"[Previous conversation compacted into summary]\n\n{summary}",
    }
    return [*system_msgs, compacted]
