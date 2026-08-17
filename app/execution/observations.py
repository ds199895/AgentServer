from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from functools import wraps
from typing import Any, Callable, Mapping, ParamSpec, Protocol, TypeVar

from .errors import IdempotencyConflict, ValidationError
from .events import EventEnvelope, EventScope, Evidence, ProducerRef, StoredEvent, new_id
from .models import EntityKind, ProducerMode, RunActivity, WaitReason, json_object


__all__ = [
    "FieldAuthority",
    "FieldCandidate",
    "MergedObservation",
    "ObservationCallback",
    "ObservationDraft",
    "ObservationEventSink",
    "ObservationMerger",
    "ObservationPublisher",
    "ObservationRecord",
    "ObservationTarget",
    "ProcessFingerprint",
    "ProcessObservation",
    "ResolvedField",
]


class FieldAuthority(IntEnum):
    """Field-local authority; it is deliberately not a global source priority."""

    HEURISTIC = 50
    PTY = 100
    PROCESS = 200
    SYSTEM = 300
    ADAPTER = 400
    ACTIVE = 500
    CONTROL = 600


@dataclass(frozen=True)
class ProcessFingerprint:
    """A process incarnation, safe against operating-system PID reuse."""

    device_id: str
    pid: int
    start_time: str | int | float
    boot_id: str = ""
    pgid: int | None = None
    tty: str = ""
    launch_nonce: str = ""

    def __post_init__(self) -> None:
        device_id = str(self.device_id or "").strip()
        if not device_id or len(device_id) > 255:
            raise ValidationError("process fingerprint device_id is required")
        object.__setattr__(self, "device_id", device_id)
        if not isinstance(self.pid, int) or isinstance(self.pid, bool) or self.pid <= 0:
            raise ValidationError("process fingerprint pid must be positive")
        start_time = str(self.start_time).strip()
        if not start_time or len(start_time) > 100:
            raise ValidationError("process fingerprint start_time is required")
        object.__setattr__(self, "start_time", start_time)
        if self.pgid is not None and (
            not isinstance(self.pgid, int)
            or isinstance(self.pgid, bool)
            or self.pgid <= 0
        ):
            raise ValidationError("process fingerprint pgid must be positive")
        for name in ("boot_id", "tty", "launch_nonce"):
            value = str(getattr(self, name) or "").strip()
            if len(value) > 255:
                raise ValidationError(f"process fingerprint {name} is too long")
            object.__setattr__(self, name, value)

    @property
    def instance_id(self) -> str:
        # TTY and PGID are attestation metadata and may be discovered later.
        # The stable incarnation key is device boot + PID + process start time.
        identity = json.dumps(
            {
                "device_id": self.device_id,
                "boot_id": self.boot_id,
                "pid": self.pid,
                "start_time": self.start_time,
                "launch_nonce": self.launch_nonce,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "process:" + hashlib.sha256(identity).hexdigest()[:32]

    def same_incarnation(self, other: ProcessFingerprint) -> bool:
        return self.instance_id == other.instance_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "device_id": self.device_id,
            "pid": self.pid,
            "start_time": self.start_time,
            "boot_id": self.boot_id,
            "pgid": self.pgid,
            "tty": self.tty,
            "launch_nonce": self.launch_nonce,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> ProcessFingerprint:
        return cls(
            device_id=str(values.get("device_id") or ""),
            pid=values.get("pid"),
            start_time=values.get("start_time", ""),
            boot_id=str(values.get("boot_id") or ""),
            pgid=values.get("pgid"),
            tty=str(values.get("tty") or ""),
            launch_nonce=str(values.get("launch_nonce") or ""),
        )


@dataclass(frozen=True)
class ObservationTarget:
    owner_id: str
    kind: str
    id: str
    device_id: str | None = None
    terminal_id: str | None = None
    agent_instance_id: str | None = None
    run_id: str | None = None

    @property
    def attributed(self) -> bool:
        return self.kind != EntityKind.DEVICE.value

    @property
    def key(self) -> tuple[str, str, str]:
        return self.owner_id, self.kind, self.id

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "kind": self.kind,
            "id": self.id,
            "device_id": self.device_id,
            "terminal_id": self.terminal_id,
            "agent_instance_id": self.agent_instance_id,
            "run_id": self.run_id,
            "attributed": self.attributed,
        }

    @classmethod
    def from_scope(cls, scope: EventScope) -> ObservationTarget:
        if scope.run_id:
            kind, identifier = EntityKind.RUN.value, scope.run_id
        elif scope.agent_instance_id:
            kind, identifier = EntityKind.AGENT_INSTANCE.value, scope.agent_instance_id
        elif scope.terminal_id:
            kind, identifier = EntityKind.TERMINAL.value, scope.terminal_id
        elif scope.device_id:
            kind, identifier = EntityKind.DEVICE.value, scope.device_id
        else:
            raise ValidationError(
                "observation scope requires run, agent, terminal, or device identity"
            )
        return cls(
            owner_id=scope.owner_id,
            kind=kind,
            id=identifier,
            device_id=scope.device_id,
            terminal_id=scope.terminal_id,
            agent_instance_id=scope.agent_instance_id,
            run_id=scope.run_id,
        )


@dataclass(frozen=True)
class ObservationDraft:
    """Structured TerminalManager/bridge callback payload before persistence."""

    type: str
    owner_id: str
    device_id: str
    terminal_id: str | None = None
    launch_id: str | None = None
    agent_instance_id: str | None = None
    task_id: str | None = None
    assignment_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: ProcessFingerprint | None = None
    confidence: float = 0.5
    valid_for_ms: int | None = 10_000
    observed_at: float = field(default_factory=time.time)
    observation_id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        event_type = str(self.type or "").strip()
        if not event_type.startswith("observation.") or len(event_type) > 120:
            raise ValidationError("passive drafts require an observation.* event type")
        object.__setattr__(self, "type", event_type)
        scope = self.scope
        if not scope.device_id:
            raise ValidationError("passive observations require device_id")
        if self.fingerprint and self.fingerprint.device_id != scope.device_id:
            raise ValidationError("process fingerprint and observation device differ")
        if not isinstance(self.confidence, (int, float)) or isinstance(
            self.confidence, bool
        ):
            raise ValidationError("observation confidence must be numeric")
        if not 0 <= float(self.confidence) <= 1:
            raise ValidationError("observation confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", float(self.confidence))
        if self.valid_for_ms is not None and (
            not isinstance(self.valid_for_ms, int)
            or isinstance(self.valid_for_ms, bool)
            or self.valid_for_ms < 0
        ):
            raise ValidationError("observation valid_for_ms must be non-negative")
        values = json_object(self.payload, field_name="observation payload")
        if self.fingerprint:
            values["process_fingerprint"] = self.fingerprint.as_dict()
        object.__setattr__(self, "payload", values)
        observation_id = str(self.observation_id or "").strip()
        if not observation_id or len(observation_id) > 255:
            raise ValidationError("observation_id must contain 1..255 characters")
        object.__setattr__(self, "observation_id", observation_id)

    @property
    def scope(self) -> EventScope:
        return EventScope(
            owner_id=self.owner_id,
            device_id=self.device_id,
            terminal_id=self.terminal_id,
            launch_id=self.launch_id,
            agent_instance_id=self.agent_instance_id,
            task_id=self.task_id,
            assignment_id=self.assignment_id,
            run_id=self.run_id,
            parent_run_id=self.parent_run_id,
        )

    @property
    def target(self) -> ObservationTarget:
        return ObservationTarget.from_scope(self.scope)

    @property
    def attributed(self) -> bool:
        return self.target.attributed

    def to_event(self, producer: ProducerRef) -> EventEnvelope:
        if producer.mode not in {ProducerMode.OBSERVED, ProducerMode.ADAPTER}:
            raise ValidationError("observation producers must be observed or adapter mode")
        return EventEnvelope(
            type=self.type,
            event_id=self.observation_id,
            scope=self.scope,
            producer=producer,
            occurred_at=self.observed_at,
            evidence=Evidence(
                confidence=self.confidence,
                valid_for_ms=self.valid_for_ms,
            ),
            payload=self.payload,
        )

    @classmethod
    def process_started(
        cls,
        *,
        owner_id: str,
        device_id: str,
        fingerprint: ProcessFingerprint,
        terminal_id: str | None = None,
        launch_id: str | None = None,
        agent_instance_id: str | None = None,
        run_id: str | None = None,
        agent_kind: str | None = None,
        cwd: str = "",
        confidence: float = 1.0,
        valid_for_ms: int | None = 15_000,
        observed_at: float | None = None,
    ) -> ObservationDraft:
        payload: dict[str, Any] = {}
        if agent_kind:
            payload["agent_kind"] = str(agent_kind)
        if cwd:
            payload["cwd"] = str(cwd)
        return cls(
            type="observation.process.started",
            owner_id=owner_id,
            device_id=device_id,
            terminal_id=terminal_id,
            launch_id=launch_id,
            agent_instance_id=agent_instance_id,
            run_id=run_id,
            payload=payload,
            fingerprint=fingerprint,
            confidence=confidence,
            valid_for_ms=valid_for_ms,
            observed_at=time.time() if observed_at is None else observed_at,
        )

    @classmethod
    def process_exited(
        cls,
        *,
        owner_id: str,
        device_id: str,
        fingerprint: ProcessFingerprint,
        terminal_id: str | None = None,
        launch_id: str | None = None,
        agent_instance_id: str | None = None,
        run_id: str | None = None,
        return_code: int | None = None,
        confidence: float = 1.0,
        observed_at: float | None = None,
    ) -> ObservationDraft:
        return cls(
            type="observation.process.exited",
            owner_id=owner_id,
            device_id=device_id,
            terminal_id=terminal_id,
            launch_id=launch_id,
            agent_instance_id=agent_instance_id,
            run_id=run_id,
            payload={"return_code": return_code},
            fingerprint=fingerprint,
            confidence=confidence,
            # Exit is a durable physical fact, not a freshness sample.
            valid_for_ms=None,
            observed_at=time.time() if observed_at is None else observed_at,
        )

    @classmethod
    def phase(
        cls,
        *,
        owner_id: str,
        device_id: str,
        activity: RunActivity | str,
        terminal_id: str | None = None,
        run_id: str | None = None,
        wait_reason: WaitReason | str | None = None,
        confidence: float = 0.5,
        valid_for_ms: int = 5_000,
        observed_at: float | None = None,
    ) -> ObservationDraft:
        try:
            normalized_activity = (
                activity if isinstance(activity, RunActivity) else RunActivity(activity)
            )
        except ValueError as exc:
            raise ValidationError("passive phase activity is invalid") from exc
        payload: dict[str, Any] = {"activity": normalized_activity.value}
        if normalized_activity is RunActivity.WAITING:
            try:
                reason = (
                    wait_reason
                    if isinstance(wait_reason, WaitReason)
                    else WaitReason(wait_reason or "")
                )
            except ValueError as exc:
                raise ValidationError("passive waiting phase requires wait_reason") from exc
            payload["wait_reason"] = reason.value
        return cls(
            type="observation.run.activity",
            owner_id=owner_id,
            device_id=device_id,
            terminal_id=terminal_id,
            run_id=run_id,
            payload=payload,
            confidence=confidence,
            valid_for_ms=valid_for_ms,
            observed_at=time.time() if observed_at is None else observed_at,
        )


SinkResult = TypeVar("SinkResult")
MethodParameters = ParamSpec("MethodParameters")
MethodResult = TypeVar("MethodResult")


class ObservationEventSink(Protocol[SinkResult]):
    def __call__(self, event: EventEnvelope) -> SinkResult: ...


class ObservationPublisher:
    """Thread-safe adapter from TerminalManager callbacks to an event sink."""

    def __init__(
        self,
        sink: ObservationEventSink[SinkResult],
        *,
        producer_id: str = "agentserver:terminal-observer",
        producer_epoch: str | None = None,
        adapter: str = "terminal-manager",
        initial_sequence: int = 0,
    ) -> None:
        if initial_sequence < 0:
            raise ValidationError("initial observation sequence must be non-negative")
        self.sink = sink
        self.producer_id = str(producer_id or "").strip()
        self.producer_epoch = producer_epoch or uuid.uuid4().hex
        self.adapter = str(adapter or "")[:100]
        self._sequence = initial_sequence
        self._lock = threading.Lock()

    def __call__(self, draft: ObservationDraft) -> SinkResult:
        if not isinstance(draft, ObservationDraft):
            raise TypeError("observation callback requires ObservationDraft")
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        event = draft.to_event(
            ProducerRef(
                id=self.producer_id,
                epoch=self.producer_epoch,
                seq=sequence,
                adapter=self.adapter,
                version="1",
                mode=ProducerMode.OBSERVED,
            )
        )
        return self.sink(event)


@dataclass(frozen=True)
class FieldCandidate:
    field: str
    value: Any
    target: ObservationTarget
    event_id: str
    event_type: str
    source: str
    authority: FieldAuthority
    confidence: float
    global_sequence: int
    recorded_at: float
    expires_at: float | None

    def fresh(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "target": self.target.as_dict(),
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "authority": self.authority.name.lower(),
            "authority_rank": int(self.authority),
            "confidence": self.confidence,
            "global_sequence": self.global_sequence,
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class ResolvedField:
    field: str
    value: Any
    source: str
    authority: FieldAuthority | None
    confidence: float
    global_sequence: int
    recorded_at: float | None
    expires_at: float | None
    stale: bool
    last_value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "authority": self.authority.name.lower() if self.authority else None,
            "authority_rank": int(self.authority) if self.authority else 0,
            "confidence": self.confidence,
            "global_sequence": self.global_sequence,
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
            "stale": self.stale,
            "last_value": self.last_value,
        }


@dataclass(frozen=True)
class ProcessObservation:
    fingerprint: ProcessFingerprint
    target: ObservationTarget
    alive: bool
    confidence: float
    first_sequence: int
    last_sequence: int
    recorded_at: float
    expires_at: float | None
    agent_kind: str = ""
    cwd: str = ""
    return_code: int | None = None

    @property
    def instance_id(self) -> str:
        return self.fingerprint.instance_id

    def fresh(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "fingerprint": self.fingerprint.as_dict(),
            "target": self.target.as_dict(),
            "alive": self.alive,
            "confidence": self.confidence,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
            "agent_kind": self.agent_kind,
            "cwd": self.cwd,
            "return_code": self.return_code,
        }


@dataclass(frozen=True)
class ObservationRecord:
    event_id: str
    event_type: str
    target: ObservationTarget
    global_sequence: int
    recorded_at: float
    payload: Mapping[str, Any]
    fingerprint: ProcessFingerprint | None = None

    @property
    def unattributed(self) -> bool:
        return not self.target.attributed

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "target": self.target.as_dict(),
            "global_sequence": self.global_sequence,
            "recorded_at": self.recorded_at,
            "payload": dict(self.payload),
            "process_fingerprint": (
                self.fingerprint.as_dict() if self.fingerprint else None
            ),
            "unattributed": self.unattributed,
        }


