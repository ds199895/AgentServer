from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .errors import InvalidTransition, ValidationError
from .events import EventEnvelope
from .models import (
    AgentLifecycle,
    AssignmentLifecycle,
    EntityKind,
    RunActivity,
    RunLifecycle,
    SpanLifecycle,
    TaskLifecycle,
    TerminalLifecycle,
    WaitReason,
)


TERMINAL_TRANSITIONS = {
    None: {TerminalLifecycle.REQUESTED.value},
    TerminalLifecycle.REQUESTED.value: {
        TerminalLifecycle.PROVISIONING.value,
        TerminalLifecycle.CONNECTING.value,
        TerminalLifecycle.READY.value,
        TerminalLifecycle.FAILED.value,
    },
    TerminalLifecycle.PROVISIONING.value: {
        TerminalLifecycle.CONNECTING.value,
        TerminalLifecycle.READY.value,
        TerminalLifecycle.FAILED.value,
    },
    TerminalLifecycle.CONNECTING.value: {
        TerminalLifecycle.READY.value,
        TerminalLifecycle.DISCONNECTED.value,
        TerminalLifecycle.FAILED.value,
    },
    TerminalLifecycle.READY.value: {
        TerminalLifecycle.DISCONNECTED.value,
        TerminalLifecycle.EXITED.value,
        TerminalLifecycle.FAILED.value,
    },
    TerminalLifecycle.DISCONNECTED.value: {
        TerminalLifecycle.CONNECTING.value,
        TerminalLifecycle.READY.value,
        TerminalLifecycle.EXITED.value,
        TerminalLifecycle.FAILED.value,
    },
    TerminalLifecycle.EXITED.value: set(),
    TerminalLifecycle.FAILED.value: set(),
}

AGENT_TRANSITIONS = {
    None: {
        AgentLifecycle.DISCOVERED.value,
        AgentLifecycle.STARTING.value,
        AgentLifecycle.ONLINE.value,
    },
    AgentLifecycle.DISCOVERED.value: {
        AgentLifecycle.STARTING.value,
        AgentLifecycle.ONLINE.value,
        AgentLifecycle.LOST.value,
    },
    AgentLifecycle.STARTING.value: {
        AgentLifecycle.ONLINE.value,
        AgentLifecycle.STOPPING.value,
        AgentLifecycle.UNREACHABLE.value,
        AgentLifecycle.EXITED.value,
        AgentLifecycle.LOST.value,
    },
    AgentLifecycle.ONLINE.value: {
        AgentLifecycle.STOPPING.value,
        AgentLifecycle.UNREACHABLE.value,
        AgentLifecycle.EXITED.value,
        AgentLifecycle.LOST.value,
    },
    AgentLifecycle.UNREACHABLE.value: {
        AgentLifecycle.ONLINE.value,
        AgentLifecycle.STOPPING.value,
        AgentLifecycle.EXITED.value,
        AgentLifecycle.LOST.value,
    },
    AgentLifecycle.STOPPING.value: {
        AgentLifecycle.EXITED.value,
        AgentLifecycle.LOST.value,
    },
    AgentLifecycle.EXITED.value: set(),
    AgentLifecycle.LOST.value: set(),
}

TASK_TRANSITIONS = {
    None: {TaskLifecycle.SUBMITTED.value},
    TaskLifecycle.SUBMITTED.value: {
        TaskLifecycle.ASSIGNED.value,
        TaskLifecycle.CANCELED.value,
        TaskLifecycle.REJECTED.value,
    },
    TaskLifecycle.ASSIGNED.value: {
        TaskLifecycle.WORKING.value,
        TaskLifecycle.INPUT_REQUIRED.value,
        TaskLifecycle.AUTH_REQUIRED.value,
        TaskLifecycle.CANCELED.value,
        TaskLifecycle.REJECTED.value,
    },
    TaskLifecycle.WORKING.value: {
        TaskLifecycle.INPUT_REQUIRED.value,
        TaskLifecycle.AUTH_REQUIRED.value,
        TaskLifecycle.COMPLETED.value,
        TaskLifecycle.FAILED.value,
        TaskLifecycle.CANCELED.value,
    },
    TaskLifecycle.INPUT_REQUIRED.value: {
        TaskLifecycle.WORKING.value,
        TaskLifecycle.FAILED.value,
        TaskLifecycle.CANCELED.value,
    },
    TaskLifecycle.AUTH_REQUIRED.value: {
        TaskLifecycle.WORKING.value,
        TaskLifecycle.FAILED.value,
        TaskLifecycle.CANCELED.value,
    },
    TaskLifecycle.COMPLETED.value: set(),
    TaskLifecycle.FAILED.value: set(),
    TaskLifecycle.CANCELED.value: set(),
    TaskLifecycle.REJECTED.value: set(),
}

