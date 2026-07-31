"""Progressive disclosure: assign description fidelity tiers to tool schemas.

Phase 2 #5: Frequency/recency-based selection as a v1 approximation of
CC's semantic routing.  Converges to task-intent-based routing when
task classification matures.

Design: TOOL_SYSTEM_NORMALIZATION_DESIGN.md, Section 4.2 #5.
"""

from __future__ import annotations

from typing import Any

from core.types import ToolDescriptionTier

# ── Constants ───────────────────────────────────────────────────────────

DEFAULT_RECENCY_WINDOW = 5    # tools called in last N turns get FULL
DEFAULT_FREQUENCY_TOP = 5     # top N most-called tools get FULL
RESERVE_FOR_RESPONSE = 2048   # tokens reserved for model response


# ── Public API ──────────────────────────────────────────────────────────


def compute_tool_tiers(
    schemas: list[Any],
    *,
    call_history: list[str] | None = None,
    available_context_tokens: int = 128_000,
    conversation_tokens: int = 0,
    system_prompt_tokens: int = 0,
) -> list[Any]:
    """Assign description tiers to tool schemas, returning modified list.

    The original schemas are NOT mutated — a new list is returned with
    ``tier`` set on each schema (or copies if schemas are immutable).

    Args:
        schemas: List of LLMToolSchema objects to tier.
        call_history: Tool names called in this session, most recent last.
        available_context_tokens: Model's max context window.
        conversation_tokens: Estimated tokens in conversation history.
        system_prompt_tokens: Estimated tokens in system prompt.
    """
    if not schemas:
        return list(schemas)

    history = list(call_history or [])
    budget = _compute_budget(
        available_context_tokens,
        conversation_tokens,
        system_prompt_tokens,
    )

    # Build frequency counts from call history
    freq: dict[str, int] = {}
    for name in history:
        freq[name] = freq.get(name, 0) + 1

    # Recent calls (last N turns)
    recent = set(history[-DEFAULT_RECENCY_WINDOW:] if history else [])

    # Top N by frequency
    top_frequent = set(
        name for name, _ in
        sorted(freq.items(), key=lambda x: -x[1])[:DEFAULT_FREQUENCY_TOP]
    )

    # Assign tiers
    result = []
    full_tokens = 0
    for schema in schemas:
        name = getattr(schema, "name", "")
        if name in recent or name in top_frequent:
            tier = ToolDescriptionTier.FULL
        else:
            tier = ToolDescriptionTier.SUMMARY

        # Estimate contribution to description token block
        desc = getattr(schema, "description", "")
        params = getattr(schema, "parameters", {})
        import json
        params_json = json.dumps(params, ensure_ascii=True, separators=(",", ":"))
        from context.token_budget import estimate_tokens
        schema_tokens = estimate_tokens(f"{name}: {desc}\n{params_json}")
        full_tokens += schema_tokens

        result.append(_set_tier(schema, tier))

    # Budget check: if FULL + SUMMARY exceeds budget, degrade lowest
    # frequency tools to SCHEMA_ONLY
    if full_tokens > budget:
        # Sort by frequency (ascending — least-called first), then alphabetically
        by_frequency: list[tuple[int, str, int]] = [
            (freq.get(getattr(s, "name", ""), 0), getattr(s, "name", ""), i)
            for i, s in enumerate(result)
            if getattr(s, "tier", ToolDescriptionTier.FULL) is ToolDescriptionTier.SUMMARY
        ]
        by_frequency.sort(key=lambda x: (x[0], x[1]))

        # Degrade enough SUMMARY tools to bring total under budget
        tokens_to_free = full_tokens - budget
        for _freq_count, _name, idx in by_frequency:
            if tokens_to_free <= 0:
                break
            schema = result[idx]
            desc = getattr(schema, "description", "")
            old_tokens = estimate_tokens(desc)
            # SCHEMA_ONLY: first sentence + params (params always preserved)
            first_sentence = desc.split(".")[0] + "." if "." in desc else desc[:80]
            new_tokens = estimate_tokens(first_sentence)
            result[idx] = _set_tier(schema, ToolDescriptionTier.SCHEMA_ONLY)
            tokens_to_free -= max(0, old_tokens - new_tokens)

    return result


# ── Internal helpers ────────────────────────────────────────────────────


def _compute_budget(
    available_context_tokens: int,
    conversation_tokens: int,
    system_prompt_tokens: int,
) -> int:
    """Dynamic budget for tool descriptions = remaining context space."""
    remaining = max(
        1000,
        available_context_tokens
        - conversation_tokens
        - system_prompt_tokens
        - RESERVE_FOR_RESPONSE,
    )
    # Tool descriptions should not consume more than 15% of remaining
    return max(1000, int(remaining * 0.15))


def _set_tier(schema: Any, tier: ToolDescriptionTier) -> Any:
    """Set tier on a schema, returning a copy if immutable."""
    if hasattr(schema, "tier"):
        schema.tier = tier
        return schema
    # dataclass replace
    try:
        from dataclasses import replace
        return replace(schema, tier=tier)
    except (TypeError, ValueError):
        # last resort: monkey-patch
        schema.tier = tier
        return schema