@dataclass(frozen=True)
class MergedObservation:
    target: ObservationTarget
    fields: Mapping[str, ResolvedField]
    processes: tuple[ProcessObservation, ...]
    records: tuple[ObservationRecord, ...]

    @property
    def state(self) -> dict[str, Any]:
        return {name: value.value for name, value in self.fields.items()}

    @property
    def stale(self) -> bool:
        return any(field.stale for field in self.fields.values())

    @property
    def unattributed(self) -> bool:
        return not self.target.attributed

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.as_dict(),
            "state": self.state,
            "fields": {
                name: value.as_dict() for name, value in self.fields.items()
            },
            "processes": [process.as_dict() for process in self.processes],
            "records": [record.as_dict() for record in self.records],
            "stale": self.stale,
            "unattributed": self.unattributed,
        }


_LIFECYCLE_VALUES = {
    "terminal.launch.requested": "requested",
    "terminal.provisioning": "provisioning",
    "terminal.connecting": "connecting",
    "terminal.ready": "ready",
    "terminal.disconnected": "disconnected",
    "terminal.exited": "exited",
    "terminal.failed": "failed",
    "terminal.launch.failed": "failed",
    "agent.discovered": "discovered",
    "agent.starting": "starting",
    "agent.registered": "online",
    "agent.stopping": "stopping",
    "agent.unreachable": "unreachable",
    "agent.recovered": "online",
    "agent.exited": "exited",
    "agent.lost": "lost",
    "task.created": "submitted",
    "task.assigned": "assigned",
    "task.working": "working",
    "task.completed": "completed",
    "task.failed": "failed",
    "task.canceled": "canceled",
    "task.rejected": "rejected",
    "assignment.created": "created",
    "assignment.accepted": "accepted",
    "assignment.rejected": "rejected",
    "assignment.expired": "expired",
    "assignment.completed": "completed",
    "run.requested": "pending",
    "run.starting": "starting",
    "run.started": "running",
    "run.succeeded": "succeeded",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.lost": "lost",
}


