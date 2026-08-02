"""P5: Event projections — read-only DomainEvent consumers.

Projections subscribe to DomainEvents and project them to
external stores or caches.  They never mutate Runtime state,
initiate business actions, or hold authoritative state.
"""
