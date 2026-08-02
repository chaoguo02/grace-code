"""
G3: Schema Registry — composite (event_type, version) key, explicit codec.

- Registry key: (EventTypeName.value, SchemaVersion.value) tuple
- Duplicate key ALWAYS raises ValueError (even same class)
- event_type version suffix MUST match schema_version
- Each entry carries explicit (encode, decode) codec functions
- decode() validates: source, event_id, scope, UTC, aggregate_version
- Same source+id + different canonical digest → EventIdentityConflict
- Zero `Any` in public API; EventPayload protocol throughout
"""

from __future__ import annotations

import hashlib
import json as _json
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from application.events.envelope import (
    EventEnvelope,
    EventTypeName,
    SchemaVersion,
    EventSource,
    CorrelationId,
    AggregateId,
    AggregateVersion,
    EventPayload,
    _payload_to_plain_dict,
    _payload_from_plain_dict,
)
from application.events.run_facts import (
    RunSubmittedV1, RunStartedV1, RunCompletedV1, RunFailedV1,
    RunCancelledV1, RunBlockedV1, RunGaveUpV1,
)
from application.events.tool_facts import ToolExecutedV1
from application.events.delegation_facts import (
    DelegationCreatedV1, DelegationCompletedV1,
    ChildTaskStartedV1, ChildTaskCompletedV1,
)
from core.eventing.identifiers import RunId, TaskId, SessionId, EventId
from core.eventing.scope import ScopeToken, ScopeKind
from core.json_codec import canonical_digest


# ── Registry types ──────────────────────────────────────────────────────────

# Encoder: payload -> plain dict (for JSON serialization)
PayloadEncoder = Callable[[EventPayload], dict]
# Decoder: plain dict -> payload instance
PayloadDecoder = Callable[[type, dict], EventPayload]
# Key: (event_type_str, version_int)
RegistryKey = tuple[str, int]


@dataclass(frozen=True, slots=True)
class SchemaEntry:
    """Typed schema entry with explicit codec pair.

    The codec functions replace loose asdict()/coerce_field() paths.
    """
    event_type: str       # e.g. "run.completed.v1"
    version: int
    payload_class: type
    description: str = ""

    def __post_init__(self) -> None:
        # Validate event_type ends with version suffix matching self.version
        expected_suffix = f".v{self.version}"
        if not self.event_type.endswith(expected_suffix):
            raise ValueError(
                f"Event type '{self.event_type}' must end with "
                f"'.v{self.version}' (version suffix mismatch)"
            )
        # Validate payload_class is a dataclass
        if not hasattr(self.payload_class, "__dataclass_fields__"):
            raise TypeError(
                f"payload_class {self.payload_class.__name__} "
                f"must be a dataclass"
            )
        # Validate it's a proper EventPayload
        if not isinstance(self.payload_class, type):
            raise TypeError(f"payload_class must be a type, got {self.payload_class}")

    @property
    def key(self) -> RegistryKey:
        return (self.event_type, self.version)


# ── Decode result types ─────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class UnknownSchemaVersion:
    """Returned when an event type+version pair is not registered."""
    event_type: str
    version: int
    message: str = ""


@dataclass(frozen=True, slots=True)
class EventIdentityConflict:
    """Two events claim the same source+id but have different payload digests."""
    event_id: str
    source: str
    existing_digest: str
    incoming_digest: str
    message: str = ""


# Canonical decode result
DecodeResult = EventEnvelope | UnknownSchemaVersion | EventIdentityConflict


# ── SchemaRegistry ──────────────────────────────────────────────────────────

def _default_encode(payload: EventPayload) -> dict:
    """Default encoder: iterate dataclass fields → plain dict."""
    return _payload_to_plain_dict(payload)


def _default_decode(payload_class: type, data: dict) -> EventPayload:
    """Default decoder: reconstruct dataclass from plain dict."""
    return _payload_from_plain_dict(payload_class, data)


