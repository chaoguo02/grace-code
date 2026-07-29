"""Release configuration for durable Workbench Multi-Agent mode."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class MultiAgentFeatureConfig:
    """Independent gate and bounded scheduler limits for AgentBatch runs."""

    enabled: bool = True
    max_tasks: int = 32
    max_wave_fanout: int = 3
    max_concurrent: int = 4

    @classmethod
    def from_environment(
        cls, environ: dict[str, str] | None = None,
    ) -> "MultiAgentFeatureConfig":
        source = os.environ if environ is None else environ
        raw_enabled = source.get("GRACE_MULTI_AGENT_MODE_ENABLED", "true")
        enabled = str(raw_enabled).strip().lower() in {"1", "true", "yes", "on"}

        def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(source.get(name, str(default)))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))

        max_tasks = bounded("GRACE_MAX_MULTI_AGENT_TASKS", 32, 2, 128)
        return cls(
            enabled=enabled,
            max_tasks=max_tasks,
            max_wave_fanout=min(
                max_tasks,
                bounded("GRACE_MAX_FANOUT_PER_TURN", 3, 1, 32),
            ),
            max_concurrent=min(
                max_tasks,
                bounded("GRACE_MAX_CONCURRENT_SUBAGENTS", 4, 1, 32),
            ),
        )
