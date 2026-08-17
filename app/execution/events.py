from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import ValidationError
from .models import AppendStatus, ProducerMode, Projection, json_object


EVENT_SCHEMA = "agentserver.event/1"


def new_id() -> str:
    """Return an opaque, collision-resistant protocol identifier."""
    return uuid.uuid4().hex


def _require_id(value: str | None, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    result = str(value or "").strip()
    if not result:
        raise ValidationError(f"{name} must not be empty")
    if len(result) > 255:
        raise ValidationError(f"{name} must not exceed 255 characters")
    return result


@dataclass(frozen=True)
class EventScope:
    owner_id: str
    device_id: str | None = None
    terminal_id: str | None = None
    launch_id: str | None = None
    agent_instance_id: str | None = None
    task_id: str | None = None
    assignment_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    span_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_id", _require_id(self.owner_id, "owner_id"))
        for name in (
            "device_id",
            "terminal_id",
            "launch_id",
            "agent_instance_id",
            "task_id",
            "assignment_id",
            "run_id",
            "parent_run_id",
            "span_id",
        ):
            object.__setattr__(
                self, name, _require_id(getattr(self, name), name, optional=True)
            )

    def as_dict(self) -> dict[str, str | None]:
        return {
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "terminal_id": self.terminal_id,
            "launch_id": self.launch_id,
            "agent_instance_id": self.agent_instance_id,
            "task_id": self.task_id,
            "assignment_id": self.assignment_id,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "span_id": self.span_id,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> EventScope:
        return cls(**{name: values.get(name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ProducerRef:
    id: str
    epoch: str
    seq: int
    adapter: str = ""
    version: str = ""
    mode: ProducerMode = ProducerMode.ACTIVE

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_id(self.id, "producer.id"))
        object.__setattr__(self, "epoch", _require_id(self.epoch, "producer.epoch"))
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 0:
            raise ValidationError("producer.seq must be a non-negative integer")
        try:
            mode = self.mode if isinstance(self.mode, ProducerMode) else ProducerMode(self.mode)
        except ValueError as exc:
            raise ValidationError(f"unsupported producer mode: {self.mode}") from exc
        object.__setattr__(self, "mode", mode)
        if len(self.adapter) > 100 or len(self.version) > 100:
            raise ValidationError("producer adapter/version must not exceed 100 characters")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "epoch": self.epoch,
            "seq": self.seq,
            "adapter": self.adapter,
            "version": self.version,
            "mode": self.mode.value,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> ProducerRef:
        return cls(
            id=str(values.get("id") or ""),
            epoch=str(values.get("epoch") or ""),
            seq=values.get("seq"),
            adapter=str(values.get("adapter") or ""),
            version=str(values.get("version") or ""),
            mode=values.get("mode") or ProducerMode.ACTIVE,
        )


@dataclass(frozen=True)
class Evidence:
    confidence: float = 1.0
    valid_for_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.confidence, (int, float)) or isinstance(
            self.confidence, bool
        ):
            raise ValidationError("evidence.confidence must be numeric")
        if not 0 <= float(self.confidence) <= 1:
            raise ValidationError("evidence.confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", float(self.confidence))
        if self.valid_for_ms is not None and (
            not isinstance(self.valid_for_ms, int)
            or isinstance(self.valid_for_ms, bool)
            or self.valid_for_ms < 0
        ):
            raise ValidationError("evidence.valid_for_ms must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "valid_for_ms": self.valid_for_ms,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, Any] | None) -> Evidence | None:
        if values is None:
            return None
        return cls(
            confidence=values.get("confidence", 1.0),
            valid_for_ms=values.get("valid_for_ms"),
        )


@dataclass(frozen=True)
class EventEnvelope:
    type: str
    scope: EventScope
    producer: ProducerRef
    event_id: str = field(default_factory=new_id)
    payload: Mapping[str, Any] = field(default_factory=dict)
    expected_revision: int | None = None
    occurred_at: str | float | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    traceparent: str | None = None
    evidence: Evidence | None = None
    schema: str = EVENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EVENT_SCHEMA:
            raise ValidationError(f"unsupported event schema: {self.schema}")
        object.__setattr__(self, "event_id", _require_id(self.event_id, "event_id"))
        event_type = str(self.type or "").strip()
        if not event_type or len(event_type) > 120:
            raise ValidationError("event type must contain 1..120 characters")
        object.__setattr__(self, "type", event_type)
        object.__setattr__(
            self, "payload", json_object(self.payload, field_name="event payload")
        )
        if self.expected_revision is not None and (
            not isinstance(self.expected_revision, int)
            or isinstance(self.expected_revision, bool)
            or self.expected_revision < 0
        ):
            raise ValidationError("expected_revision must be a non-negative integer")
        for name in ("causation_id", "correlation_id"):
            object.__setattr__(
                self, name, _require_id(getattr(self, name), name, optional=True)
            )
        if self.traceparent is not None and len(self.traceparent) > 512:
            raise ValidationError("traceparent must not exceed 512 characters")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "type": self.type,
            "scope": self.scope.as_dict(),
            "producer": self.producer.as_dict(),
            "expected_revision": self.expected_revision,
            "occurred_at": self.occurred_at,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "traceparent": self.traceparent,
            "evidence": self.evidence.as_dict() if self.evidence else None,
            "payload": dict(self.payload),
        }

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.as_dict(), separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> EventEnvelope:
        return cls(
            schema=str(values.get("schema") or ""),
            event_id=str(values.get("event_id") or ""),
            type=str(values.get("type") or ""),
            scope=EventScope.from_dict(values.get("scope") or {}),
            producer=ProducerRef.from_dict(values.get("producer") or {}),
            expected_revision=values.get("expected_revision"),
            occurred_at=values.get("occurred_at"),
            causation_id=values.get("causation_id"),
            correlation_id=values.get("correlation_id"),
            traceparent=values.get("traceparent"),
            evidence=Evidence.from_dict(values.get("evidence")),
            payload=values.get("payload") or {},
        )


@dataclass(frozen=True)
class StoredEvent:
    global_sequence: int
    stream_version: int | None
    recorded_at: float
    envelope: EventEnvelope
    aggregate_kind: str | None = None
    aggregate_id: str | None = None

    @property
    def id(self) -> str:
        return self.envelope.event_id

    @property
    def type(self) -> str:
        return self.envelope.type

    @property
    def scope(self) -> EventScope:
        return self.envelope.scope

    @property
    def producer(self) -> ProducerRef:
        return self.envelope.producer

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.envelope.payload

    def as_dict(self) -> dict[str, Any]:
        result = self.envelope.as_dict()
        result.update(
            {
                "global_sequence": self.global_sequence,
                "stream_version": self.stream_version,
                "recorded_at": self.recorded_at,
                "aggregate_kind": self.aggregate_kind,
                "aggregate_id": self.aggregate_id,
            }
        )
        return result


@dataclass(frozen=True)
class AppendResult:
    status: AppendStatus
    event: StoredEvent
    projection: Projection | None

    @property
    def duplicate(self) -> bool:
        return self.status is AppendStatus.DUPLICATE


@dataclass(frozen=True)
class ExecutionSnapshot:
    owner_id: str
    as_of_sequence: int
    after_sequence: int
    events: tuple[StoredEvent, ...]
    projections: tuple[Projection, ...]
    resync_required: bool = False

    @property
    def cursor(self) -> int:
        return self.as_of_sequence

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "after_sequence": self.after_sequence,
            "as_of_sequence": self.as_of_sequence,
            "events": [event.as_dict() for event in self.events],
            "projections": [projection.as_dict() for projection in self.projections],
            "resync_required": self.resync_required,
        }


def server_timestamp() -> float:
    return time.time()