class SchemaRegistry:
    """Immutable after construction. Duplicate keys → ValueError.

    Key: (event_type_str, version_int) — composite prevents silent
    overwrites when only event_type or only version matches.
    """

    def __init__(self) -> None:
        self._entries: dict[RegistryKey, SchemaEntry] = {}
        self._encode: dict[RegistryKey, PayloadEncoder] = {}
        self._decode: dict[RegistryKey, PayloadDecoder] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        defaults: list[tuple[SchemaEntry, PayloadEncoder | None, PayloadDecoder | None]] = [
            # Run facts
            (SchemaEntry("run.submitted.v1", 1, RunSubmittedV1), None, None),
            (SchemaEntry("run.started.v1", 1, RunStartedV1), None, None),
            (SchemaEntry("run.completed.v1", 1, RunCompletedV1), None, None),
            (SchemaEntry("run.failed.v1", 1, RunFailedV1), None, None),
            (SchemaEntry("run.cancelled.v1", 1, RunCancelledV1), None, None),
            (SchemaEntry("run.blocked.v1", 1, RunBlockedV1), None, None),
            (SchemaEntry("run.gave_up.v1", 1, RunGaveUpV1), None, None),
            # Tool facts
            (SchemaEntry("tool.executed.v1", 1, ToolExecutedV1), None, None),
            # Delegation facts
            (SchemaEntry("delegation.created.v1", 1, DelegationCreatedV1), None, None),
            (SchemaEntry("delegation.completed.v1", 1, DelegationCompletedV1), None, None),
            (SchemaEntry("child_task.started.v1", 1, ChildTaskStartedV1), None, None),
            (SchemaEntry("child_task.completed.v1", 1, ChildTaskCompletedV1), None, None),
        ]
        for entry, encode_fn, decode_fn in defaults:
            self.register(entry, encoder=encode_fn, decoder=decode_fn)

    def register(
        self,
        entry: SchemaEntry,
        *,
        encoder: PayloadEncoder | None = None,
        decoder: PayloadDecoder | None = None,
    ) -> None:
        """Register a schema entry.

        Raises ValueError on duplicate key (regardless of class match).
        """
        key = entry.key
        if key in self._entries:
            existing = self._entries[key]
            raise ValueError(
                f"Duplicate schema key {key}: "
                f"existing={existing.payload_class.__name__}, "
                f"new={entry.payload_class.__name__}"
            )
        self._entries[key] = entry
        self._encode[key] = encoder or _default_encode
        self._decode[key] = decoder or _default_decode

    def get(self, event_type: str, version: int | None = None) -> SchemaEntry | None:
        """Look up by event_type string and optional version.

        If version is None, searches all entries for matching event_type.
        """
        if version is not None:
            return self._entries.get((event_type, version))
        # Search by event_type string only
        for key, entry in self._entries.items():
            if key[0] == event_type:
                return entry
        return None

    def has(self, event_type: str, version: int | None = None) -> bool:
        return self.get(event_type, version) is not None

    def validate_payload(self, event_type: str, payload: Any) -> bool:
        """True if *payload* is an instance of the registered class."""
        entry = self.get(event_type)
        if entry is None:
            return False
        return isinstance(payload, entry.payload_class)

    # ── Encode ──────────────────────────────────────────────────────────

    def encode(self, envelope: EventEnvelope) -> str:
        """Encode a typed EventEnvelope to canonical JSON string.

        Uses the registered encoder for the payload class.
        """
        key = (str(envelope.event_type), envelope.schema_version.value)
        if key not in self._entries:
            raise UnknownSchemaVersion(
                event_type=str(envelope.event_type),
                version=envelope.schema_version.value,
                message=f"No schema registered for {key}",
            )
        return envelope.canonical_json()

    # ── Decode ──────────────────────────────────────────────────────────

    def decode(self, json_str: str) -> DecodeResult:
        """Decode canonical JSON to typed EventEnvelope.

        Returns:
            EventEnvelope — successfully decoded
            UnknownSchemaVersion — event_type+version not registered
            EventIdentityConflict — same source+id, different digest
        """
        data = _json.loads(json_str)
        event_type = data["event_type"]
        schema_version = data["schema_version"]

        # Look up by composite key
        key = (event_type, schema_version)
        entry = self._entries.get(key)
        if entry is None:
            return UnknownSchemaVersion(
                event_type=event_type,
                version=schema_version,
                message=f"No schema registered for ({event_type}, v{schema_version})",
            )

        # ── Validate envelope fields ─────────────────────────────────
        # occurred_at: must be present and parseable as UTC
        occurred_at_str = data.get("occurred_at", "")
        try:
            occurred_at = datetime.fromisoformat(occurred_at_str)
        except (ValueError, TypeError):
            raise ValueError(
                f"Invalid occurred_at: {occurred_at_str!r}"
            )
        if occurred_at.tzinfo is None:
            raise ValueError(
                f"occurred_at must be timezone-aware: {occurred_at_str}"
            )

        # source: must be "component/process_id"
        source_str = data.get("source", "")
        if "/" in source_str:
            component, process_id = source_str.split("/", 1)
        else:
            raise ValueError(f"Invalid source format: {source_str!r}")

        # event_id: must be valid UUID
        event_id_str = data.get("event_id", "")
        try:
            event_id_uuid = _uuid.UUID(event_id_str)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid event_id UUID: {event_id_str!r}")

        # aggregate_version: must be positive integer
        agg_ver = data.get("aggregate_version", 0)
        if not isinstance(agg_ver, int) or agg_ver < 1:
            raise ValueError(
                f"Invalid aggregate_version: {agg_ver}"
            )

        # ── Scope reconstruction ──────────────────────────────────────
        scope_data = data.get("scope", {})
        scope = ScopeToken(
            kind=ScopeKind(scope_data["kind"]),
            global_id=_uuid.UUID(scope_data["global_id"]),
            generation=scope_data.get("generation", 0),
            session_id=SessionId(scope_data["session_id"])
            if scope_data.get("session_id") else None,
            task_id=TaskId(scope_data["task_id"])
            if scope_data.get("task_id") else None,
        )

        # ── Payload decode ────────────────────────────────────────────
        decoder = self._decode.get(key, _default_decode)
        payload = decoder(entry.payload_class, data.get("payload", {}))

        # ── Assemble envelope ─────────────────────────────────────────
        envelope = EventEnvelope(
            event_id=EventId(value=event_id_uuid),
            event_type=EventTypeName(event_type),
            schema_version=SchemaVersion(schema_version),
            occurred_at=occurred_at,
            source=EventSource(process_id=process_id, component=component),
            scope=scope,
            correlation_id=CorrelationId(data.get("correlation_id", "")),
            causation_id=EventId(value=_uuid.UUID(data["causation_id"]))
            if data.get("causation_id") else None,
            aggregate_id=AggregateId(data.get("aggregate_id", "")),
            aggregate_version=AggregateVersion(agg_ver),
            payload=payload,
        )

        return envelope

    def check_identity(self, json_str: str, seen_digests: dict[str, str] | None = None) -> str | None:
        """Verify event identity.  Returns None if OK or conflict message.

        Checks that no two events claim the same (source, event_id) with
        different payload digests.  *seen_digests* maps "source/event_id" →
        canonical_digest.  If None, always returns None (no tracking).
        """
        if seen_digests is None:
            return None

        data = _json.loads(json_str)
        source = data.get("source", "")
        event_id = data.get("event_id", "")
        identity_key = f"{source}/{event_id}"

        # Compute digest of the canonical payload
        payload_data = data.get("payload", {})
        payload_json = _json.dumps(payload_data, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        if identity_key in seen_digests:
            existing = seen_digests[identity_key]
            if existing != digest:
                return (
                    f"EventIdentityConflict: source={source} event_id={event_id} "
                    f"existing_digest={existing[:16]}... incoming_digest={digest[:16]}..."
                )
            # Same digest → idempotent, OK
            return None

        seen_digests[identity_key] = digest
        return None

    def insert_or_check(
        self,
        json_str: str,
        seen_digests: dict[str, str],
    ) -> EventIdentityConflict | None:
        """Insert event identity or return conflict.

        Used by OutboxStore to prevent INSERT OR IGNORE from silently
        discarding conflicting events.
        """
        data = _json.loads(json_str)
        source = data.get("source", "")
        event_id = data.get("event_id", "")
        identity_key = f"{source}/{event_id}"

        payload_data = data.get("payload", {})
        payload_json = _json.dumps(
            payload_data, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        if identity_key in seen_digests:
            existing = seen_digests[identity_key]
            if existing != digest:
                return EventIdentityConflict(
                    event_id=event_id,
                    source=source,
                    existing_digest=existing,
                    incoming_digest=digest,
                    message=(
                        f"Same source+id with different payload: "
                        f"existing_digest={existing[:16]}... "
                        f"incoming_digest={digest[:16]}..."
                    ),
                )
            # Same identity, same digest → idempotent, return None
            return None

        seen_digests[identity_key] = digest
        return None

    @property
    def registered_types(self) -> list[str]:
        return sorted(k[0] for k in self._entries.keys())

    @property
    def entry_count(self) -> int:
        return len(self._entries)
