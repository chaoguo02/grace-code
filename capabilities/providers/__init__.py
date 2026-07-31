"""Capability provider protocol definitions."""

from __future__ import annotations

from typing import Protocol

from capabilities.models import CapabilityDescriptor, CapabilityQuery


class CapabilityProvider(Protocol):
    def list_descriptors(self, query: CapabilityQuery) -> tuple[CapabilityDescriptor, ...]:
        ...
