"""Release configuration for durable Workbench Multi-Agent mode."""

from __future__ import annotations

from dataclasses import dataclass
import os

from config.env import bounded_int, env_flag

@dataclass(frozen=True)
class MultiAgentFeatureConfig:
    """Independent gate and bounded scheduler limits for AgentBatch runs."""

    enabled: bool = True
    max_tasks: int = 32
    max_wave_fanout: int = 3
    max_concurrent: int = 4

    @classmethod
    def from_environment(
        cls, environ: dict[str, str] | None = None, governor: object | None = None,
    ) -> "MultiAgentFeatureConfig":
        """Load config from environment, optionally informed by ResourceGovernor.

        Phase 4: when *governor* is provided and not in observe mode,
        max_concurrent is read from the governor's worker.global_max config
        instead of the GRACE_MAX_CONCURRENT_SUBAGENTS env var.
        """
        source = os.environ if environ is None else environ
        enabled = env_flag(
            source, "GRACE_MULTI_AGENT_MODE_ENABLED", default=True,
        )
        max_tasks = bounded_int(
            source, "GRACE_MAX_MULTI_AGENT_TASKS", 32,
            minimum=2, maximum=128,
        )
        max_wave_fanout = min(
            max_tasks,
            bounded_int(
                source, "GRACE_MAX_FANOUT_PER_TURN", 3, maximum=32,
            ),
        )
        # Production runtimes receive the governor and use it as the sole
        # renewable-capacity owner.  The env fallback is retained only for
        # isolated/observe-mode runtimes that have no enforcing governor.
        if governor is not None and governor.mode != "observe":
            worker = getattr(
                getattr(governor, "_config", None), "worker", None,
            )
            max_concurrent = min(
                max_tasks,
                int(getattr(worker, "global_max", 4)),
            )
        else:
            env_concurrency = bounded_int(
                source, "GRACE_MAX_CONCURRENT_SUBAGENTS", 4, maximum=32,
            )
            max_concurrent = min(max_tasks, env_concurrency)
        return cls(
            enabled=enabled,
            max_tasks=max_tasks,
            max_wave_fanout=max_wave_fanout,
            max_concurrent=max_concurrent,
        )
