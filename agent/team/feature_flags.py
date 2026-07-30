"""Explicit capability gate for experimental agent teams."""

from __future__ import annotations

from dataclasses import dataclass
import os

from config.env import bounded_float, bounded_int, env_flag

@dataclass(frozen=True)
class TeamFeatureConfig:
    enabled: bool = False
    require_user_approval: bool = True
    max_members: int = 4
    max_tasks: int = 32
    lease_ttl_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_members < 2:
            raise ValueError("an agent team requires at least two member slots")
        if self.max_tasks < 1:
            raise ValueError("max_tasks must be positive")
        if self.lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")

    @classmethod
    def from_environment(
        cls, environ: dict[str, str] | None = None, app_config: object | None = None,
    ) -> "TeamFeatureConfig":
        """Load team feature config, preferring AppConfig when available (Phase 4)."""
        source = os.environ if environ is None else environ
        enabled = env_flag(source, "GRACE_AGENT_TEAMS_ENABLED")
        # Phase 4: override from AppConfig if present
        if app_config is not None:
            rg_cfg = getattr(app_config, "resource_governance", None)
            if rg_cfg is not None:
                enabled = getattr(rg_cfg, "team_enabled", enabled)

        return cls(
            enabled=enabled,
            require_user_approval=True,
            max_members=bounded_int(
                source, "GRACE_AGENT_TEAM_MAX_MEMBERS", 4,
                minimum=2, maximum=8,
            ),
            max_tasks=bounded_int(
                source, "GRACE_AGENT_TEAM_MAX_TASKS", 32, maximum=128,
            ),
            lease_ttl_seconds=bounded_float(
                source,
                "GRACE_AGENT_TEAM_LEASE_TTL_SECONDS",
                120.0,
                minimum=10.0,
                maximum=3600.0,
            ),
        )