ASSIGNMENT_TRANSITIONS = {
    None: {AssignmentLifecycle.CREATED.value},
    AssignmentLifecycle.CREATED.value: {
        AssignmentLifecycle.ACCEPTED.value,
        AssignmentLifecycle.REJECTED.value,
        AssignmentLifecycle.EXPIRED.value,
    },
    AssignmentLifecycle.ACCEPTED.value: {
        AssignmentLifecycle.COMPLETED.value,
        AssignmentLifecycle.EXPIRED.value,
    },
    AssignmentLifecycle.REJECTED.value: set(),
    AssignmentLifecycle.EXPIRED.value: set(),
    AssignmentLifecycle.COMPLETED.value: set(),
}

RUN_TRANSITIONS = {
    None: {RunLifecycle.PENDING.value},
    RunLifecycle.PENDING.value: {
        RunLifecycle.STARTING.value,
        RunLifecycle.RUNNING.value,
        RunLifecycle.CANCELLED.value,
    },
    RunLifecycle.STARTING.value: {
        RunLifecycle.RUNNING.value,
        RunLifecycle.FAILED.value,
        RunLifecycle.CANCELLED.value,
        RunLifecycle.LOST.value,
    },
    RunLifecycle.RUNNING.value: {
        RunLifecycle.SUCCEEDED.value,
        RunLifecycle.FAILED.value,
        RunLifecycle.CANCELLED.value,
        RunLifecycle.LOST.value,
    },
    RunLifecycle.SUCCEEDED.value: set(),
    RunLifecycle.FAILED.value: set(),
    RunLifecycle.CANCELLED.value: set(),
    RunLifecycle.LOST.value: set(),
}

SPAN_TRANSITIONS = {
    None: {SpanLifecycle.RUNNING.value},
    SpanLifecycle.RUNNING.value: {
        SpanLifecycle.SUCCEEDED.value,
        SpanLifecycle.FAILED.value,
        SpanLifecycle.CANCELLED.value,
    },
    SpanLifecycle.SUCCEEDED.value: set(),
    SpanLifecycle.FAILED.value: set(),
    SpanLifecycle.CANCELLED.value: set(),
}


