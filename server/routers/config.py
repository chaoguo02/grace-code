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
    # SSOT: model metadata is defined here, not in the frontend (P2-13/14).

    _MODEL_CATALOG: list[dict[str, Any]] = [
        {
            "key": "deepseek-v4-flash", "family": "Fast",
            "note": "Quick iteration and lower latency.",
        },
        {
            "key": "deepseek-v4", "family": "Balanced",
            "note": "General coding and reasoning.",
        },
        {
            "key": "gpt-5-codex", "family": "Strong",
            "note": "Best for long multi-step tasks.",
        },
    ]

    @router.get("/models")
    async def list_models(
        service=Depends(get_service),
        response: Response = None,
    ) -> list[dict[str, Any]]:
        """Return the available LLM model catalog (SSOT).

        Cache-Control: max-age=300 (5 min).  Frontend falls back to
        a built-in default list when this endpoint is unreachable.
        """
        response.headers["Cache-Control"] = "max-age=300"
        return _MODEL_CATALOG

    return router
