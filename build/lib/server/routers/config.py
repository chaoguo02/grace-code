"""
Config router — exposes runtime configuration to the frontend.

Mounted under ``/api/config``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi import Response

logger = logging.getLogger(__name__)


def create_config_router(get_service: Any) -> APIRouter:
    """Create the config router with dependency injection.

    Args:
        get_service: FastAPI dependency callable returning AgentService.

    Returns:
        APIRouter configured with config endpoints.
    """
    router = APIRouter(prefix="/api/config", tags=["config"])

    # ── GET /api/config/agents ───────────────────────────────────────────

    @router.get("/agents")
    async def list_agents(
        service=Depends(get_service),
    ) -> list[dict[str, Any]]:
        """
        List available primary agent modes.

        Returns the agent definitions that can be selected via the mode
        switcher in the frontend composer.

        **Response (200):**
        Array of agent definitions, each with:
        - ``name`` (string): Agent name (e.g. ``"build"``, ``"plan"``).
        - ``description`` (string): Human-readable description.
        - ``intent`` (string): ``"edit"`` or ``"analysis"``.
        - ``tools`` (list[str]): Canonical tool names available.
        - ``max_turns`` (int): Maximum ReAct steps.

        **Errors:**
        - 500: Agent registry not available.
        """
        try:
            agents = service._agent_registry.list_primary_agents()
            return [
                {
                    "name": a.name,
                    "description": a.description,
                    "intent": a.intent.value if hasattr(a.intent, "value") else str(a.intent),
                    "tools": sorted(a.tools),
                    "max_turns": a.max_turns,
                }
                for a in agents
            ]
        except Exception as exc:
            logger.exception("Failed to list agents")
            return []

    # ── GET /api/config/models ──────────────────────────────────────────────
    # Model catalog built from the active config, not hardcoded.

    # Known alternatives per provider — used as suggestions beyond the active model.
    _PROVIDER_ALTERNATIVES: dict[str, list[dict[str, Any]]] = {
        "deepseek": [
            {"key": "deepseek-v4", "family": "Balanced",
             "note": "General coding and reasoning."},
        ],
        "openai": [
            {"key": "gpt-4o", "family": "Balanced",
             "note": "General coding and reasoning."},
            {"key": "gpt-4o-mini", "family": "Fast",
             "note": "Quick iteration and lower latency."},
        ],
        "anthropic": [
            {"key": "claude-sonnet-5", "family": "Balanced",
             "note": "General coding and reasoning."},
            {"key": "claude-haiku-4-5", "family": "Fast",
             "note": "Quick iteration and lower latency."},
        ],
    }

    @router.get("/models")
    async def list_models(
        service=Depends(get_service),
        response: Response = None,
    ) -> list[dict[str, Any]]:
        """Return the LLM model catalog — active model + provider alternatives.

        Cache-Control: max-age=300 (5 min).  Frontend falls back to
        a built-in default list when this endpoint is unreachable.
        """
        response.headers["Cache-Control"] = "max-age=300"
        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        # 1. Active model from config
        _llm_cfg = getattr(service, "_config", None)
        active_model = getattr(_llm_cfg.llm, "model", "") if _llm_cfg else ""
        active_provider = getattr(_llm_cfg.llm, "provider", "").lower() if _llm_cfg else ""
        if active_model:
            result.append({
                "key": active_model, "family": "Active",
                "note": "Currently configured model.",
            })
            seen.add(active_model)

        # 2. Provider-specific alternatives
        for alt in _PROVIDER_ALTERNATIVES.get(active_provider, []):
            if alt["key"] not in seen:
                result.append(dict(alt))
                seen.add(alt["key"])

        # 3. Fallback: if nothing was added, return minimal defaults
        if not result:
            result = [
                {"key": "deepseek-v4", "family": "Balanced",
                 "note": "General coding and reasoning."},
            ]

        return result

    # ── GET /api/config/defaults ──────────────────────────────────────────

    @router.get("/defaults")
    async def get_defaults(
        service=Depends(get_service),
        response: Response = None,
    ) -> dict[str, Any]:
        """Return default config values for new sessions."""
        response.headers["Cache-Control"] = "max-age=300"
        _cfg = getattr(service, "_config", None)
        return {
            "default_agent": getattr(_cfg.agent, "default_agent", "build") if _cfg else "build",
        }

    return router