_LIFECYCLE_EVENTS: dict[str, tuple[str, str]] = {
    "terminal.launch.requested": (EntityKind.TERMINAL.value, TerminalLifecycle.REQUESTED.value),
    "terminal.provisioning": (EntityKind.TERMINAL.value, TerminalLifecycle.PROVISIONING.value),
    "terminal.connecting": (EntityKind.TERMINAL.value, TerminalLifecycle.CONNECTING.value),
    "terminal.ready": (EntityKind.TERMINAL.value, TerminalLifecycle.READY.value),
    "terminal.disconnected": (EntityKind.TERMINAL.value, TerminalLifecycle.DISCONNECTED.value),
    "terminal.exited": (EntityKind.TERMINAL.value, TerminalLifecycle.EXITED.value),
    "terminal.launch.failed": (EntityKind.TERMINAL.value, TerminalLifecycle.FAILED.value),
    "terminal.failed": (EntityKind.TERMINAL.value, TerminalLifecycle.FAILED.value),
    "agent.discovered": (EntityKind.AGENT_INSTANCE.value, AgentLifecycle.DISCOVERED.value),
    "agent.starting": (EntityKind.AGENT_INSTANCE.value, AgentLifecycle.STARTING.value),
    "agent.registered": (EntityKind.AGENT_INSTANCE.value, AgentLifecycle.ONLINE.value),
    "agent.stopping": (EntityKind.AGENT_INSTANCE.value, AgentLifecycle.STOPPING.value),
    "agent.unreachable": (EntityKind.AGENT_INSTANCE.value, AgentLifecycle.UNREACHABLE.value),
    "agent.recovered": (EntityKind.AGENT_INSTANCE.value, AgentLifecycle.ONLINE.value),
    "agent.exited": (EntityKind.AGENT_INSTANCE.value, AgentLifecycle.EXITED.value),
    "agent.lost": (EntityKind.AGENT_INSTANCE.value, AgentLifecycle.LOST.value),
    "task.created": (EntityKind.TASK.value, TaskLifecycle.SUBMITTED.value),
    "task.assigned": (EntityKind.TASK.value, TaskLifecycle.ASSIGNED.value),
    "task.working": (EntityKind.TASK.value, TaskLifecycle.WORKING.value),
    "task.input.required": (EntityKind.TASK.value, TaskLifecycle.INPUT_REQUIRED.value),
    "task.auth.required": (EntityKind.TASK.value, TaskLifecycle.AUTH_REQUIRED.value),
    "task.completed": (EntityKind.TASK.value, TaskLifecycle.COMPLETED.value),
    "task.failed": (EntityKind.TASK.value, TaskLifecycle.FAILED.value),
    "task.canceled": (EntityKind.TASK.value, TaskLifecycle.CANCELED.value),
    "task.rejected": (EntityKind.TASK.value, TaskLifecycle.REJECTED.value),
    "assignment.created": (EntityKind.ASSIGNMENT.value, AssignmentLifecycle.CREATED.value),
    "assignment.accepted": (EntityKind.ASSIGNMENT.value, AssignmentLifecycle.ACCEPTED.value),
    "assignment.rejected": (EntityKind.ASSIGNMENT.value, AssignmentLifecycle.REJECTED.value),
    "assignment.expired": (EntityKind.ASSIGNMENT.value, AssignmentLifecycle.EXPIRED.value),
    "assignment.completed": (EntityKind.ASSIGNMENT.value, AssignmentLifecycle.COMPLETED.value),
    "run.requested": (EntityKind.RUN.value, RunLifecycle.PENDING.value),
    "run.starting": (EntityKind.RUN.value, RunLifecycle.STARTING.value),
    "run.started": (EntityKind.RUN.value, RunLifecycle.RUNNING.value),
    "run.succeeded": (EntityKind.RUN.value, RunLifecycle.SUCCEEDED.value),
    "run.failed": (EntityKind.RUN.value, RunLifecycle.FAILED.value),
    "run.cancelled": (EntityKind.RUN.value, RunLifecycle.CANCELLED.value),
    "run.lost": (EntityKind.RUN.value, RunLifecycle.LOST.value),
    "span.started": (EntityKind.SPAN.value, SpanLifecycle.RUNNING.value),
}

_TRANSITIONS = {
    EntityKind.TERMINAL.value: TERMINAL_TRANSITIONS,
    EntityKind.AGENT_INSTANCE.value: AGENT_TRANSITIONS,
    EntityKind.TASK.value: TASK_TRANSITIONS,
    EntityKind.ASSIGNMENT.value: ASSIGNMENT_TRANSITIONS,
    EntityKind.RUN.value: RUN_TRANSITIONS,
    EntityKind.SPAN.value: SPAN_TRANSITIONS,
}

_SCOPE_IDS = {
    EntityKind.TERMINAL.value: "terminal_id",
    EntityKind.AGENT_INSTANCE.value: "agent_instance_id",
    EntityKind.TASK.value: "task_id",
    EntityKind.ASSIGNMENT.value: "assignment_id",
    EntityKind.RUN.value: "run_id",
    EntityKind.SPAN.value: "span_id",
}

_NON_LIFECYCLE_EVENTS = {
    "agent.heartbeat": EntityKind.AGENT_INSTANCE.value,
    "run.activity.changed": EntityKind.RUN.value,
    "run.progress.updated": EntityKind.RUN.value,
    "run.input.requested": EntityKind.RUN.value,
    "run.input.provided": EntityKind.RUN.value,
    "run.cancel.requested": EntityKind.RUN.value,
    "run.stale": EntityKind.RUN.value,
    "run.recovered": EntityKind.RUN.value,
    "span.updated": EntityKind.SPAN.value,
    "span.ended": EntityKind.SPAN.value,
}


def aggregate_for_event(event: EventEnvelope) -> tuple[str, str] | None:
    """Return the state stream affected by an event, if it has one."""
    lifecycle = _LIFECYCLE_EVENTS.get(event.type)
    kind = lifecycle[0] if lifecycle else _NON_LIFECYCLE_EVENTS.get(event.type)
    if event.type.endswith(".lifecycle.changed"):
        prefix = event.type.split(".", 1)[0]
        if prefix == "agent":
            kind = EntityKind.AGENT_INSTANCE.value
        elif prefix in {
            EntityKind.TERMINAL.value,
            EntityKind.TASK.value,
            EntityKind.ASSIGNMENT.value,
            EntityKind.RUN.value,
        }:
            kind = prefix
    if kind is None:
        return None
    identifier = getattr(event.scope, _SCOPE_IDS[kind])
    if not identifier:
        raise ValidationError(f"{event.type} requires {_SCOPE_IDS[kind]} in scope")
    return kind, identifier


