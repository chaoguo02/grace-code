"""Explicit capability gate for experimental agent teams."""

from __future__ import annotations

from dataclasses import dataclass
import os


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
        cls, environ: dict[str, str] | None = None
    ) -> "TeamFeatureConfig":
        source = os.environ if environ is None else environ
        enabled = source.get("GRACE_AGENT_TEAMS_ENABLED", "").lower() in {
            "1", "true", "yes", "on",
        }

        def _int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(source.get(name, str(default)))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))

        def _float(
            name: str, default: float, minimum: float, maximum: float,
        ) -> float:
            try:
                value = float(source.get(name, str(default)))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))

        return cls(
            enabled=enabled,
            # Team activation remains approval-gated by design.  It cannot be
            # disabled through environment configuration.
            require_user_approval=True,
            max_members=_int("GRACE_AGENT_TEAM_MAX_MEMBERS", 4, 2, 8),
            max_tasks=_int("GRACE_AGENT_TEAM_MAX_TASKS", 32, 1, 128),
            lease_ttl_seconds=_float(
                "GRACE_AGENT_TEAM_LEASE_TTL_SECONDS", 120.0, 10.0, 3600.0,
            ),
        )
