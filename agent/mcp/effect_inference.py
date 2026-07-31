"""MCP tool effect inference — normalises hardcoded `is_read_only=False`.

MCP servers may declare tool effects in their tool metadata annotations.
If no annotation exists, a heuristic based on tool name and description
produces a reasonable default.  The inference result is always overrideable
via session config.
"""

from __future__ import annotations

import logging
from typing import Any

from core.types import ToolEffect

logger = logging.getLogger(__name__)

# ── Heuristic patterns ──────────────────────────────────────────────────

_READ_ONLY_NAME_PREFIXES: tuple[str, ...] = (
    "get_", "list_", "search_", "find_", "query_", "read_", "fetch_",
    "lookup_", "describe_", "show_", "view_", "check_", "validate_",
)

_READ_ONLY_NAME_PATTERNS: tuple[str, ...] = (
    "_search", "_lookup", "_query", "_find", "_read",
)

_READ_ONLY_DESCRIPTION_KEYWORDS: tuple[str, ...] = (
    "read", "search", "lookup", "find", "query", "list", "get",
    "fetch", "view", "show", "describe", "check", "validate",
    "inspect", "browse", "explore",
)

_DESTRUCTIVE_NAME_PREFIXES: tuple[str, ...] = (
    "create_", "update_", "delete_", "remove_", "write_", "set_", "put_",
    "install_", "deploy_", "run_", "execute_", "apply_", "modify_", "change_",
)

_DESTRUCTIVE_DESCRIPTION_KEYWORDS: tuple[str, ...] = (
    "create", "update", "delete", "remove", "write", "modify", "change",
    "install", "deploy", "run", "execute", "apply", "destroy", "migrate",
    "deploy", "provision", "restart",
)


# ── Public API ───────────────────────────────────────────────────────────


def infer_mcp_effects(
    tool_name: str,
    description: str,
    *,
    metadata: dict[str, Any] | None = None,
    server_name: str = "",
) -> frozenset[ToolEffect]:
    """Infer ToolEffect values for one MCP tool.

    Resolution order:
    1. Explicit ``effects`` list in MCP tool metadata (server-declared).
    2. Explicit ``read_only_hint`` boolean in metadata.
    3. Heuristic inference from tool name and description.
    4. Fallback: ``{UNKNOWN}`` with a logged warning.

    Returns:
        A non-empty frozenset of ToolEffect values.
    """
    # 1. Explicit server-declared effects
    if metadata:
        raw_effects = metadata.get("effects")
        if raw_effects is not None:
            effects = _parse_effects_list(raw_effects)
            if effects:
                return effects

        # 2. read_only_hint
        ro_hint = metadata.get("read_only_hint") or metadata.get("readOnlyHint")
        if ro_hint is True or str(ro_hint).lower() in ("true", "1", "yes"):
            return _pick_read_effect(tool_name, description)

    # 3. Heuristic
    effects = _infer_by_name(tool_name, description)
    if ToolEffect.UNKNOWN not in effects:
        return effects

    # 4. Fallback
    full_name = f"{server_name}/{tool_name}" if server_name else tool_name
    logger.warning(
        "MCP tool %s has no declared effects — defaulting to UNKNOWN. "
        "Add 'effects' or 'read_only_hint' to tool metadata.",
        full_name,
    )
    return frozenset({ToolEffect.UNKNOWN})


def infer_mcp_is_read_only(
    tool_name: str,
    description: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Return True if the MCP tool should be treated as read-only.

    Uses the same inference pipeline as ``infer_mcp_effects``, then checks
    whether ALL inferred effects fall within ``READ_ONLY_EFFECTS``.
    """
    effects = infer_mcp_effects(
        tool_name,
        description,
        metadata=metadata,
    )
    from core.policy import READ_ONLY_EFFECTS
    return effects.issubset(READ_ONLY_EFFECTS)


# ── Internal helpers ─────────────────────────────────────────────────────


def _parse_effects_list(raw: Any) -> frozenset[ToolEffect]:
    """Parse a server-declared effects list into ToolEffect values."""
    if isinstance(raw, (list, tuple, set, frozenset)):
        result: set[ToolEffect] = set()
        for item in raw:
            if isinstance(item, ToolEffect):
                result.add(item)
            elif isinstance(item, str):
                try:
                    result.add(ToolEffect(item))
                except ValueError:
                    logger.debug("Unknown ToolEffect string: %r", item)
        return frozenset(result) if result else frozenset()

    if isinstance(raw, str):
        try:
            return frozenset({ToolEffect(raw)})
        except ValueError:
            pass

    return frozenset()


def _pick_read_effect(name: str, description: str) -> frozenset[ToolEffect]:
    """When server declares read_only_hint, pick the most specific READ effect."""
    lower = name.lower()
    desc_lower = description.lower()
    if any(kw in lower for kw in ("search", "find", "query", "lookup", "grep")):
        return frozenset({ToolEffect.DISCOVER_WORKSPACE})
    if any(kw in desc_lower for kw in ("search", "find", "lookup", "grep", "list")):
        return frozenset({ToolEffect.DISCOVER_WORKSPACE})
    return frozenset({ToolEffect.READ_WORKSPACE})


def _infer_by_name(name: str, description: str) -> frozenset[ToolEffect]:
    """Heuristic effect inference from tool name and description patterns."""
    lower = name.lower()
    desc_lower = description.lower()

    # Destructive signals dominate
    destructive_score = 0
    for prefix in _DESTRUCTIVE_NAME_PREFIXES:
        if lower.startswith(prefix) or f"_{prefix}" in lower:
            destructive_score += 1
            break
    for kw in _DESTRUCTIVE_DESCRIPTION_KEYWORDS:
        if kw in desc_lower:
            destructive_score += 1
            break
    if destructive_score >= 2:
        return frozenset({ToolEffect.WRITE_WORKSPACE})

    # Read-only signals
    read_score = 0
    for prefix in _READ_ONLY_NAME_PREFIXES:
        if lower.startswith(prefix):
            read_score += 1
            break
    for pattern in _READ_ONLY_NAME_PATTERNS:
        if lower.endswith(pattern):
            read_score += 1
            break
    for kw in _READ_ONLY_DESCRIPTION_KEYWORDS:
        if kw in desc_lower:
            read_score += 1
            break
    if read_score >= 2:
        return _pick_read_effect(name, description)

    # Weak read signal — one match
    if read_score >= 1:
        return _pick_read_effect(name, description)

    # Weak destructive signal
    if destructive_score >= 1:
        return frozenset({ToolEffect.WRITE_WORKSPACE})

    return frozenset({ToolEffect.UNKNOWN})
