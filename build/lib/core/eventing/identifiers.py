"""
P1: Core value objects — frozen, slots, validated IDs.

MUST NOT import server/agent/runtime/hooks.
All IDs are validated at construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("SessionId must not be empty")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TaskId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("TaskId must not be empty")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class RunId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("RunId must not be empty")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class EventId:
    value: uuid.UUID

    @classmethod
    def generate(cls) -> EventId:
        return cls(value=uuid.uuid4())

    def __post_init__(self) -> None:
        if self.value is None:
            raise ValueError("EventId must not be None")

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class AggregateVersion:
    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError(f"AggregateVersion must be >= 0, got {self.value}")

    def next(self) -> AggregateVersion:
        return AggregateVersion(value=self.value + 1)

    def __str__(self) -> str:
        return str(self.value)
