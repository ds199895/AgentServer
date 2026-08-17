from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .errors import ValidationError


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class EntityKind(StringEnum):
    DEVICE = "device"
    TERMINAL = "terminal"
    AGENT_INSTANCE = "agent_instance"
    TASK = "task"
    ASSIGNMENT = "assignment"
    RUN = "run"
    SPAN = "span"
    ARTIFACT = "artifact"


class RelationKind(StringEnum):
    PARENT_RUN = "parent_run"
    CONTAINS = "contains"
    EXECUTES = "executes"
    ASSIGNED_TO = "assigned_to"
    BOUND_TO = "bound_to"
    PRODUCED = "produced"


class TerminalLifecycle(StringEnum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    CONNECTING = "connecting"
    READY = "ready"
    DISCONNECTED = "disconnected"
    EXITED = "exited"
    FAILED = "failed"


class AgentLifecycle(StringEnum):
    DISCOVERED = "discovered"
    STARTING = "starting"
    ONLINE = "online"
    STOPPING = "stopping"
    UNREACHABLE = "unreachable"
    EXITED = "exited"
    LOST = "lost"


class TaskLifecycle(StringEnum):
    SUBMITTED = "submitted"
    ASSIGNED = "assigned"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    AUTH_REQUIRED = "auth_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


class AssignmentLifecycle(StringEnum):
    CREATED = "created"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    COMPLETED = "completed"


class RunLifecycle(StringEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


class RunActivity(StringEnum):
    IDLE = "idle"
    THINKING = "thinking"
    PLANNING = "planning"
    CODING = "coding"
    TOOLING = "tooling"
    TESTING = "testing"
    REVIEWING = "reviewing"
    WAITING = "waiting"
    FINALIZING = "finalizing"
    UNKNOWN = "unknown"


class WaitReason(StringEnum):
    USER_INPUT = "user_input"
    APPROVAL = "approval"
    AUTHENTICATION = "authentication"
    TOOL = "tool"
    CHILD_RUN = "child_run"
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    RETRY_BACKOFF = "retry_backoff"
    DEPENDENCY = "dependency"
    RESOURCE = "resource"
    UNKNOWN = "unknown"


class SpanLifecycle(StringEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProducerMode(StringEnum):
    CONTROL = "control"
    ACTIVE = "active"
    ADAPTER = "adapter"
    OBSERVED = "observed"
    SYSTEM = "system"


class AppendStatus(StringEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


class LeaseStatus(StringEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class CommandStatus(StringEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"
    EXPIRED = "expired"


TERMINAL_COMMAND_STATUSES = {
    CommandStatus.REJECTED,
    CommandStatus.COMPLETED,
    CommandStatus.EXPIRED,
}


def enum_value(value: StringEnum | str) -> str:
    result = value.value if isinstance(value, StringEnum) else str(value).strip()
    if not result:
        raise ValidationError("enum-like values must not be empty")
    return result


def json_object(value: Mapping[str, Any] | None, *, field_name: str) -> dict[str, Any]:
    result = dict(value or {})
    try:
        encoded = json.dumps(
            result, separators=(",", ":"), sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a JSON object") from exc
    # Normalize and detach nested containers so later caller mutations cannot
    # change an envelope after its idempotency fingerprint has been computed.
    return json.loads(encoded)


@dataclass(frozen=True)
class Entity:
    owner_id: str
    kind: str
    id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "kind": self.kind,
            "id": self.id,
            "attributes": dict(self.attributes),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class EntityRelation:
    id: str
    owner_id: str
    relation: str
    source_kind: str
    source_id: str
    target_kind: str
    target_id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "relation": self.relation,
            "source": {"kind": self.source_kind, "id": self.source_id},
            "target": {"kind": self.target_kind, "id": self.target_id},
            "attributes": dict(self.attributes),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Projection:
    owner_id: str
    aggregate_kind: str
    aggregate_id: str
    revision: int
    state: Mapping[str, Any]
    updated_sequence: int
    updated_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "aggregate_kind": self.aggregate_kind,
            "aggregate_id": self.aggregate_id,
            "revision": self.revision,
            "state": dict(self.state),
            "updated_sequence": self.updated_sequence,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class Lease:
    id: str
    owner_id: str
    resource_kind: str
    resource_id: str
    holder_id: str
    status: LeaseStatus
    revision: int
    acquired_at: float
    renewed_at: float
    expires_at: float
    released_at: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status is LeaseStatus.ACTIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "holder_id": self.holder_id,
            "status": self.status.value,
            "revision": self.revision,
            "acquired_at": self.acquired_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
            "released_at": self.released_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Command:
    sequence: int
    id: str
    owner_id: str
    target_kind: str
    target_id: str
    type: str
    payload: Mapping[str, Any]
    status: CommandStatus
    expected_revision: int | None
    created_at: float
    expires_at: float | None
    delivered_at: float | None = None
    acked_at: float | None = None
    ack_payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_COMMAND_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "command_id": self.id,
            "owner_id": self.owner_id,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "type": self.type,
            "payload": dict(self.payload),
            "status": self.status.value,
            "expected_revision": self.expected_revision,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "delivered_at": self.delivered_at,
            "acked_at": self.acked_at,
            "ack_payload": dict(self.ack_payload),
        }


@dataclass(frozen=True)
class ResyncRequired:
    after_sequence: int
    latest_sequence: int
    reason: str = "subscription_queue_overflow"

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "subscription.resync_required",
            "after_sequence": self.after_sequence,
            "latest_sequence": self.latest_sequence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProducerAcknowledgement:
    producer_id: str
    producer_epoch: str
    accepted_through_seq: int
    missing_ranges: tuple[tuple[int, int], ...]
    received_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "producer_id": self.producer_id,
            "producer_epoch": self.producer_epoch,
            "accepted_through_seq": self.accepted_through_seq,
            "missing_ranges": [list(item) for item in self.missing_ranges],
            "received_count": self.received_count,
        }
