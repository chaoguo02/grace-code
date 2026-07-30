"""Typed, bounded environment configuration helpers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


def bounded_int(
    source: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 10_000,
) -> int:
    try:
        value = int(source.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def bounded_float(
    source: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(source.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def env_flag(
    source: Mapping[str, str],
    name: str,
    default: bool = False,
) -> bool:
    raw_default = "true" if default else "false"
    return str(source.get(name, raw_default)).strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass(frozen=True)
class SubagentSafetyLimits:
    """Lifetime/depth safety limits, distinct from renewable capacity."""

    max_spawn_per_session: int = 64
    max_spawn_depth: int = 1

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "SubagentSafetyLimits":
        source = os.environ if environ is None else environ
        return cls(
            max_spawn_per_session=bounded_int(
                source,
                "GRACE_MAX_SUBAGENTS_PER_SESSION",
                64,
                maximum=10_000,
            ),
            max_spawn_depth=bounded_int(
                source,
                "GRACE_MAX_SUBAGENT_SPAWN_DEPTH",
                1,
                maximum=5,
            ),
        )
