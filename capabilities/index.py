"""Read-only facade for normalized capability snapshots."""

from __future__ import annotations

from collections.abc import Sequence

from capabilities.models import CapabilityDescriptor, CapabilityQuery, CapabilitySnapshot
from capabilities.providers import CapabilityProvider


class CapabilityIndex:
    def __init__(self, providers: Sequence[CapabilityProvider]) -> None:
        self._providers = tuple(providers)

    def snapshot(self, query: CapabilityQuery | None = None) -> CapabilitySnapshot:
        effective_query = query or CapabilityQuery()
        descriptors: list[CapabilityDescriptor] = []
        for provider in self._providers:
            descriptors.extend(provider.list_descriptors(effective_query))
        normalized = self._normalize(tuple(descriptors))
        filtered = tuple(
            descriptor for descriptor in normalized
            if effective_query.matches(descriptor)
        )
        ordered = tuple(sorted(filtered, key=lambda descriptor: descriptor.fingerprint_key()))
        return CapabilitySnapshot(
            descriptors=ordered,
            fingerprint=CapabilitySnapshot.fingerprint_for(ordered),
        )

    @staticmethod
    def _normalize(
        descriptors: tuple[CapabilityDescriptor, ...],
    ) -> tuple[CapabilityDescriptor, ...]:
        deduped: dict[tuple[str, str, str], CapabilityDescriptor] = {}
        for descriptor in descriptors:
            key = (
                descriptor.metadata.kind.value,
                descriptor.metadata.namespace,
                descriptor.metadata.name,
            )
            deduped[key] = descriptor
        return tuple(deduped.values())
