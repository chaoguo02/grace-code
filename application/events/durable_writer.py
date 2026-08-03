"""
P7: Durable Fact Writer — accepts whitelist envelopes, rejects dict.

Protocol only.  Eliminates agent→server reverse dependency.
"""

from __future__ import annotations

from typing import Protocol

from application.events.envelope import EventEnvelope


class DurableFactRejectedError(RuntimeError):
    """Envelope rejected — payload not in schema registry or scope closed."""


class DurableFactWriter(Protocol):
    """Write durable facts to the transactional outbox.

    Only accepts EventEnvelope instances with registered payload types.
    Raw dicts are rejected at the type level.
    """

    def write(self, envelope: EventEnvelope) -> None:
        """Write a durable fact.

        MUST be called inside a SessionUnitOfWork transaction.
        Raises DurableFactRejectedError if the payload is not registered
        or the target scope is closed.
        """
        ...