def stream_for_event(event: EventEnvelope) -> tuple[str, str] | None:
    """Return a useful timeline stream even when the event has no projection.

    Observations must remain evidence rather than state mutations, but a
    run-scoped observation still belongs in that Run's replayable timeline.
    """
    projected = aggregate_for_event(event)
    if projected is not None:
        return projected
    for kind, field_name in (
        (EntityKind.SPAN.value, "span_id"),
        (EntityKind.RUN.value, "run_id"),
        (EntityKind.AGENT_INSTANCE.value, "agent_instance_id"),
        (EntityKind.ASSIGNMENT.value, "assignment_id"),
        (EntityKind.TASK.value, "task_id"),
        (EntityKind.TERMINAL.value, "terminal_id"),
        (EntityKind.DEVICE.value, "device_id"),
    ):
        identifier = getattr(event.scope, field_name)
        if identifier:
            return kind, identifier
    return None


def _transition(
    kind: str,
    state: dict[str, Any],
    target: str,
    transitions: Mapping[str | None, set[str]],
) -> None:
    current = state.get("lifecycle")
    if target == current:
        raise InvalidTransition(kind, current, target)
    if target not in transitions.get(current, set()):
        raise InvalidTransition(kind, current, target)
    state["lifecycle"] = target


def _lifecycle_target(event: EventEnvelope, kind: str) -> str | None:
    known = _LIFECYCLE_EVENTS.get(event.type)
    if known:
        return known[1]
    if event.type.endswith(".lifecycle.changed"):
        target = str(event.payload.get("lifecycle") or "")
        if not target:
            raise ValidationError("lifecycle.changed requires payload.lifecycle")
        return target
    return None