def _synchronized(
    method: Callable[MethodParameters, MethodResult],
) -> Callable[MethodParameters, MethodResult]:
    @wraps(method)
    def wrapped(
        *args: MethodParameters.args, **kwargs: MethodParameters.kwargs
    ) -> MethodResult:
        owner = args[0]
        with owner._lock:
            return method(*args, **kwargs)

    return wrapped


class ObservationMerger:
    """Merge immutable facts into field-local, freshness-aware candidates.

    It never writes a Run projection. In particular, process exit only updates
    the exact process incarnation and cannot manufacture a successful outcome.
    """

    def __init__(
        self,
        *,
        default_active_ttl_ms: int = 30_000,
        default_observation_ttl_ms: int = 10_000,
    ) -> None:
        if default_active_ttl_ms < 0 or default_observation_ttl_ms < 0:
            raise ValueError("default evidence TTLs must be non-negative")
        self.default_active_ttl_ms = default_active_ttl_ms
        self.default_observation_ttl_ms = default_observation_ttl_ms
        self._lock = threading.RLock()
        self._candidates: dict[
            tuple[str, str, str], dict[str, list[FieldCandidate]]
        ] = {}
        self._targets: dict[tuple[str, str, str], ObservationTarget] = {}
        self._records: dict[tuple[str, str, str], list[ObservationRecord]] = {}
        self._processes: dict[str, ProcessObservation] = {}
        self._event_fingerprints: dict[str, str] = {}
        self._global_sequences: dict[int, str] = {}

    @staticmethod
    def _source(mode: ProducerMode) -> str:
        return {
            ProducerMode.CONTROL: "control",
            ProducerMode.ACTIVE: "reported",
            ProducerMode.ADAPTER: "adapter",
            ProducerMode.SYSTEM: "inferred",
            ProducerMode.OBSERVED: "observed",
        }[mode]

    @staticmethod
    def _authority(
        event_type: str, mode: ProducerMode, field_name: str
    ) -> FieldAuthority:
        if field_name == "lifecycle" and mode in {
            ProducerMode.CONTROL,
            ProducerMode.SYSTEM,
        }:
            # AgentServer's validated state machine owns canonical lifecycle.
            return FieldAuthority.CONTROL
        if mode is ProducerMode.CONTROL:
            return FieldAuthority.CONTROL
        if mode is ProducerMode.ACTIVE:
            return FieldAuthority.ACTIVE
        if mode is ProducerMode.ADAPTER:
            return FieldAuthority.ADAPTER
        if mode is ProducerMode.SYSTEM:
            return FieldAuthority.SYSTEM
        if event_type.startswith("observation.process."):
            return FieldAuthority.PROCESS
        if event_type == "observation.pty.signature":
            return FieldAuthority.PTY
        return FieldAuthority.HEURISTIC

    def _ttl_ms(self, event: EventEnvelope, field_name: str) -> int | None:
        if event.evidence and event.evidence.valid_for_ms is not None:
            return event.evidence.valid_for_ms
        if field_name == "lifecycle":
            return None
        if event.type == "observation.process.exited":
            return None
        if event.producer.mode in {ProducerMode.ACTIVE, ProducerMode.ADAPTER}:
            return self.default_active_ttl_ms
        if event.producer.mode is ProducerMode.OBSERVED:
            return self.default_observation_ttl_ms
        return None

    def _add_candidate(
        self,
        *,
        target: ObservationTarget,
        event: EventEnvelope,
        field_name: str,
        value: Any,
        global_sequence: int,
        recorded_at: float,
    ) -> None:
        ttl_ms = self._ttl_ms(event, field_name)
        expires_at = recorded_at + ttl_ms / 1000 if ttl_ms is not None else None
        evidence = event.evidence
        candidate = FieldCandidate(
            field=field_name,
            value=value,
            target=target,
            event_id=event.event_id,
            event_type=event.type,
            source=self._source(event.producer.mode),
            authority=self._authority(event.type, event.producer.mode, field_name),
            confidence=evidence.confidence if evidence else 1.0,
            global_sequence=global_sequence,
            recorded_at=recorded_at,
            expires_at=expires_at,
        )
        self._candidates.setdefault(target.key, {}).setdefault(field_name, []).append(
            candidate
        )

    @staticmethod
    def _fingerprint(event: EventEnvelope) -> ProcessFingerprint | None:
        values = event.payload.get("process_fingerprint")
        if values is None:
            return None
        if not isinstance(values, Mapping):
            raise ValidationError("process_fingerprint must be an object")
        fingerprint = ProcessFingerprint.from_dict(values)
        if event.scope.device_id and fingerprint.device_id != event.scope.device_id:
            raise ValidationError("process fingerprint and event scope device differ")
        return fingerprint

    def _observe_process(
        self,
        *,
        target: ObservationTarget,
        event: EventEnvelope,
        fingerprint: ProcessFingerprint,
        global_sequence: int,
        recorded_at: float,
    ) -> None:
        started = event.type == "observation.process.started"
        current = self._processes.get(fingerprint.instance_id)
        if current and global_sequence <= current.last_sequence:
            return
        # An exit is absorbing for one exact incarnation. A delayed start event
        # may not resurrect it; PID reuse instead creates a new fingerprint.
        if current and not current.alive and started:
            return
        ttl_ms = self._ttl_ms(event, "process_alive") if started else None
        expires_at = recorded_at + ttl_ms / 1000 if ttl_ms is not None else None
        confidence = event.evidence.confidence if event.evidence else 1.0
        self._processes[fingerprint.instance_id] = ProcessObservation(
            fingerprint=fingerprint,
            target=target,
            alive=started,
            confidence=confidence,
            first_sequence=(
                current.first_sequence if current else global_sequence
            ),
            last_sequence=global_sequence,
            recorded_at=recorded_at,
            expires_at=expires_at,
            agent_kind=str(
                event.payload.get("agent_kind")
                or (current.agent_kind if current else "")
            ),
            cwd=str(event.payload.get("cwd") or (current.cwd if current else "")),
            return_code=(
                event.payload.get("return_code")
                if not started
                else current.return_code if current else None
            ),
        )

    @_synchronized
    def ingest(
        self,
        value: StoredEvent | EventEnvelope | ObservationDraft,
        *,
        global_sequence: int | None = None,
        recorded_at: float | None = None,
    ) -> ObservationRecord:
        if isinstance(value, StoredEvent):
            event = value.envelope
            sequence = value.global_sequence
            timestamp = value.recorded_at
        elif isinstance(value, ObservationDraft):
            if global_sequence is None:
                raise ValidationError("draft ingestion requires global_sequence")
            event = value.to_event(
                ProducerRef(
                    id="observation-merger:draft",
                    epoch="local",
                    seq=global_sequence,
                    adapter="observation-draft",
                    mode=ProducerMode.OBSERVED,
                )
            )
            sequence = global_sequence
            timestamp = value.observed_at if recorded_at is None else recorded_at
        else:
            event = value
            if global_sequence is None:
                raise ValidationError("envelope ingestion requires global_sequence")
            sequence = global_sequence
            timestamp = time.time() if recorded_at is None else recorded_at
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValidationError("global_sequence must be a non-negative integer")
        timestamp = float(timestamp)
        fingerprint_value = event.fingerprint()
        existing_fingerprint = self._event_fingerprints.get(event.event_id)
        if existing_fingerprint is not None:
            if existing_fingerprint != fingerprint_value:
                raise IdempotencyConflict(
                    "observation event_id was reused for different contents"
                )
            existing = next(
                record
                for records in self._records.values()
                for record in records
                if record.event_id == event.event_id
            )
            return existing
        existing_event_id = self._global_sequences.get(sequence)
        if existing_event_id is not None and existing_event_id != event.event_id:
            raise IdempotencyConflict(
                "global observation sequence was reused by another event"
            )

        process_fingerprint = self._fingerprint(event)
        candidate_values: list[tuple[str, Any]] = []
        # Passive evidence never owns business lifecycle/outcome fields. Even a
        # forged observed `run.succeeded` remains a record, not a state fact.
        if (
            event.type in _LIFECYCLE_VALUES
            and event.producer.mode is not ProducerMode.OBSERVED
        ):
            candidate_values.append(("lifecycle", _LIFECYCLE_VALUES[event.type]))
        if event.type in {
            "run.started",
            "run.activity.changed",
            "observation.run.activity",
        }:
            activity = str(
                event.payload.get("activity")
                or (RunActivity.UNKNOWN.value if event.type == "run.started" else "")
            )
            try:
                normalized_activity = RunActivity(activity)
            except ValueError as exc:
                raise ValidationError("activity evidence is invalid") from exc
            # A server-derived ``run.started(activity=unknown)`` establishes
            # lifecycle only.  Treating that placeholder as fresh semantic
            # evidence would hide the fact that the last real phase report has
            # expired.
            lifecycle_placeholder = (
                event.type == "run.started"
                and event.producer.mode
                in {ProducerMode.CONTROL, ProducerMode.SYSTEM}
                and normalized_activity is RunActivity.UNKNOWN
            )
            if not lifecycle_placeholder:
                candidate_values.append(("activity", normalized_activity.value))
            if not lifecycle_placeholder and normalized_activity is RunActivity.WAITING:
                try:
                    reason = WaitReason(str(event.payload.get("wait_reason") or ""))
                except ValueError as exc:
                    raise ValidationError(
                        "waiting activity evidence requires wait_reason"
                    ) from exc
                candidate_values.append(("wait_reason", reason.value))
            elif not lifecycle_placeholder:
                # A field-level tombstone prevents an older waiting reason from
                # leaking into a newer coding/testing phase.
                candidate_values.append(("wait_reason", None))
        if event.type == "run.input.requested":
            try:
                reason = WaitReason(
                    str(event.payload.get("wait_reason") or WaitReason.USER_INPUT.value)
                )
            except ValueError as exc:
                raise ValidationError("input request wait_reason is invalid") from exc
            candidate_values.extend(
                (
                    ("activity", RunActivity.WAITING.value),
                    ("wait_reason", reason.value),
                )
            )
        if event.type == "run.input.provided":
            candidate_values.append(("activity", RunActivity.UNKNOWN.value))
        if event.type in {"observation.cwd.changed", "agent.registered"}:
            cwd = event.payload.get("cwd")
            if cwd:
                candidate_values.append(("cwd", str(cwd)))
        if event.type == "agent.registered":
            agent_kind = event.payload.get("agent_kind") or event.payload.get("kind")
            if agent_kind:
                candidate_values.append(("agent_kind", str(agent_kind)))
        if event.type == "observation.pty.signature":
            agent_kind = event.payload.get("agent_kind") or event.payload.get(
                "signature"
            )
            if agent_kind:
                candidate_values.append(("agent_kind", str(agent_kind)))
        is_process_event = event.type in {
            "observation.process.started",
            "observation.process.exited",
        }
        agent_local_observation = (
            (is_process_event or event.type == "observation.pty.signature")
            and event.scope.agent_instance_id is not None
        )
        if agent_local_observation:
            # Keep run_id on the immutable envelope so the fact appears in that
            # Run's timeline, while merging process/banner fields onto the Agent
            # whose identity they actually describe.
            target = ObservationTarget.from_scope(
                EventScope(
                    owner_id=event.scope.owner_id,
                    device_id=event.scope.device_id,
                    terminal_id=event.scope.terminal_id,
                    launch_id=event.scope.launch_id,
                    agent_instance_id=event.scope.agent_instance_id,
                    task_id=event.scope.task_id,
                    assignment_id=event.scope.assignment_id,
                )
            )
        else:
            target = ObservationTarget.from_scope(event.scope)
        if is_process_event:
            if process_fingerprint is None:
                raise ValidationError("process observations require process_fingerprint")
            current_process = self._processes.get(process_fingerprint.instance_id)
            if current_process and current_process.target.key != target.key:
                raise ValidationError(
                    "one process incarnation cannot migrate between observation targets"
                )
            for field_name in ("agent_kind", "cwd"):
                if event.payload.get(field_name):
                    candidate_values.append(
                        (field_name, str(event.payload[field_name]))
                    )

        record = ObservationRecord(
            event_id=event.event_id,
            event_type=event.type,
            target=target,
            global_sequence=sequence,
            recorded_at=timestamp,
            payload=event.payload,
            fingerprint=process_fingerprint,
        )
        self._event_fingerprints[event.event_id] = fingerprint_value
        self._global_sequences[sequence] = event.event_id
        self._targets[target.key] = target
        self._records.setdefault(target.key, []).append(record)
        self._records[target.key].sort(key=lambda item: item.global_sequence)
        for field_name, candidate_value in candidate_values:
            self._add_candidate(
                target=target,
                event=event,
                field_name=field_name,
                value=candidate_value,
                global_sequence=sequence,
                recorded_at=timestamp,
            )
        if is_process_event:
            assert process_fingerprint is not None
            self._observe_process(
                target=target,
                event=event,
                fingerprint=process_fingerprint,
                global_sequence=sequence,
                recorded_at=timestamp,
            )
        return record

    @staticmethod
    def _select(
        field_name: str, candidates: list[FieldCandidate], now: float
    ) -> ResolvedField:
        def priority(candidate: FieldCandidate) -> tuple[int, float, int]:
            return (
                int(candidate.authority),
                candidate.confidence,
                candidate.global_sequence,
            )

        fresh = [candidate for candidate in candidates if candidate.fresh(now)]
        if fresh:
            selected = max(fresh, key=priority)
            return ResolvedField(
                field=field_name,
                value=selected.value,
                source=selected.source,
                authority=selected.authority,
                confidence=selected.confidence,
                global_sequence=selected.global_sequence,
                recorded_at=selected.recorded_at,
                expires_at=selected.expires_at,
                stale=False,
            )
        selected = max(candidates, key=priority)
        return ResolvedField(
            field=field_name,
            value=(RunActivity.UNKNOWN.value if field_name == "activity" else None),
            source="stale",
            authority=selected.authority,
            confidence=selected.confidence,
            global_sequence=selected.global_sequence,
            recorded_at=selected.recorded_at,
            expires_at=selected.expires_at,
            stale=True,
            last_value=selected.value,
        )

    def _process_field(
        self, processes: tuple[ProcessObservation, ...], now: float
    ) -> ResolvedField | None:
        if not processes:
            return None
        live = [process for process in processes if process.alive and process.fresh(now)]
        if live:
            selected = max(
                live, key=lambda item: (item.confidence, item.last_sequence)
            )
            return ResolvedField(
                field="process_alive",
                value=True,
                source="observed",
                authority=FieldAuthority.PROCESS,
                confidence=selected.confidence,
                global_sequence=selected.last_sequence,
                recorded_at=selected.recorded_at,
                expires_at=selected.expires_at,
                stale=False,
            )
        stale_live = [process for process in processes if process.alive]
        if stale_live:
            selected = max(stale_live, key=lambda item: item.last_sequence)
            return ResolvedField(
                field="process_alive",
                value=None,
                source="stale",
                authority=FieldAuthority.PROCESS,
                confidence=selected.confidence,
                global_sequence=selected.last_sequence,
                recorded_at=selected.recorded_at,
                expires_at=selected.expires_at,
                stale=True,
                last_value=True,
            )
        selected = max(processes, key=lambda item: item.last_sequence)
        return ResolvedField(
            field="process_alive",
            value=False,
            source="observed",
            authority=FieldAuthority.PROCESS,
            confidence=selected.confidence,
            global_sequence=selected.last_sequence,
            recorded_at=selected.recorded_at,
            expires_at=None,
            stale=False,
        )

    @_synchronized
    def state_for(
        self,
        *,
        owner_id: str,
        run_id: str | None = None,
        agent_instance_id: str | None = None,
        terminal_id: str | None = None,
        device_id: str | None = None,
        now: float | None = None,
    ) -> MergedObservation:
        scope = EventScope(
            owner_id=owner_id,
            device_id=device_id,
            terminal_id=terminal_id,
            agent_instance_id=agent_instance_id,
            run_id=run_id,
        )
        target = ObservationTarget.from_scope(scope)
        key = target.key
        canonical_target = self._targets.get(key, target)
        timestamp = time.time() if now is None else float(now)
        fields = {
            field_name: self._select(field_name, candidates, timestamp)
            for field_name, candidates in self._candidates.get(key, {}).items()
            if candidates
        }
        processes = tuple(
            sorted(
                (
                    process
                    for process in self._processes.values()
                    if process.target.key == key
                ),
                key=lambda item: (item.last_sequence, item.instance_id),
            )
        )
        process_field = self._process_field(processes, timestamp)
        if process_field:
            fields[process_field.field] = process_field
        records = tuple(self._records.get(key, ()))
        return MergedObservation(
            target=canonical_target,
            fields=fields,
            processes=processes,
            records=records,
        )

    @_synchronized
    def candidates_for(
        self,
        *,
        owner_id: str,
        run_id: str | None = None,
        agent_instance_id: str | None = None,
        terminal_id: str | None = None,
        device_id: str | None = None,
        field_name: str | None = None,
    ) -> tuple[FieldCandidate, ...]:
        """Expose auditable candidates without exposing mutable merger state."""
        target = ObservationTarget.from_scope(
            EventScope(
                owner_id=owner_id,
                device_id=device_id,
                terminal_id=terminal_id,
                agent_instance_id=agent_instance_id,
                run_id=run_id,
            )
        )
        by_field = self._candidates.get(target.key, {})
        if field_name is not None:
            values = list(by_field.get(field_name, ()))
        else:
            values = [
                candidate
                for candidates in by_field.values()
                for candidate in candidates
            ]
        return tuple(
            sorted(
                values,
                key=lambda item: (
                    item.global_sequence,
                    item.field,
                    item.event_id,
                ),
            )
        )

    @_synchronized
    def unattributed(
        self, *, owner_id: str, device_id: str, now: float | None = None
    ) -> MergedObservation:
        """Return device-level evidence that was never guessed onto a terminal."""
        return self.state_for(owner_id=owner_id, device_id=device_id, now=now)

    @_synchronized
    def records(
        self,
        *,
        owner_id: str | None = None,
        unattributed_only: bool = False,
    ) -> tuple[ObservationRecord, ...]:
        values = [record for records in self._records.values() for record in records]
        if owner_id is not None:
            values = [record for record in values if record.target.owner_id == owner_id]
        if unattributed_only:
            values = [record for record in values if record.unattributed]
        return tuple(sorted(values, key=lambda item: item.global_sequence))


# TerminalManager can accept this as a typed callback without importing a
# service implementation. ObservationPublisher is the standard implementation.
ObservationCallback = Callable[[ObservationDraft], Any]
