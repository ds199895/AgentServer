from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import replace
from functools import wraps
from typing import Any, Callable, Iterable, Mapping

from .errors import (
    EntityNotFound,
    IdempotencyConflict,
    InvalidTransition,
    LeaseConflict,
    RelationConstraintError,
    RevisionConflict,
    ValidationError,
)
from .events import EventEnvelope, EventScope, ProducerRef, StoredEvent, new_id
from .models import (
    AgentLifecycle,
    AppendStatus,
    AssignmentLifecycle,
    Command,
    Entity,
    EntityKind,
    Lease,
    ProducerMode,
    Projection,
    RelationKind,
    RunLifecycle,
    TaskLifecycle,
)
from .security import (
    ADAPTER_REPORT_CAPABILITY,
    COMMAND_CAPABILITIES,
    REPORT_CAPABILITIES,
    ReporterClaims,
    ReporterTokenRegistry,
)
from .store import ExecutionStore
from .projector import aggregate_for_event


ACTIVE_RUN_LIFECYCLES = frozenset(
    {RunLifecycle.PENDING.value, RunLifecycle.STARTING.value, RunLifecycle.RUNNING.value}
)
TERMINAL_RUN_LIFECYCLES = frozenset(
    {
        RunLifecycle.SUCCEEDED.value,
        RunLifecycle.FAILED.value,
        RunLifecycle.CANCELLED.value,
        RunLifecycle.LOST.value,
    }
)
ACTIVE_AGENT_LIFECYCLES = frozenset(
    {
        AgentLifecycle.DISCOVERED.value,
        AgentLifecycle.STARTING.value,
        AgentLifecycle.ONLINE.value,
        AgentLifecycle.UNREACHABLE.value,
    }
)

# A runtime credential may report facts about its own execution only.  Task,
# Assignment and Terminal control-plane transitions remain server-authoritative.
RUNTIME_EVENT_TYPES = frozenset(
    {
        "agent.registered",
        "agent.stopping",
        "run.started",
        "run.activity.changed",
        "run.progress.updated",
        "run.input.requested",
        "span.started",
        "span.updated",
        "span.ended",
        "artifact.published",
        "child_run.requested",
        "child_run.observed",
        "child_run.linked",
        "run.succeeded",
        "run.failed",
        "run.cancelled",
    }
)