def _validate_progress(payload: Mapping[str, Any]) -> dict[str, Any]:
    progress: dict[str, Any] = {}
    if "progress" in payload:
        value = payload["progress"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            raise ValidationError("payload.progress must be between 0 and 1")
        progress["progress"] = float(value)
    if "current" in payload or "total" in payload:
        current = payload.get("current")
        total = payload.get("total")
        if (
            not isinstance(current, (int, float))
            or isinstance(current, bool)
            or not isinstance(total, (int, float))
            or isinstance(total, bool)
            or current < 0
            or total <= 0
            or current > total
        ):
            raise ValidationError("payload current/total must satisfy 0 <= current <= total")
        progress.update({"current": current, "total": total, "progress": current / total})
    if not progress:
        raise ValidationError("progress update requires progress or current/total")
    return progress


def project_event(
    current_state: Mapping[str, Any] | None, event: EventEnvelope
) -> dict[str, Any] | None:
    """Validate one event and return the next canonical aggregate state."""
    target = aggregate_for_event(event)
    if target is None:
        return None
    kind, _identifier = target
    state: dict[str, Any] = deepcopy(dict(current_state or {}))
    lifecycle = _lifecycle_target(event, kind)
    if lifecycle is not None:
        _transition(kind, state, lifecycle, _TRANSITIONS[kind])
        if kind == EntityKind.RUN.value:
            if lifecycle == RunLifecycle.RUNNING.value:
                state["activity"] = str(
                    event.payload.get("activity") or RunActivity.UNKNOWN.value
                )
                try:
                    RunActivity(state["activity"])
                except ValueError as exc:
                    raise ValidationError("run.started payload.activity is invalid") from exc
                state["stale"] = False
            elif lifecycle in {
                RunLifecycle.SUCCEEDED.value,
                RunLifecycle.FAILED.value,
                RunLifecycle.CANCELLED.value,
                RunLifecycle.LOST.value,
            }:
                state.pop("activity", None)
                state.pop("wait_reason", None)
                state.pop("wait_target_run_id", None)
                state["stale"] = False
        if kind == EntityKind.AGENT_INSTANCE.value:
            state["stale"] = lifecycle == AgentLifecycle.UNREACHABLE.value
    elif event.type == "agent.heartbeat":
        current = state.get("lifecycle")
        if current == AgentLifecycle.UNREACHABLE.value:
            _transition(kind, state, AgentLifecycle.ONLINE.value, AGENT_TRANSITIONS)
        elif current != AgentLifecycle.ONLINE.value:
            raise InvalidTransition(kind, current, AgentLifecycle.ONLINE.value)
        state["stale"] = False
        state["last_heartbeat_occurred_at"] = event.occurred_at
    elif event.type == "run.activity.changed":
        if state.get("lifecycle") != RunLifecycle.RUNNING.value:
            raise InvalidTransition(kind, state.get("lifecycle"), "activity.changed")
        try:
            activity = RunActivity(str(event.payload.get("activity") or ""))
        except ValueError as exc:
            raise ValidationError("run.activity.changed requires a valid activity") from exc
        state["activity"] = activity.value
        if activity is RunActivity.WAITING:
            try:
                reason = WaitReason(str(event.payload.get("wait_reason") or ""))
            except ValueError as exc:
                raise ValidationError("waiting activity requires a valid wait_reason") from exc
            state["wait_reason"] = reason.value
            target_run = event.payload.get("wait_target_run_id")
            if reason is WaitReason.CHILD_RUN and not target_run:
                raise ValidationError("child_run waiting requires wait_target_run_id")
            if target_run:
                state["wait_target_run_id"] = str(target_run)
        else:
            state.pop("wait_reason", None)
            state.pop("wait_target_run_id", None)
    elif event.type == "run.progress.updated":
        if state.get("lifecycle") != RunLifecycle.RUNNING.value:
            raise InvalidTransition(kind, state.get("lifecycle"), "progress.updated")
        state.update(_validate_progress(event.payload))
    elif event.type == "run.input.requested":
        if state.get("lifecycle") != RunLifecycle.RUNNING.value:
            raise InvalidTransition(kind, state.get("lifecycle"), "input.requested")
        state.update(
            {
                "activity": RunActivity.WAITING.value,
                "wait_reason": str(
                    event.payload.get("wait_reason") or WaitReason.USER_INPUT.value
                ),
            }
        )
        try:
            WaitReason(state["wait_reason"])
        except ValueError as exc:
            raise ValidationError("run.input.requested wait_reason is invalid") from exc
    elif event.type == "run.input.provided":
        if state.get("lifecycle") != RunLifecycle.RUNNING.value:
            raise InvalidTransition(kind, state.get("lifecycle"), "input.provided")
        state.update({"activity": RunActivity.UNKNOWN.value, "stale": False})
        state.pop("wait_reason", None)
        state.pop("wait_target_run_id", None)
    elif event.type == "run.cancel.requested":
        if state.get("lifecycle") not in {
            RunLifecycle.PENDING.value,
            RunLifecycle.STARTING.value,
            RunLifecycle.RUNNING.value,
        }:
            raise InvalidTransition(kind, state.get("lifecycle"), "cancel.requested")
        state["cancel_requested"] = True
    elif event.type == "run.stale":
        if state.get("lifecycle") not in {
            RunLifecycle.STARTING.value,
            RunLifecycle.RUNNING.value,
        }:
            raise InvalidTransition(kind, state.get("lifecycle"), "stale")
        state["stale"] = True
    elif event.type == "run.recovered":
        if state.get("lifecycle") not in {
            RunLifecycle.STARTING.value,
            RunLifecycle.RUNNING.value,
        } or not state.get("stale"):
            raise InvalidTransition(kind, state.get("lifecycle"), "recovered")
        state["stale"] = False
    elif event.type == "span.updated":
        if state.get("lifecycle") != SpanLifecycle.RUNNING.value:
            raise InvalidTransition(kind, state.get("lifecycle"), "updated")
    elif event.type == "span.ended":
        try:
            outcome = SpanLifecycle(str(event.payload.get("outcome") or ""))
        except ValueError as exc:
            raise ValidationError("span.ended requires a terminal payload.outcome") from exc
        if outcome is SpanLifecycle.RUNNING:
            raise ValidationError("span.ended outcome must be terminal")
        _transition(kind, state, outcome.value, SPAN_TRANSITIONS)
    else:  # pragma: no cover - aggregate_for_event and handlers stay in lockstep
        raise ValidationError(f"unsupported projected event type: {event.type}")

    if "summary" in event.payload:
        summary = str(event.payload["summary"])
        if len(summary) > 2000:
            raise ValidationError("payload.summary must not exceed 2000 characters")
        state["summary"] = summary
    state["last_event_type"] = event.type
    return state