def _serialized_workflow(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._workflow_lock:
            return method(self, *args, **kwargs)

    return wrapped


class ExecutionService:
    """Composes the event-core atoms into Task, Assignment and Run workflows."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        reporter_tokens: ReporterTokenRegistry | None = None,
        artifact_publisher: Callable[[EventEnvelope], None] | None = None,
        lease_ttl: float = 30.0,
        lost_grace: float = 90.0,
        max_child_runs: int = 8,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.reporter_tokens = reporter_tokens
        self.artifact_publisher = artifact_publisher
        self.lease_ttl = max(5.0, float(lease_ttl))
        self.lost_grace = max(self.lease_ttl, float(lost_grace))
        self.max_child_runs = max(1, min(int(max_child_runs), 256))
        self.clock = clock or time.time
        self._producer_epoch = uuid.uuid4().hex
        self._producer_sequence = 0
        self._producer_lock = threading.Lock()
        self._workflow_lock = threading.RLock()

    def _producer(self, mode: ProducerMode = ProducerMode.CONTROL) -> ProducerRef:
        with self._producer_lock:
            self._producer_sequence += 1
            sequence = self._producer_sequence
        return ProducerRef(
            id="agentserver:execution-service",
            epoch=self._producer_epoch,
            seq=sequence,
            adapter="agentserver",
            version="1",
            mode=mode,
        )

    def projection(
        self, *, owner_id: str, kind: EntityKind | str, entity_id: str
    ) -> Projection | None:
        snapshot = self.store.snapshot(
            owner_id=owner_id,
            aggregate_kind=str(kind),
            aggregate_id=entity_id,
        )
        return snapshot.projections[0] if snapshot.projections else None

    def _revision(self, owner_id: str, kind: EntityKind | str, entity_id: str) -> int:
        projection = self.projection(owner_id=owner_id, kind=kind, entity_id=entity_id)
        return projection.revision if projection else 0

    def _append(
        self,
        event_type: str,
        scope: EventScope,
        *,
        payload: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
        mode: ProducerMode = ProducerMode.CONTROL,
        event_id: str | None = None,
        causation_id: str | None = None,
    ):
        return self.store.append(
            self._event(
                event_type,
                scope,
                payload=payload,
                expected_revision=expected_revision,
                mode=mode,
                event_id=event_id,
                causation_id=causation_id,
            )
        )

    def _event(
        self,
        event_type: str,
        scope: EventScope,
        *,
        payload: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
        mode: ProducerMode = ProducerMode.CONTROL,
        event_id: str | None = None,
        causation_id: str | None = None,
    ) -> EventEnvelope:
        return EventEnvelope(
            type=event_type,
            event_id=event_id or new_id(),
            scope=scope,
            producer=self._producer(mode),
            payload=payload or {},
            expected_revision=expected_revision,
            causation_id=causation_id,
            correlation_id=scope.task_id,
        )

    @staticmethod
    def _idempotent_id(owner_id: str, namespace: str, key: str) -> str:
        return hashlib.sha256(
            f"{namespace}\0{owner_id}\0{key}".encode("utf-8")
        ).hexdigest()[:32]

    def register_terminal(
        self,
        *,
        owner_id: str,
        terminal_id: str | None = None,
        launch_id: str | None = None,
        device_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key is not None:
            idempotency_key = str(idempotency_key).strip()
            if not 1 <= len(idempotency_key) <= 255:
                raise ValidationError(
                    "idempotency_key must contain 1..255 characters"
                )
            resolved_terminal_id = self._idempotent_id(
                owner_id, "terminal", idempotency_key
            )
            resolved_launch_id = self._idempotent_id(
                owner_id, "terminal-launch", idempotency_key
            )
            terminal_id = terminal_id or resolved_terminal_id
            launch_id = launch_id or resolved_launch_id
        if not terminal_id or not launch_id:
            raise ValidationError(
                "terminal_id and launch_id are required without idempotency_key"
            )
        values = {
            **dict(attributes or {}),
            "launch_id": launch_id,
            "device_id": device_id,
            "managed": True,
            "origin": "agentserver",
            **({"idempotency_key": idempotency_key} if idempotency_key else {}),
        }
        entity, _projection = self.store.register_entity_with_initial_event(
            owner_id=owner_id,
            kind=EntityKind.TERMINAL,
            entity_id=terminal_id,
            attributes=values,
            event=self._event(
                "terminal.launch.requested",
                EventScope(
                    owner_id=owner_id,
                    device_id=device_id,
                    terminal_id=terminal_id,
                    launch_id=launch_id,
                ),
                payload={"origin": "agentserver"},
                expected_revision=0,
            ),
        )
        return entity.as_dict()

    def terminal_ready(
        self, *, owner_id: str, terminal_id: str, recovered: bool = False
    ) -> Projection:
        entity = self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        if entity is None:
            raise EntityNotFound("terminal is not registered")
        projection = self.projection(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        if projection and projection.state.get("lifecycle") == "ready":
            return projection
        result = self._append(
            "terminal.ready",
            EventScope(
                owner_id=owner_id,
                device_id=entity.attributes.get("device_id"),
                terminal_id=terminal_id,
                launch_id=str(entity.attributes.get("launch_id") or "") or None,
            ),
            payload={"recovered": recovered},
            expected_revision=projection.revision if projection else 0,
        )
        assert result.projection is not None
        return result.projection

    def terminal_connecting(
        self, *, owner_id: str, terminal_id: str
    ) -> Projection:
        """Record that a registered launch has crossed into its OS transport.

        Launching a PTY/SSH process is an external side effect, so registration
        deliberately leaves the Terminal at ``requested``.  Callers move it to
        ``connecting`` immediately before starting that side effect and only
        report ``ready`` after an exec handshake or positive PTY evidence.
        """
        entity = self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        if entity is None:
            raise EntityNotFound("terminal is not registered")
        projection = self.projection(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        if projection and projection.state.get("lifecycle") in {
            "connecting",
            "ready",
        }:
            return projection
        result = self._append(
            "terminal.connecting",
            EventScope(
                owner_id=owner_id,
                device_id=entity.attributes.get("device_id"),
                terminal_id=terminal_id,
                launch_id=str(entity.attributes.get("launch_id") or "") or None,
            ),
            expected_revision=projection.revision if projection else 0,
        )
        assert result.projection is not None
        return result.projection

    def terminal_launch_failed(
        self, *, owner_id: str, terminal_id: str, summary: str
    ) -> Projection:
        entity = self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        if entity is None:
            raise EntityNotFound("terminal is not registered")
        revision = self._revision(owner_id, EntityKind.TERMINAL, terminal_id)
        result = self._append(
            "terminal.launch.failed",
            EventScope(
                owner_id=owner_id,
                device_id=entity.attributes.get("device_id"),
                terminal_id=terminal_id,
                launch_id=str(entity.attributes.get("launch_id") or "") or None,
            ),
            payload={"summary": str(summary)[:2000]},
            expected_revision=revision,
        )
        assert result.projection is not None
        return result.projection

    def terminal_exited(
        self, *, owner_id: str, terminal_id: str, return_code: int | None = None
    ) -> Projection | None:
        entity = self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        projection = self.projection(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        if entity is None or projection is None:
            return None
        if projection.state.get("lifecycle") in {"exited", "failed"}:
            return projection
        result = self._append(
            "terminal.exited",
            EventScope(
                owner_id=owner_id,
                device_id=entity.attributes.get("device_id"),
                terminal_id=terminal_id,
                launch_id=str(entity.attributes.get("launch_id") or "") or None,
            ),
            payload={"return_code": return_code},
            expected_revision=projection.revision,
            mode=ProducerMode.SYSTEM,
        )
        return result.projection

    def create_task(
        self,
        *,
        owner_id: str,
        title: str,
        description: str = "",
        context_id: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        parent_run_id: str | None = None,
        retry_of_task_id: str | None = None,
        deadline_at: float | None = None,
        token_budget: int | None = None,
        cost_budget_micros: int | None = None,
        max_child_runs: int | None = None,
        cancel_propagates: bool = True,
    ) -> dict[str, Any]:
        title = str(title).strip()
        if not title or len(title) > 240:
            raise ValidationError("task title must contain 1..240 characters")
        if len(description) > 16_000:
            raise ValidationError("task description must not exceed 16000 characters")
        if deadline_at is not None and float(deadline_at) <= time.time():
            raise ValidationError("task deadline must be in the future")
        for label, value in (
            ("token_budget", token_budget),
            ("cost_budget_micros", cost_budget_micros),
        ):
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValidationError(f"{label} must be a positive integer")
        if max_child_runs is not None and (
            not isinstance(max_child_runs, int)
            or isinstance(max_child_runs, bool)
            or not 1 <= max_child_runs <= 256
        ):
            raise ValidationError("max_child_runs must be between 1 and 256")
        if idempotency_key is not None:
            idempotency_key = str(idempotency_key).strip()
            if not 1 <= len(idempotency_key) <= 255:
                raise ValidationError(
                    "idempotency_key must contain 1..255 characters"
                )
        inherited: Mapping[str, Any] = {}
        if parent_run_id:
            parent = self.store.get_entity(
                owner_id=owner_id,
                kind=EntityKind.RUN,
                entity_id=parent_run_id,
            )
            parent_projection = self.projection(
                owner_id=owner_id,
                kind=EntityKind.RUN,
                entity_id=parent_run_id,
            )
            if parent is None or parent_projection is None:
                raise EntityNotFound("parent Run does not exist")
            if parent_projection.state.get("lifecycle") not in ACTIVE_RUN_LIFECYCLES:
                raise InvalidTransition(
                    "run", parent_projection.state.get("lifecycle"), "create_child"
                )
            inherited = parent.attributes
            parent_deadline = inherited.get("deadline_at")
            if parent_deadline is not None:
                if deadline_at is None:
                    deadline_at = float(parent_deadline)
                elif float(deadline_at) > float(parent_deadline):
                    raise ValidationError("child deadline cannot exceed parent deadline")
            for label, value in (
                ("token_budget", token_budget),
                ("cost_budget_micros", cost_budget_micros),
            ):
                parent_value = inherited.get(label)
                if parent_value is not None and value is None:
                    raise ValidationError(
                        f"child {label} must explicitly reserve part of parent budget"
                    )
                if (
                    parent_value is not None
                    and value is not None
                    and value > int(parent_value)
                ):
                    raise ValidationError(f"child {label} cannot exceed parent budget")
            parent_max = int(
                inherited.get("max_child_runs") or self.max_child_runs
            )
            max_child_runs = min(max_child_runs or parent_max, parent_max)
        if idempotency_key:
            resolved_task_id = self._idempotent_id(
                owner_id, "task", idempotency_key
            )
            task_id = task_id or resolved_task_id
        elif task_id is None:
            task_id = new_id()
        attributes = {
            "title": title,
            "description": description,
            "context_id": context_id or task_id,
            "parent_run_id": parent_run_id,
            "retry_of_task_id": retry_of_task_id,
            "idempotency_key": idempotency_key,
            "deadline_at": float(deadline_at) if deadline_at is not None else None,
            "token_budget": token_budget,
            "cost_budget_micros": cost_budget_micros,
            "max_child_runs": max_child_runs or self.max_child_runs,
            "cancel_propagates": bool(cancel_propagates),
        }
        _entity, projection = self.store.register_entity_with_initial_event(
            owner_id=owner_id,
            kind=EntityKind.TASK,
            entity_id=task_id,
            attributes=attributes,
            event=self._event(
                "task.created",
                EventScope(
                    owner_id=owner_id,
                    task_id=task_id,
                    parent_run_id=parent_run_id,
                ),
                payload={"title": title},
                expected_revision=0,
            ),
        )
        return self._projection_view(projection)

    def _assignment_preflight(
        self,
        *,
        owner_id: str,
        task_id: str,
        expected_task_revision: int,
        parent_run_id: str | None,
        terminal_id: str | None,
    ) -> tuple[Entity, Entity | None, Projection]:
        task = self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.TASK, entity_id=task_id
        )
        if task is None:
            raise EntityNotFound("task does not exist in the owner scope")
        current_task = self.projection(
            owner_id=owner_id, kind=EntityKind.TASK, entity_id=task_id
        )
        if current_task is None:
            raise EntityNotFound("task projection does not exist")
        if current_task.revision != expected_task_revision:
            raise RevisionConflict(
                expected_task_revision,
                current_task.revision,
                dict(current_task.state),
            )
        if current_task.state.get("lifecycle") != TaskLifecycle.SUBMITTED.value:
            raise InvalidTransition(
                "task", current_task.state.get("lifecycle"), "assigned"
            )
        terminal = None
        if terminal_id is not None:
            terminal = self.store.get_entity(
                owner_id=owner_id,
                kind=EntityKind.TERMINAL,
                entity_id=terminal_id,
            )
            if terminal is None:
                raise EntityNotFound(
                    "task and terminal must exist in the same owner scope"
                )
            terminal_projection = self.projection(
                owner_id=owner_id,
                kind=EntityKind.TERMINAL,
                entity_id=terminal_id,
            )
            if (
                terminal_projection is None
                or terminal_projection.state.get("lifecycle") != "ready"
            ):
                raise InvalidTransition(
                    "terminal",
                    (
                        terminal_projection.state.get("lifecycle")
                        if terminal_projection
                        else None
                    ),
                    "assign",
                )
            if not str(terminal.attributes.get("launch_id") or ""):
                raise ValidationError(
                    "task assignment requires a managed terminal launch"
                )
        if parent_run_id:
            self.store.validate_new_child_run(
                owner_id=owner_id, parent_run_id=parent_run_id
            )
            if str(task.attributes.get("parent_run_id") or "") != parent_run_id:
                raise RelationConstraintError(
                    "child Task and Assignment must name the same parent Run"
                )
            parent = self.store.get_entity(
                owner_id=owner_id,
                kind=EntityKind.RUN,
                entity_id=parent_run_id,
            )
            parent_projection = self.projection(
                owner_id=owner_id,
                kind=EntityKind.RUN,
                entity_id=parent_run_id,
            )
            if parent is None or parent_projection is None:
                raise EntityNotFound("parent Run does not exist")
            if parent_projection.state.get("lifecycle") not in ACTIVE_RUN_LIFECYCLES:
                raise InvalidTransition(
                    "run", parent_projection.state.get("lifecycle"), "assign_child"
                )
            child_relations = self.store.relations(
                owner_id=owner_id,
                relation=RelationKind.PARENT_RUN,
                source_kind=EntityKind.RUN,
                source_id=parent_run_id,
                target_kind=EntityKind.RUN,
            )
            child_runs = [
                self.projection(
                    owner_id=owner_id,
                    kind=EntityKind.RUN,
                    entity_id=relation.target_id,
                )
                for relation in child_relations
            ]
            active_children = sum(
                1
                for child in child_runs
                if child and child.state.get("lifecycle") in ACTIVE_RUN_LIFECYCLES
            )
            maximum = int(
                parent.attributes.get("max_child_runs") or self.max_child_runs
            )
            if active_children >= maximum:
                raise RelationConstraintError(
                    "parent Run active child concurrency limit was reached"
                )
            deadline = task.attributes.get("deadline_at")
            if deadline is not None and float(deadline) <= time.time():
                raise ValidationError("child Task deadline has expired")
            for label in ("token_budget", "cost_budget_micros"):
                limit = parent.attributes.get(label)
                requested = task.attributes.get(label)
                if limit is None:
                    continue
                allocated = 0
                for relation in child_relations:
                    child_entity = self.store.get_entity(
                        owner_id=owner_id,
                        kind=EntityKind.RUN,
                        entity_id=relation.target_id,
                    )
                    if child_entity is not None:
                        allocated += int(child_entity.attributes.get(label) or 0)
                if requested is None or allocated + int(requested) > int(limit):
                    raise RelationConstraintError(
                        f"parent Run {label} allocation would be exceeded"
                    )
        return task, terminal, current_task

    def preflight_assignment(
        self,
        *,
        owner_id: str,
        task_id: str,
        expected_task_revision: int,
        parent_run_id: str | None = None,
        terminal_id: str | None = None,
    ) -> dict[str, Any]:
        """Perform a read-only assignment check before external terminal launch."""
        _task, _terminal, projection = self._assignment_preflight(
            owner_id=owner_id,
            task_id=task_id,
            expected_task_revision=expected_task_revision,
            parent_run_id=parent_run_id,
            terminal_id=terminal_id,
        )
        return self._projection_view(projection)

    @_serialized_workflow
    def assign_task(
        self,
        *,
        owner_id: str,
        task_id: str,
        terminal_id: str,
        agent_kind: str,
        expected_task_revision: int,
        device_id: str | None = None,
        assignment_id: str | None = None,
        run_id: str | None = None,
        agent_instance_id: str | None = None,
        parent_run_id: str | None = None,
        attempt: int = 1,
        lease_ttl: float | None = None,
    ) -> dict[str, Any]:
        agent_kind = str(agent_kind).strip()
        if not 1 <= len(agent_kind) <= 40:
            raise ValidationError("agent_kind must contain 1..40 characters")
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
        ):
            raise ValidationError("attempt must be a positive integer")
        task, terminal, _current_task = self._assignment_preflight(
            owner_id=owner_id,
            task_id=task_id,
            expected_task_revision=expected_task_revision,
            parent_run_id=parent_run_id,
            terminal_id=terminal_id,
        )
        assert terminal is not None
        assignment_id = assignment_id or new_id()
        run_id = run_id or new_id()
        agent_instance_id = agent_instance_id or new_id()
        for kind, identifier in (
            (EntityKind.ASSIGNMENT, assignment_id),
            (EntityKind.RUN, run_id),
            (EntityKind.AGENT_INSTANCE, agent_instance_id),
        ):
            if self.store.get_entity(
                owner_id=owner_id, kind=kind, entity_id=identifier
            ) is not None:
                raise IdempotencyConflict(
                    f"{kind.value} identifier is already registered"
                )
        launch_id = str(terminal.attributes.get("launch_id") or "")
        device_id = device_id or terminal.attributes.get("device_id")
        common = {
            "task_id": task_id,
            "terminal_id": terminal_id,
            "launch_id": launch_id,
            "device_id": device_id,
            "agent_kind": agent_kind,
            "agent_instance_id": agent_instance_id,
            "parent_run_id": parent_run_id,
            "attempt": attempt,
            "deadline_at": task.attributes.get("deadline_at"),
            "token_budget": task.attributes.get("token_budget"),
            "cost_budget_micros": task.attributes.get("cost_budget_micros"),
            "max_child_runs": task.attributes.get("max_child_runs")
            or self.max_child_runs,
            "cancel_propagates": bool(
                task.attributes.get("cancel_propagates", True)
            ),
        }
        assignment_attributes = {**common, "run_id": run_id}
        run_attributes = {**common, "assignment_id": assignment_id}
        agent_attributes = {
            "kind": agent_kind,
            "terminal_id": terminal_id,
            "launch_id": launch_id,
            "device_id": device_id,
            "run_id": run_id,
        }
        scope = EventScope(
            owner_id=owner_id,
            device_id=device_id,
            terminal_id=terminal_id,
            launch_id=launch_id,
            agent_instance_id=agent_instance_id,
            task_id=task_id,
            assignment_id=assignment_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )
        events = (
            self._event(
                "assignment.created",
                scope,
                payload={"agent_kind": agent_kind},
                expected_revision=0,
            ),
            self._event(
                "run.requested",
                scope,
                payload={"attempt": attempt, "activity": "idle"},
                expected_revision=0,
            ),
            self._event(
                "task.assigned",
                scope,
                payload={"assignment_id": assignment_id, "run_id": run_id},
                expected_revision=expected_task_revision,
            ),
        )
        self.store.commit_assignment(
            owner_id=owner_id,
            task_id=task_id,
            terminal_id=terminal_id,
            assignment_id=assignment_id,
            run_id=run_id,
            agent_instance_id=agent_instance_id,
            expected_task_revision=expected_task_revision,
            assignment_attributes=assignment_attributes,
            run_attributes=run_attributes,
            agent_attributes=agent_attributes,
            events=events,
            parent_run_id=parent_run_id,
            lease_ttl=max(float(lease_ttl or self.lost_grace), self.lost_grace),
            default_max_child_runs=self.max_child_runs,
        )
        return self.get_task(owner_id=owner_id, task_id=task_id)

    def _scope_for_run(self, owner_id: str, run_id: str) -> EventScope:
        run = self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id
        )
        if run is None:
            raise EntityNotFound("run does not exist")
        values = run.attributes
        return EventScope(
            owner_id=owner_id,
            device_id=values.get("device_id"),
            terminal_id=values.get("terminal_id"),
            launch_id=values.get("launch_id") or None,
            agent_instance_id=values.get("agent_instance_id"),
            task_id=values.get("task_id"),
            assignment_id=values.get("assignment_id"),
            run_id=run_id,
            parent_run_id=values.get("parent_run_id"),
        )

    def record_observation(self, event: EventEnvelope):
        """Persist passive evidence and maintain only Agent identity lifecycle.

        Process disappearance may close an AgentInstance, but this method never
        completes a Run.  Semantic fields are resolved from the immutable
        evidence ledger at read time by ``ObservationMerger``.
        """
        if not event.type.startswith("observation."):
            raise ValidationError("observation sink accepts observation.* only")
        if event.producer.mode not in {ProducerMode.OBSERVED, ProducerMode.ADAPTER}:
            raise ValidationError("observation sink requires an observed producer")
        result = self.store.append(event)
        if result.status is AppendStatus.DUPLICATE:
            return result
        agent_id = event.scope.agent_instance_id
        if not agent_id:
            return result
        entity = self.store.get_entity(
            owner_id=event.scope.owner_id,
            kind=EntityKind.AGENT_INSTANCE,
            entity_id=agent_id,
        )
        if entity is None:
            self.store.register_entity(
                owner_id=event.scope.owner_id,
                kind=EntityKind.AGENT_INSTANCE,
                entity_id=agent_id,
                attributes={
                    "kind": event.payload.get("agent_kind"),
                    "device_id": event.scope.device_id,
                    "terminal_id": event.scope.terminal_id,
                    "launch_id": event.scope.launch_id,
                    "cwd": event.payload.get("cwd") or "",
                    "observed": True,
                },
            )
            if event.scope.terminal_id and self.store.get_entity(
                owner_id=event.scope.owner_id,
                kind=EntityKind.TERMINAL,
                entity_id=event.scope.terminal_id,
            ):
                self.store.link_entities(
                    owner_id=event.scope.owner_id,
                    relation=RelationKind.BOUND_TO,
                    source_kind=EntityKind.AGENT_INSTANCE,
                    source_id=agent_id,
                    target_kind=EntityKind.TERMINAL,
                    target_id=event.scope.terminal_id,
                )
        projection = self.projection(
            owner_id=event.scope.owner_id,
            kind=EntityKind.AGENT_INSTANCE,
            entity_id=agent_id,
        )
        if event.type in {
            "observation.process.started",
            "observation.pty.signature",
        } and projection is None:
            self._append(
                "agent.discovered",
                event.scope,
                payload={
                    "kind": event.payload.get("agent_kind"),
                    "source": "observed",
                },
                expected_revision=0,
                mode=ProducerMode.SYSTEM,
                causation_id=event.event_id,
            )
        elif (
            event.type == "observation.process.exited"
            and projection
            and projection.state.get("lifecycle")
            in {
                AgentLifecycle.DISCOVERED.value,
                AgentLifecycle.STARTING.value,
                AgentLifecycle.ONLINE.value,
                AgentLifecycle.STOPPING.value,
                AgentLifecycle.UNREACHABLE.value,
            }
        ):
            self._append(
                "agent.exited",
                event.scope,
                payload={"source": "observed"},
                expected_revision=projection.revision,
                mode=ProducerMode.SYSTEM,
                causation_id=event.event_id,
            )
        return result

    def issue_reporter_token(
        self,
        *,
        owner_id: str,
        run_id: str,
        ttl: int | None = None,
        capabilities: Iterable[str] = REPORT_CAPABILITIES,
    ) -> str:
        if self.reporter_tokens is None:
            raise RuntimeError("reporter token registry is not configured")
        scope = self._scope_for_run(owner_id, run_id)
        projection = self.projection(
            owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id
        )
        if projection is None or projection.state.get("lifecycle") not in ACTIVE_RUN_LIFECYCLES:
            raise InvalidTransition(
                "run",
                projection.state.get("lifecycle") if projection else None,
                "issue_reporter_token",
            )
        return self.reporter_tokens.issue(
            owner_id=owner_id,
            run_id=run_id,
            terminal_id=scope.terminal_id or "",
            launch_id=scope.launch_id or "",
            device_id=scope.device_id,
            agent_instance_id=scope.agent_instance_id,
            capabilities=capabilities,
            ttl=ttl,
        )

    def issue_bridge_tokens(
        self, *, owner_id: str, run_id: str, ttl: int | None = None
    ) -> dict[str, str]:
        return {
            "report_token": self.issue_reporter_token(
                owner_id=owner_id,
                run_id=run_id,
                ttl=ttl,
                capabilities=REPORT_CAPABILITIES | {ADAPTER_REPORT_CAPABILITY},
            ),
            "command_token": self.issue_reporter_token(
                owner_id=owner_id,
                run_id=run_id,
                ttl=ttl,
                capabilities=COMMAND_CAPABILITIES,
            ),
        }

    def _scope_for_claims(self, claims: ReporterClaims) -> EventScope:
        canonical = self._scope_for_run(claims.owner_id, claims.run_id)
        for field, value in {
            "owner_id": claims.owner_id,
            "run_id": claims.run_id,
            "terminal_id": claims.terminal_id,
            "launch_id": claims.launch_id,
            "device_id": claims.device_id,
            "agent_instance_id": claims.agent_instance_id,
        }.items():
            if value != getattr(canonical, field):
                raise ValidationError(
                    f"reporter token {field} does not match canonical Run scope"
                )
        return canonical

    @_serialized_workflow
    def refresh_runtime_token(self, *, claims: ReporterClaims) -> dict[str, Any]:
        """Rotate one scoped credential while the assignment lease is active."""

        if self.reporter_tokens is None:
            raise RuntimeError("reporter token registry is not configured")
        scope = self._scope_for_claims(claims)
        projection = self.projection(
            owner_id=claims.owner_id,
            kind=EntityKind.RUN,
            entity_id=claims.run_id,
        )
        if (
            projection is None
            or projection.state.get("lifecycle") not in ACTIVE_RUN_LIFECYCLES
        ):
            raise InvalidTransition(
                "run",
                projection.state.get("lifecycle") if projection else None,
                "refresh_token",
            )
        self._require_active_assignment_lease(scope)
        now = int(self.clock())
        token = self.reporter_tokens.refresh(claims, now=now)
        replacement = self.reporter_tokens.signer.verify(
            token, now=now, clock_skew=0
        )
        return {
            "token": token,
            "token_type": "Bearer",
            "expires_at": replacement.expires_at,
        }

    def authorize_runtime_command_access(
        self, *, claims: ReporterClaims, command: Command | None = None
    ) -> EventScope:
        """Fence command delivery/ACK to the current Run assignment."""

        if not (claims.permits("commands") or claims.permits("ack")):
            raise ValidationError("reporter token has no command authority")
        scope = self._scope_for_claims(claims)
        projection = self.projection(
            owner_id=claims.owner_id,
            kind=EntityKind.RUN,
            entity_id=claims.run_id,
        )
        if (
            projection is None
            or projection.state.get("lifecycle") not in ACTIVE_RUN_LIFECYCLES
        ):
            raise InvalidTransition(
                "run",
                projection.state.get("lifecycle") if projection else None,
                "runtime_commands",
            )
        lease = self._active_assignment_lease(scope)
        if command is not None:
            expected_payload = {
                "run_id": scope.run_id or "",
                "assignment_id": scope.assignment_id or "",
                "terminal_id": scope.terminal_id or "",
                "launch_id": scope.launch_id or "",
                "terminal_lease_id": lease.id,
            }
            if (
                command.owner_id != claims.owner_id
                or command.target_kind != EntityKind.AGENT_INSTANCE.value
                or command.target_id != (scope.agent_instance_id or "")
            ):
                raise CommandConflict("command is outside reporter agent scope")
            for name, value in expected_payload.items():
                if str(command.payload.get(name) or "") != value:
                    raise CommandConflict(
                        f"command {name} fence is outside the active assignment"
                    )
            fence_revision = command.payload.get("terminal_lease_revision")
            if (
                not isinstance(fence_revision, int)
                or isinstance(fence_revision, bool)
                or fence_revision < 1
                or lease.revision < fence_revision
            ):
                raise CommandConflict("command terminal lease revision is invalid")
            if (
                command.expected_revision is not None
                and projection.revision < command.expected_revision
            ):
                raise CommandConflict("command Run revision is not yet visible")
        return scope

    def _require_claim_scope(
        self, event: EventEnvelope, claims: ReporterClaims
    ) -> None:
        canonical = self._scope_for_claims(claims)
        for field in (
            "owner_id",
            "device_id",
            "terminal_id",
            "launch_id",
            "agent_instance_id",
            "task_id",
            "assignment_id",
            "run_id",
            "parent_run_id",
        ):
            if getattr(event.scope, field) != getattr(canonical, field):
                raise ValidationError(
                    f"runtime event {field} does not match canonical Run scope"
                )

    @staticmethod
    def runtime_producer_id(
        *, claims: ReporterClaims, reported_producer_id: str
    ) -> str:
        """Namespace caller-controlled producer identities to one owner/Run.

        SQLite's producer idempotency key is intentionally global.  A runtime
        caller must therefore never be able to collide with another owner or
        Run merely by guessing its producer id/epoch/sequence tuple.
        """
        digest = hashlib.sha256(
            f"{claims.owner_id}\0{claims.run_id}\0{reported_producer_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        return f"runtime:{digest}"

    def _active_assignment_lease(
        self, scope: EventScope, *, now: float | None = None
    ) -> Lease:
        if not scope.terminal_id or not scope.assignment_id:
            raise ValidationError("runtime Run has no terminal assignment")
        lease = self.store.get_lease(
            owner_id=scope.owner_id,
            resource_kind=EntityKind.TERMINAL,
            resource_id=scope.terminal_id,
            now=now,
        )
        if lease is None or lease.holder_id != scope.assignment_id:
            raise LeaseConflict("runtime Run no longer owns its terminal lease")
        lease_run_id = str(lease.metadata.get("run_id") or "")
        if lease_run_id and lease_run_id != str(scope.run_id or ""):
            raise LeaseConflict("runtime Run no longer owns its terminal lease")
        return lease

    def _require_active_assignment_lease(self, scope: EventScope) -> None:
        self._active_assignment_lease(scope)

    def _require_declared_child_relation(
        self, *, owner_id: str, parent_run_id: str, child_run_id: str
    ) -> None:
        """Accept only control-plane-created child links.

        Runtime reporters may acknowledge a delegation relation but may not
        create one after the fact.  Child limits, deadlines, budgets, terminal
        ownership and the declared parent are all validated atomically by
        ``commit_assignment`` before the relation becomes visible.
        """

        child = self.store.get_entity(
            owner_id=owner_id,
            kind=EntityKind.RUN,
            entity_id=child_run_id,
        )
        if child is None:
            raise EntityNotFound("child Run does not exist")
        if str(child.attributes.get("parent_run_id") or "") != parent_run_id:
            raise RelationConstraintError(
                "runtime may link only a Run declared for this parent"
            )
        relations = self.store.relations(
            owner_id=owner_id,
            relation=RelationKind.PARENT_RUN,
            source_kind=EntityKind.RUN,
            source_id=parent_run_id,
            target_kind=EntityKind.RUN,
            target_id=child_run_id,
        )
        if not relations:
            raise RelationConstraintError(
                "child Run relation must be created by the control plane"
            )

    def _activate_runtime_assignment(
        self, scope: EventScope, *, claims: ReporterClaims, causation_id: str
    ) -> None:
        run_projection = self.projection(
            owner_id=scope.owner_id,
            kind=EntityKind.RUN,
            entity_id=scope.run_id or "",
        )
        if (
            run_projection is None
            or run_projection.state.get("lifecycle") in TERMINAL_RUN_LIFECYCLES
        ):
            return
        if not scope.agent_instance_id or not scope.terminal_id or not scope.assignment_id:
            raise ValidationError("runtime assignment scope is incomplete")
        self.store.heartbeat_leases(
            owner_id=scope.owner_id,
            run_id=scope.run_id or "",
            agent_instance_id=scope.agent_instance_id,
            agent_holder_id=claims.token_id,
            agent_ttl_seconds=self.lease_ttl,
            terminal_id=scope.terminal_id,
            assignment_id=scope.assignment_id,
            terminal_ttl_seconds=self.lost_grace,
        )
        if run_projection.state.get("lifecycle") in {
            RunLifecycle.PENDING.value,
            RunLifecycle.STARTING.value,
        }:
            try:
                self._append(
                    "run.started",
                    scope,
                    payload={"activity": "unknown"},
                    expected_revision=run_projection.revision,
                    mode=ProducerMode.SYSTEM,
                    causation_id=causation_id,
                )
            except RevisionConflict:
                concurrent = self.projection(
                    owner_id=scope.owner_id,
                    kind=EntityKind.RUN,
                    entity_id=scope.run_id or "",
                )
                if (
                    concurrent is None
                    or concurrent.state.get("lifecycle") != RunLifecycle.RUNNING.value
                ):
                    raise
        self._accept_assignment(scope, causation_id=causation_id)

    def _ensure_adapter_registered(
        self, scope: EventScope, *, claims: ReporterClaims, causation_id: str
    ) -> None:
        if not scope.agent_instance_id:
            raise ValidationError("runtime assignment has no Agent instance")
        agent = self.projection(
            owner_id=scope.owner_id,
            kind=EntityKind.AGENT_INSTANCE,
            entity_id=scope.agent_instance_id,
        )
        if agent is None:
            try:
                self._append(
                    "agent.registered",
                    scope,
                    payload={"kind": "adapter"},
                    expected_revision=0,
                    mode=ProducerMode.SYSTEM,
                    causation_id=causation_id,
                )
            except RevisionConflict:
                agent = self.projection(
                    owner_id=scope.owner_id,
                    kind=EntityKind.AGENT_INSTANCE,
                    entity_id=scope.agent_instance_id,
                )
                if agent is None:
                    raise
        self._activate_runtime_assignment(
            scope, claims=claims, causation_id=causation_id
        )

    @_serialized_workflow
    def ingest_runtime_event(
        self, event: EventEnvelope, *, claims: ReporterClaims
    ):
        if event.type not in RUNTIME_EVENT_TYPES:
            raise ValidationError(f"runtime event type is not allowed: {event.type}")
        if event.producer.mode not in {ProducerMode.ACTIVE, ProducerMode.ADAPTER}:
            raise ValidationError("runtime producer mode must be active or adapter")
        adapter_authority = claims.permits(ADAPTER_REPORT_CAPABILITY)
        if (event.producer.mode is ProducerMode.ADAPTER) != adapter_authority:
            raise ValidationError(
                "reporter token producer authority does not match event mode"
            )
        self._require_claim_scope(event, claims)
        if not claims.permits("report"):
            raise ValidationError("reporter token does not permit report")
        event = replace(
            event,
            producer=replace(
                event.producer,
                id=self.runtime_producer_id(
                    claims=claims, reported_producer_id=event.producer.id
                ),
            ),
        )

        current_run = self.projection(
            owner_id=claims.owner_id,
            kind=EntityKind.RUN,
            entity_id=claims.run_id,
        )
        if event.type in {"run.succeeded", "run.failed", "run.cancelled"}:
            if current_run and current_run.state.get("lifecycle") == RunLifecycle.LOST.value:
                conflict = replace(
                    event,
                    type="state.conflict.detected",
                    expected_revision=None,
                    payload={
                        "reason": "late_terminal_event_after_lost",
                        "reported_type": event.type,
                        "reported_payload": dict(event.payload),
                        "lost_revision": current_run.revision,
                    },
                )
                return self._commit_runtime_event(conflict, claims=claims)

        if current_run and current_run.state.get("lifecycle") in TERMINAL_RUN_LIFECYCLES:
            history = self.store.snapshot(owner_id=claims.owner_id).events
            existing = next(
                (
                    item
                    for item in history
                    if item.id == event.event_id
                    or (
                        item.producer.id == event.producer.id
                        and item.producer.epoch == event.producer.epoch
                        and item.producer.seq == event.producer.seq
                    )
                ),
                None,
            )
            if existing is not None:
                if (
                    replace(existing.envelope, expected_revision=None).fingerprint()
                    != replace(event, expected_revision=None).fingerprint()
                ):
                    return self.store.append(event, require_effect_ack=True)
                return self._commit_runtime_event(existing.envelope, claims=claims)
            raise InvalidTransition(
                "run", current_run.state.get("lifecycle"), event.type
            )

        canonical_scope = self._scope_for_run(claims.owner_id, claims.run_id)
        self._require_active_assignment_lease(canonical_scope)

        # A trusted provider adapter can miss SessionStart when a terminal was
        # assigned after the provider process began.  Its first authenticated
        # runtime fact is enough to register the canonical Agent and activate
        # the already-created assignment; native ACTIVE reporters still require
        # an explicit agent.registered event and strict CAS.
        if (
            event.producer.mode is ProducerMode.ADAPTER
            and event.type not in {"agent.registered", "agent.stopping"}
            and current_run is not None
            and current_run.state.get("lifecycle")
            in {RunLifecycle.PENDING.value, RunLifecycle.STARTING.value}
        ):
            self._ensure_adapter_registered(
                canonical_scope, claims=claims, causation_id=event.event_id
            )
            current_run = self.projection(
                owner_id=claims.owner_id,
                kind=EntityKind.RUN,
                entity_id=claims.run_id,
            )

        if event.type == "child_run.linked":
            child_run_id = str(event.payload.get("child_run_id") or "")
            if not child_run_id:
                raise ValidationError("child_run.linked requires child_run_id")
            self._require_declared_child_relation(
                owner_id=claims.owner_id,
                parent_run_id=claims.run_id,
                child_run_id=child_run_id,
            )
        elif event.type == "child_run.requested":
            delegation_id = str(event.payload.get("delegation_id") or "")
            if not delegation_id or len(delegation_id) > 255:
                raise ValidationError(
                    "child_run.requested requires a bounded delegation_id"
                )

        # Older reporters did not know the aggregate revision.  The authenticated
        # ingest boundary may fill that value explicitly, while direct core users
        # still get strict CAS.  Replays are normalized to the already-persisted
        # envelope first so at-least-once delivery remains idempotent after the
        # stream has advanced.
        if event.expected_revision is None:
            history = self.store.snapshot(owner_id=claims.owner_id).events
            existing = next(
                (
                    item
                    for item in history
                    if item.id == event.event_id
                    or (
                        item.producer.id == event.producer.id
                        and item.producer.epoch == event.producer.epoch
                        and item.producer.seq == event.producer.seq
                    )
                ),
                None,
            )
            if existing is not None:
                if replace(existing.envelope, expected_revision=None).fingerprint() != event.fingerprint():
                    # Let the core produce the precise idempotency conflict.
                    return self.store.append(event, require_effect_ack=True)
                return self._commit_runtime_event(existing.envelope, claims=claims)
            target = aggregate_for_event(event)
            if target is not None:
                if event.producer.mode is not ProducerMode.ADAPTER:
                    raise ValidationError(
                        "active runtime state events require expected_revision"
                    )
                event = replace(
                    event,
                    expected_revision=self._revision(
                        claims.owner_id, target[0], target[1]
                    ),
                )
        return self._commit_runtime_event(event, claims=claims)

    def _commit_runtime_event(
        self, event: EventEnvelope, *, claims: ReporterClaims
    ):
        """Append a report and complete its replayable Saga side effects.

        The event and a pending-effect marker are committed together. Producer
        ACK calculation excludes pending markers, so an Artifact failure or a
        process crash between append and derived Task/Assignment updates keeps
        the exact WAL item eligible for replay instead of silently losing work.
        """
        result = self.store.append(event, require_effect_ack=True)
        try:
            self._after_runtime_event(result.event.envelope, claims=claims)
        except BaseException as error:
            self.store.fail_event_effect(event_id=result.event.id, error=str(error))
            raise
        self.store.complete_event_effect(event_id=result.event.id)
        return result

    def record_runtime_rejection(
        self,
        event: EventEnvelope,
        *,
        claims: ReporterClaims,
        code: str,
        message: str,
    ):
        """Consume a permanently rejected producer sequence with an audit fact.

        Without a durable tombstone the server would advertise the sequence as a
        missing range forever, poisoning every later WAL flush. Revision
        conflicts are intentionally excluded by the API because reporters may
        rebase those and retry the original event.
        """
        canonical = self._scope_for_run(claims.owner_id, claims.run_id)
        producer = replace(
            event.producer,
            id=self.runtime_producer_id(
                claims=claims, reported_producer_id=event.producer.id
            ),
        )
        rejection = replace(
            event,
            type="runtime.event.rejected",
            scope=canonical,
            producer=producer,
            expected_revision=None,
            payload={
                "reported_type": event.type,
                "reported_fingerprint": event.fingerprint(),
                "code": str(code)[:120],
                "message": str(message)[:1000],
            },
        )
        return self.store.append(rejection)

    def _after_runtime_event(
        self, event: EventEnvelope, *, claims: ReporterClaims
    ) -> None:
        """Idempotent Saga effects; safe to retry after a committed append."""
        if event.type == "artifact.published" and self.artifact_publisher:
            self.artifact_publisher(event)
        scope = event.scope
        if event.type == "agent.registered":
            self._activate_runtime_assignment(
                scope, claims=claims, causation_id=event.event_id
            )
        elif event.type in {"run.succeeded", "run.failed", "run.cancelled"}:
            self._finish_related(scope, event.type, causation_id=event.event_id)

    def _accept_assignment(self, scope: EventScope, *, causation_id: str) -> None:
        if not scope.assignment_id or not scope.task_id:
            return
        assignment = self.projection(
            owner_id=scope.owner_id,
            kind=EntityKind.ASSIGNMENT,
            entity_id=scope.assignment_id,
        )
        if assignment and assignment.state.get("lifecycle") == AssignmentLifecycle.CREATED.value:
            self._append(
                "assignment.accepted",
                scope,
                expected_revision=assignment.revision,
                mode=ProducerMode.SYSTEM,
                causation_id=causation_id,
            )
        task = self.projection(
            owner_id=scope.owner_id, kind=EntityKind.TASK, entity_id=scope.task_id
        )
        if task and task.state.get("lifecycle") == TaskLifecycle.ASSIGNED.value:
            self._append(
                "task.working",
                scope,
                expected_revision=task.revision,
                mode=ProducerMode.SYSTEM,
                causation_id=causation_id,
            )

    def _finish_related(
        self, scope: EventScope, outcome_event: str, *, causation_id: str
    ) -> None:
        if scope.assignment_id:
            assignment = self.projection(
                owner_id=scope.owner_id,
                kind=EntityKind.ASSIGNMENT,
                entity_id=scope.assignment_id,
            )
            if assignment and assignment.state.get("lifecycle") == AssignmentLifecycle.ACCEPTED.value:
                self._append(
                    "assignment.completed",
                    scope,
                    payload={"outcome": outcome_event.rsplit(".", 1)[-1]},
                    expected_revision=assignment.revision,
                    mode=ProducerMode.SYSTEM,
                    causation_id=causation_id,
                )
        if scope.task_id:
            task = self.projection(
                owner_id=scope.owner_id, kind=EntityKind.TASK, entity_id=scope.task_id
            )
            task_event = {
                "run.succeeded": "task.completed",
                "run.failed": "task.failed",
                "run.cancelled": "task.canceled",
            }[outcome_event]
            if task and task.state.get("lifecycle") not in {
                TaskLifecycle.COMPLETED.value,
                TaskLifecycle.FAILED.value,
                TaskLifecycle.CANCELED.value,
                TaskLifecycle.REJECTED.value,
            }:
                self._append(
                    task_event,
                    scope,
                    expected_revision=task.revision,
                    mode=ProducerMode.SYSTEM,
                    causation_id=causation_id,
                )
        if scope.terminal_id and scope.assignment_id:
            lease = self.store.get_lease(
                owner_id=scope.owner_id,
                resource_kind=EntityKind.TERMINAL,
                resource_id=scope.terminal_id,
            )
            if lease and lease.holder_id == scope.assignment_id:
                self.store.release_lease(
                    owner_id=scope.owner_id,
                    lease_id=lease.id,
                    holder_id=scope.assignment_id,
                    expected_revision=lease.revision,
                )
        if self.reporter_tokens is not None and scope.run_id:
            self.reporter_tokens.revoke_run_capabilities(
                owner_id=scope.owner_id,
                run_id=scope.run_id,
                capabilities={"commands", "ack"},
            )

    @_serialized_workflow
    def heartbeat(self, *, claims: ReporterClaims, holder_id: str) -> dict[str, Any]:
        if not claims.permits("heartbeat"):
            raise ValidationError("reporter token does not permit heartbeat")
        run_projection = self.projection(
            owner_id=claims.owner_id,
            kind=EntityKind.RUN,
            entity_id=claims.run_id,
        )
        if (
            run_projection is None
            or run_projection.state.get("lifecycle") not in ACTIVE_RUN_LIFECYCLES
        ):
            raise InvalidTransition(
                "run",
                run_projection.state.get("lifecycle") if run_projection else None,
                "heartbeat",
            )
        agent_id = claims.agent_instance_id
        if not agent_id:
            raise ValidationError("heartbeat token has no agent instance scope")
        scope = self._scope_for_run(claims.owner_id, claims.run_id)
        if (
            scope.agent_instance_id != agent_id
            or scope.terminal_id != claims.terminal_id
            or scope.launch_id != claims.launch_id
            or scope.device_id != claims.device_id
        ):
            raise ValidationError("heartbeat token does not match canonical Run scope")
        if not scope.terminal_id or not scope.assignment_id:
            raise ValidationError("heartbeat Run has no terminal assignment")
        lease, _terminal_lease = self.store.heartbeat_leases(
            owner_id=claims.owner_id,
            run_id=claims.run_id,
            agent_instance_id=agent_id,
            agent_holder_id=holder_id,
            agent_ttl_seconds=self.lease_ttl,
            terminal_id=scope.terminal_id,
            assignment_id=scope.assignment_id,
            terminal_ttl_seconds=self.lost_grace,
        )
        agent = self.projection(
            owner_id=claims.owner_id,
            kind=EntityKind.AGENT_INSTANCE,
            entity_id=agent_id,
        )
        if agent and agent.state.get("lifecycle") == AgentLifecycle.UNREACHABLE.value:
            self._append(
                "agent.recovered",
                scope,
                expected_revision=agent.revision,
                mode=ProducerMode.SYSTEM,
            )
        run = self.projection(
            owner_id=claims.owner_id, kind=EntityKind.RUN, entity_id=claims.run_id
        )
        if run and run.state.get("stale"):
            self._append(
                "run.recovered",
                scope,
                expected_revision=run.revision,
                mode=ProducerMode.SYSTEM,
            )
        return lease.as_dict()

    def reconcile_liveness(self, *, owner_id: str, now: float | None = None) -> int:
        """Advance expired Agent leases through unreachable/stale to lost.

        Lease expiry itself is coordination state.  UI events are emitted only
        when a freshness threshold is crossed, keeping heartbeat traffic out of
        the browser stream.
        """
        timestamp = time.time() if now is None else float(now)
        snapshot = self.store.snapshot(owner_id=owner_id)
        changed = 0
        agents = [
            projection
            for projection in snapshot.projections
            if projection.aggregate_kind == EntityKind.AGENT_INSTANCE.value
        ]
        for agent in agents:
            entity = self.store.get_entity(
                owner_id=owner_id,
                kind=EntityKind.AGENT_INSTANCE,
                entity_id=agent.aggregate_id,
            )
            if entity is None:
                continue
            run_id = str(entity.attributes.get("run_id") or "")
            if not run_id:
                continue
            scope = self._scope_for_run(owner_id, run_id)
            lifecycle = agent.state.get("lifecycle")
            lease = self.store.get_lease(
                owner_id=owner_id,
                resource_kind=EntityKind.AGENT_INSTANCE,
                resource_id=agent.aggregate_id,
                now=timestamp,
            )
            if lifecycle == AgentLifecycle.ONLINE.value and lease is None:
                self._append(
                    "agent.unreachable",
                    scope,
                    payload={"reason": "heartbeat_lease_expired"},
                    expected_revision=agent.revision,
                    mode=ProducerMode.SYSTEM,
                )
                run = self.projection(
                    owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id
                )
                if (
                    run
                    and run.state.get("lifecycle") in {
                        RunLifecycle.STARTING.value,
                        RunLifecycle.RUNNING.value,
                    }
                    and not run.state.get("stale")
                ):
                    self._append(
                        "run.stale",
                        scope,
                        payload={"reason": "agent_unreachable"},
                        expected_revision=run.revision,
                        mode=ProducerMode.SYSTEM,
                    )
                changed += 1
                continue
            if (
                lifecycle == AgentLifecycle.UNREACHABLE.value
                and timestamp - agent.updated_at >= self.lost_grace
            ):
                self._append(
                    "agent.lost",
                    scope,
                    payload={"reason": "heartbeat_grace_expired"},
                    expected_revision=agent.revision,
                    mode=ProducerMode.SYSTEM,
                )
                run = self.projection(
                    owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id
                )
                if run and run.state.get("lifecycle") in {
                    RunLifecycle.STARTING.value,
                    RunLifecycle.RUNNING.value,
                }:
                    self._append(
                        "run.lost",
                        scope,
                        payload={"reason": "agent_lost"},
                        expected_revision=run.revision,
                        mode=ProducerMode.SYSTEM,
                    )
                    self._lose_related(scope)
                changed += 1
        return changed

    def _lose_related(self, scope: EventScope) -> None:
        if scope.assignment_id:
            assignment = self.projection(
                owner_id=scope.owner_id,
                kind=EntityKind.ASSIGNMENT,
                entity_id=scope.assignment_id,
            )
            if assignment and assignment.state.get("lifecycle") in {
                AssignmentLifecycle.CREATED.value,
                AssignmentLifecycle.ACCEPTED.value,
            }:
                self._append(
                    "assignment.expired",
                    scope,
                    payload={"reason": "run_lost"},
                    expected_revision=assignment.revision,
                    mode=ProducerMode.SYSTEM,
                )
        if scope.task_id:
            task = self.projection(
                owner_id=scope.owner_id, kind=EntityKind.TASK, entity_id=scope.task_id
            )
            if task and task.state.get("lifecycle") in {
                TaskLifecycle.ASSIGNED.value,
                TaskLifecycle.WORKING.value,
                TaskLifecycle.INPUT_REQUIRED.value,
                TaskLifecycle.AUTH_REQUIRED.value,
            }:
                self._append(
                    "task.failed",
                    scope,
                    payload={"summary": "Agent heartbeat lost"},
                    expected_revision=task.revision,
                    mode=ProducerMode.SYSTEM,
                )
        if scope.terminal_id and scope.assignment_id:
            lease = self.store.get_lease(
                owner_id=scope.owner_id,
                resource_kind=EntityKind.TERMINAL,
                resource_id=scope.terminal_id,
            )
            if lease and lease.holder_id == scope.assignment_id:
                self.store.release_lease(
                    owner_id=scope.owner_id,
                    lease_id=lease.id,
                    holder_id=scope.assignment_id,
                    expected_revision=lease.revision,
                )

    def _existing_cancel_command(
        self, *, owner_id: str, scope: EventScope
    ) -> Command | None:
        target_id = scope.agent_instance_id or scope.run_id or ""
        for command in self.store.commands(
            owner_id=owner_id,
            target_kind=EntityKind.AGENT_INSTANCE,
            target_id=target_id,
            after_sequence=0,
            include_terminal=True,
            limit=1000,
        ):
            if command.type == "cancel" and command.payload.get("run_id") == scope.run_id:
                return command
        return None

    def _command_fence_payload(self, scope: EventScope) -> dict[str, Any]:
        lease = self._active_assignment_lease(scope)
        return {
            "run_id": scope.run_id or "",
            "assignment_id": scope.assignment_id or "",
            "terminal_id": scope.terminal_id or "",
            "launch_id": scope.launch_id or "",
            "terminal_lease_id": lease.id,
            "terminal_lease_revision": lease.revision,
        }

    def _request_cancel_one(self, *, owner_id: str, run_id: str) -> Command:
        scope = self._scope_for_run(owner_id, run_id)
        run = self.projection(owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id)
        if run is None:
            raise EntityNotFound("run does not exist")
        if run.state.get("lifecycle") not in ACTIVE_RUN_LIFECYCLES:
            raise InvalidTransition("run", run.state.get("lifecycle"), "cancel")
        existing = self._existing_cancel_command(owner_id=owner_id, scope=scope)
        if existing is not None:
            return existing
        revision = run.revision
        if not run.state.get("cancel_requested"):
            result = self._append(
                "run.cancel.requested",
                scope,
                expected_revision=revision,
            )
            if result.projection is not None:
                revision = result.projection.revision
        command_id = hashlib.sha256(
            f"cancel\0{owner_id}\0{run_id}".encode("utf-8")
        ).hexdigest()[:32]
        return self.store.enqueue_command(
            owner_id=owner_id,
            target_kind=EntityKind.AGENT_INSTANCE,
            target_id=scope.agent_instance_id or run_id,
            command_type="cancel",
            command_id=command_id,
            payload=self._command_fence_payload(scope),
            expected_revision=revision,
            expires_at=time.time() + 5 * 60,
        )

    @_serialized_workflow
    def request_cancel(self, *, owner_id: str, run_id: str) -> Command:
        command = self._request_cancel_one(owner_id=owner_id, run_id=run_id)
        pending = [run_id]
        seen = {run_id}
        while pending:
            parent_id = pending.pop()
            parent = self.store.get_entity(
                owner_id=owner_id, kind=EntityKind.RUN, entity_id=parent_id
            )
            if parent is None or not bool(
                parent.attributes.get("cancel_propagates", True)
            ):
                continue
            for relation in self.store.relations(
                owner_id=owner_id,
                relation=RelationKind.PARENT_RUN,
                source_kind=EntityKind.RUN,
                source_id=parent_id,
                target_kind=EntityKind.RUN,
            ):
                child_id = relation.target_id
                if child_id in seen:
                    continue
                seen.add(child_id)
                child = self.projection(
                    owner_id=owner_id,
                    kind=EntityKind.RUN,
                    entity_id=child_id,
                )
                if child and child.state.get("lifecycle") in ACTIVE_RUN_LIFECYCLES:
                    self._request_cancel_one(owner_id=owner_id, run_id=child_id)
                    pending.append(child_id)
        return command

    def provide_input(
        self, *, owner_id: str, run_id: str, value: str
    ) -> Command:
        scope = self._scope_for_run(owner_id, run_id)
        run = self.projection(owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id)
        if run is None or run.state.get("lifecycle") != RunLifecycle.RUNNING.value:
            raise InvalidTransition("run", run.state.get("lifecycle") if run else None, "input")
        if len(value.encode("utf-8")) > 64 * 1024:
            raise ValidationError("run input exceeds 64 KiB")
        return self.store.enqueue_command(
            owner_id=owner_id,
            target_kind=EntityKind.AGENT_INSTANCE,
            target_id=scope.agent_instance_id or run_id,
            command_type="input",
            payload={**self._command_fence_payload(scope), "value": value},
            expected_revision=run.revision,
            expires_at=time.time() + 60,
        )

    def retry_run(self, *, owner_id: str, run_id: str) -> dict[str, Any]:
        old = self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id
        )
        projection = self.projection(
            owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id
        )
        if old is None or projection is None:
            raise EntityNotFound("run does not exist")
        if projection.state.get("lifecycle") not in TERMINAL_RUN_LIFECYCLES:
            raise InvalidTransition("run", projection.state.get("lifecycle"), "retry")
        old_task = self.store.get_entity(
            owner_id=owner_id,
            kind=EntityKind.TASK,
            entity_id=str(old.attributes.get("task_id") or ""),
        )
        if old_task is None:
            raise EntityNotFound("run task does not exist")
        new_task = self.create_task(
            owner_id=owner_id,
            title=str(old_task.attributes.get("title") or "Retry"),
            description=str(old_task.attributes.get("description") or ""),
            context_id=str(old_task.attributes.get("context_id") or "") or None,
            retry_of_task_id=old_task.id,
        )
        return self.assign_task(
            owner_id=owner_id,
            task_id=new_task["id"],
            terminal_id=str(old.attributes.get("terminal_id") or ""),
            device_id=old.attributes.get("device_id"),
            agent_kind=str(old.attributes.get("agent_kind") or "generic"),
            expected_task_revision=int(new_task["revision"]),
            parent_run_id=old.attributes.get("parent_run_id"),
            attempt=int(old.attributes.get("attempt") or 1) + 1,
        )

    def runtime_context(self, *, claims: ReporterClaims) -> dict[str, Any]:
        scope = self._scope_for_claims(claims)
        run = self.projection(
            owner_id=claims.owner_id, kind=EntityKind.RUN, entity_id=claims.run_id
        )
        if run is None:
            raise EntityNotFound("run does not exist")
        server_time = float(self.clock())
        active = run.state.get("lifecycle") in ACTIVE_RUN_LIFECYCLES
        # A Run projection can remain active briefly after its Terminal lease
        # expires.  Never advertise that stale projection as executable
        # command context: downloaded commands may already exist on a remote
        # Bridge and their ACK would arrive only after the side effect.
        terminal_lease = (
            self._active_assignment_lease(scope, now=server_time)
            if active
            else None
        )
        assignment_id = scope.assignment_id
        assignment = (
            self.projection(
                owner_id=claims.owner_id,
                kind=EntityKind.ASSIGNMENT,
                entity_id=assignment_id,
            )
            if assignment_id
            else None
        )
        task_id = scope.task_id
        task = (
            self.store.get_entity(
                owner_id=claims.owner_id, kind=EntityKind.TASK, entity_id=task_id
            )
            if task_id
            else None
        )
        return {
            "schema": "agentserver.context/1",
            "managed": True,
            "origin": "agentserver",
            "terminal_id": scope.terminal_id,
            "launch_id": scope.launch_id,
            "server_time": server_time,
            "terminal_lease": (
                {
                    "id": terminal_lease.id,
                    "revision": terminal_lease.revision,
                    "expires_at": terminal_lease.expires_at,
                }
                if terminal_lease is not None
                else None
            ),
            "context_revision": max(
                run.updated_sequence,
                assignment.updated_sequence if assignment else 0,
            ),
            "assignment": (
                {
                    "task_id": task_id,
                    "assignment_id": assignment_id,
                    "title": task.attributes.get("title") if task else "",
                    "status": assignment.state.get("lifecycle") if assignment else None,
                }
                if assignment_id
                else None
            ),
            "active_run_id": (
                claims.run_id
                if active
                else None
            ),
            "recent_run": self._projection_view(run),
            "revisions": {
                "run": run.revision,
                "assignment": assignment.revision if assignment else 0,
            },
            "control_available": True,
        }

    def terminal_context(
        self, *, owner_id: str, terminal_id: str, launch_id: str
    ) -> dict[str, Any]:
        terminal = self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        terminal_projection = self.projection(
            owner_id=owner_id, kind=EntityKind.TERMINAL, entity_id=terminal_id
        )
        if terminal is None or terminal_projection is None:
            raise EntityNotFound("managed terminal does not exist")
        if str(terminal.attributes.get("launch_id") or "") != launch_id:
            raise ValidationError("terminal launch identity does not match")
        view = self.execution_view(owner_id=owner_id)
        binding = next(
            (
                item
                for item in view["terminal_bindings"]
                if item["terminal_id"] == terminal_id
            ),
            None,
        )
        active_run_id = binding.get("active_run_id") if binding else None
        terminal_runs = [
            item
            for item in view["runs"]
            if item["attributes"].get("terminal_id") == terminal_id
        ]
        recent_run = max(
            terminal_runs,
            key=lambda item: item["last_global_sequence"],
            default=None,
        )
        active_run = next(
            (item for item in terminal_runs if item["id"] == active_run_id),
            None,
        )
        assignment = None
        if active_run:
            assignment_id = active_run["attributes"].get("assignment_id")
            task_id = active_run["attributes"].get("task_id")
            assignment_projection = next(
                (
                    item
                    for item in view["assignments"]
                    if item["id"] == assignment_id
                ),
                None,
            )
            task = self.store.get_entity(
                owner_id=owner_id,
                kind=EntityKind.TASK,
                entity_id=str(task_id or ""),
            )
            assignment = {
                "task_id": task_id,
                "assignment_id": assignment_id,
                "title": task.attributes.get("title") if task else "",
                "status": (
                    assignment_projection["state"].get("lifecycle")
                    if assignment_projection
                    else None
                ),
            }
        return {
            "schema": "agentserver.context/1",
            "managed": True,
            "origin": "agentserver",
            "terminal_id": terminal_id,
            "launch_id": launch_id,
            "context_revision": max(
                terminal_projection.updated_sequence,
                active_run["last_global_sequence"] if active_run else 0,
                recent_run["last_global_sequence"] if recent_run else 0,
            ),
            "assignment": assignment,
            "active_run_id": active_run_id,
            "recent_run": recent_run,
            "control_available": True,
        }

    @staticmethod
    def _field_events(event: StoredEvent) -> tuple[str, ...]:
        event_type = event.type
        fields: list[str] = []
        if event.aggregate_kind:
            if "lifecycle" in event_type or event_type in {
                "task.created", "task.assigned", "task.working", "task.completed",
                "task.failed", "task.canceled", "task.rejected",
                "assignment.created", "assignment.accepted", "assignment.rejected",
                "assignment.expired", "assignment.completed", "run.requested",
                "run.starting", "run.started", "run.succeeded", "run.failed",
                "run.cancelled", "run.lost", "agent.discovered", "agent.starting",
                "agent.registered", "agent.stopping", "agent.unreachable",
                "agent.recovered", "agent.exited", "agent.lost",
            }:
                fields.append("lifecycle")
        if event_type in {"run.started", "run.activity.changed", "run.input.requested", "run.input.provided"}:
            fields.extend(("activity", "wait_reason"))
        if event_type == "run.progress.updated":
            fields.append("progress")
        if "summary" in event.payload:
            fields.append("summary")
        if event_type in {"agent.registered", "observation.cwd.changed"} and "cwd" in event.payload:
            fields.append("cwd")
        return tuple(fields)

    @staticmethod
    def _source_label(mode: ProducerMode) -> str:
        return {
            ProducerMode.ACTIVE: "reported",
            ProducerMode.ADAPTER: "adapter",
            ProducerMode.OBSERVED: "observed",
            ProducerMode.CONTROL: "control",
            ProducerMode.SYSTEM: "inferred",
        }[mode]

    def _evidence_for(
        self, projection: Projection, events: Iterable[StoredEvent], *, now: float
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for event in events:
            if (
                event.aggregate_kind != projection.aggregate_kind
                or event.aggregate_id != projection.aggregate_id
            ):
                continue
            evidence = event.envelope.evidence
            expires_at = (
                event.recorded_at + evidence.valid_for_ms / 1000
                if evidence and evidence.valid_for_ms is not None
                else None
            )
            for field in self._field_events(event):
                result[field] = {
                    "source": self._source_label(event.producer.mode),
                    "producer_id": event.producer.id,
                    "confidence": evidence.confidence if evidence else 1.0,
                    "recorded_at": event.recorded_at,
                    "expires_at": expires_at,
                    "fresh": expires_at is None or expires_at > now,
                    "global_sequence": event.global_sequence,
                }
        return result

    def _projection_view(
        self,
        projection: Projection,
        events: Iterable[StoredEvent] = (),
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        entity = self.store.get_entity(
            owner_id=projection.owner_id,
            kind=projection.aggregate_kind,
            entity_id=projection.aggregate_id,
        )
        state = dict(projection.state)
        evidence = self._evidence_for(projection, events, now=timestamp)
        activity_evidence = evidence.get("activity")
        if (
            projection.aggregate_kind == EntityKind.RUN.value
            and state.get("lifecycle") == RunLifecycle.RUNNING.value
            and activity_evidence
            and not activity_evidence["fresh"]
        ):
            state["activity"] = "unknown"
            state.pop("wait_reason", None)
            activity_evidence = {**activity_evidence, "source": "stale"}
            evidence["activity"] = activity_evidence
        return {
            "id": projection.aggregate_id,
            "kind": projection.aggregate_kind,
            "revision": projection.revision,
            "last_global_sequence": projection.updated_sequence,
            "view_sequence": projection.updated_sequence,
            "updated_at": projection.updated_at,
            "state": state,
            "attributes": dict(entity.attributes) if entity else {},
            "evidence": evidence,
        }

    def execution_view(self, *, owner_id: str, now: float | None = None) -> dict[str, Any]:
        snapshot = self.store.snapshot(owner_id=owner_id)
        from .observations import ObservationMerger

        merger = ObservationMerger()
        for event in snapshot.events:
            try:
                merger.ingest(event)
            except (ValidationError, IdempotencyConflict):
                continue
        by_kind: dict[str, list[dict[str, Any]]] = {
            "tasks": [],
            "assignments": [],
            "runs": [],
            "agents": [],
            "terminals": [],
        }
        key_for_kind = {
            EntityKind.TASK.value: "tasks",
            EntityKind.ASSIGNMENT.value: "assignments",
            EntityKind.RUN.value: "runs",
            EntityKind.AGENT_INSTANCE.value: "agents",
            EntityKind.TERMINAL.value: "terminals",
        }
        projections: dict[tuple[str, str], Projection] = {}
        for projection in snapshot.projections:
            projections[(projection.aggregate_kind, projection.aggregate_id)] = projection
            key = key_for_kind.get(projection.aggregate_kind)
            if key:
                by_kind[key].append(
                    self._projection_view(projection, snapshot.events, now=now)
                )
        timestamp = time.time() if now is None else float(now)
        for key, identifier_name in (
            ("runs", "run_id"),
            ("agents", "agent_instance_id"),
            ("terminals", "terminal_id"),
        ):
            for item in by_kind[key]:
                arguments = {
                    "owner_id": owner_id,
                    identifier_name: item["id"],
                    "now": timestamp,
                }
                merged = merger.state_for(**arguments)
                for field_name, field in merged.fields.items():
                    if field_name in {
                        "activity",
                        "wait_reason",
                        "wait_target_run_id",
                        "progress",
                        "summary",
                        "cwd",
                        "process_alive",
                    }:
                        if field.value is None:
                            item["state"].pop(field_name, None)
                        else:
                            item["state"][field_name] = field.value
                    item["evidence"][field_name] = {
                        **field.as_dict(),
                        "fresh": not field.stale,
                    }
                    item["view_sequence"] = max(
                        int(item["view_sequence"]), field.global_sequence
                    )
        terminal_ids = {
            item["id"] for item in by_kind["terminals"]
        } | {
            str(item["attributes"].get("terminal_id"))
            for item in by_kind["runs"] + by_kind["agents"]
            if item["attributes"].get("terminal_id")
        }
        bindings: list[dict[str, Any]] = []
        for terminal_id in sorted(terminal_ids):
            active_runs = [
                item for item in by_kind["runs"]
                if item["attributes"].get("terminal_id") == terminal_id
                and item["state"].get("lifecycle") in ACTIVE_RUN_LIFECYCLES
            ]
            active_agents = [
                item for item in by_kind["agents"]
                if item["attributes"].get("terminal_id") == terminal_id
                and item["state"].get("lifecycle") in ACTIVE_AGENT_LIFECYCLES
            ]
            run = max(active_runs, key=lambda item: item["view_sequence"], default=None)
            agent = max(active_agents, key=lambda item: item["view_sequence"], default=None)
            bindings.append(
                {
                    "terminal_id": terminal_id,
                    "active_run_id": run["id"] if run else None,
                    "active_agent_instance_id": agent["id"] if agent else (
                        run["attributes"].get("agent_instance_id") if run else None
                    ),
                    "last_global_sequence": max(
                        run["view_sequence"] if run else 0,
                        agent["view_sequence"] if agent else 0,
                    ),
                }
            )
        return {
            "schema": "agentserver.execution-snapshot/1",
            "owner_id": owner_id,
            "as_of_sequence": snapshot.as_of_sequence,
            **by_kind,
            "terminal_bindings": bindings,
            "relations": [
                relation.as_dict()
                for relation in self.store.relations(owner_id=owner_id)
            ],
            "unattributed_observations": [
                record.as_dict()
                for record in merger.records(
                    owner_id=owner_id, unattributed_only=True
                )
            ],
        }

    def get_task(self, *, owner_id: str, task_id: str) -> dict[str, Any]:
        view = self.execution_view(owner_id=owner_id)
        tasks = [item for item in view["tasks"] if item["id"] == task_id]
        if not tasks:
            raise EntityNotFound("task does not exist")
        assignment_ids = {
            relation.target_id
            for relation in self.store.relations(
                owner_id=owner_id,
                relation=RelationKind.CONTAINS,
                source_kind=EntityKind.TASK,
                source_id=task_id,
                target_kind=EntityKind.ASSIGNMENT,
            )
        }
        assignments = [
            item for item in view["assignments"] if item["id"] in assignment_ids
        ]
        run_ids = {
            relation.target_id
            for assignment_id in assignment_ids
            for relation in self.store.relations(
                owner_id=owner_id,
                relation=RelationKind.EXECUTES,
                source_kind=EntityKind.ASSIGNMENT,
                source_id=assignment_id,
                target_kind=EntityKind.RUN,
            )
        }
        return {
            "task": tasks[0],
            "assignments": assignments,
            "runs": [item for item in view["runs"] if item["id"] in run_ids],
            "as_of_sequence": view["as_of_sequence"],
        }

    def get_run(self, *, owner_id: str, run_id: str) -> dict[str, Any]:
        snapshot = self.store.snapshot(
            owner_id=owner_id,
            aggregate_kind=EntityKind.RUN,
            aggregate_id=run_id,
        )
        if not snapshot.projections:
            raise EntityNotFound("run does not exist")
        run = self._projection_view(snapshot.projections[0], snapshot.events)
        run["parents"] = [
            item.source_id
            for item in self.store.relations(
                owner_id=owner_id,
                relation=RelationKind.PARENT_RUN,
                target_kind=EntityKind.RUN,
                target_id=run_id,
            )
        ]
        run["children"] = [
            item.target_id
            for item in self.store.relations(
                owner_id=owner_id,
                relation=RelationKind.PARENT_RUN,
                source_kind=EntityKind.RUN,
                source_id=run_id,
            )
        ]
        return run

    def run_events(
        self,
        *,
        owner_id: str,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id
        ) is None:
            raise EntityNotFound("run does not exist")
        return self.run_event_page(
            owner_id=owner_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )["events"]

    def run_event_page(
        self,
        *,
        owner_id: str,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        if self.store.get_entity(
            owner_id=owner_id, kind=EntityKind.RUN, entity_id=run_id
        ) is None:
            raise EntityNotFound("run does not exist")
        page = self.store.timeline(
            owner_id=owner_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=max(1, min(int(limit), 1000)),
        )
        events = [event.as_dict() for event in page.events]
        next_sequence = (
            int(events[-1]["global_sequence"]) if events else after_sequence
        )
        return {
            "events": events,
            "after_sequence": after_sequence,
            "next_sequence": next_sequence,
            "has_more": bool(page.resync_required and events),
            "as_of_sequence": page.as_of_sequence,
            "resync_required": bool(
                page.resync_required and not events and after_sequence > page.as_of_sequence
            ),
        }
