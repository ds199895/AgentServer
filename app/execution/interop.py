from __future__ import annotations

import hashlib
import hmac
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

from .events import StoredEvent


_MACHINE_CODE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ACTIVITIES = frozenset(
    {
        "idle", "thinking", "planning", "coding", "tooling", "testing",
        "reviewing", "waiting", "finalizing", "unknown",
    }
)
_WAIT_REASONS = frozenset(
    {
        "user_input", "approval", "authentication", "tool", "child_run",
        "network", "rate_limit", "retry_backoff", "dependency", "resource",
        "unknown",
    }
)
_OUTCOMES = frozenset({"succeeded", "failed", "cancelled"})
_KINDS = frozenset(
    {
        "generic", "codex", "claude", "kimi", "deepseek", "tool", "file",
        "image", "log", "subagent", "phase",
    }
)
_PROVIDER_STATUSES = frozenset({"completed", "failed"})
_PUBLIC_ADAPTERS = frozenset(
    {
        "agentserver",
        "claude",
        "codex",
        "deepseek",
        "generic",
        "kimi",
        "observation-draft",
        "terminal-manager",
    }
)
_PUBLIC_CODES = frozenset(
    {
        "agent_failed",
        "authentication_failed",
        "codex_runtime_error",
        "invalid_transition",
        "provider_error",
        "rate_limited",
        "resource_exhausted",
        "revision_conflict",
        "runtime_error",
        "timeout",
        "tool_failed",
        "unknown",
        "validation_error",
    }
)
_PUBLIC_REASONS = frozenset(
    {
        "agent_lost",
        "agent_unreachable",
        "clear",
        "completed",
        "dependency_cancelled",
        "failed",
        "heartbeat_grace_expired",
        "heartbeat_lease_expired",
        "logout",
        "other",
        "prompt_input_exit",
        "run_lost",
        "server_requested",
        "shutdown",
        "timeout",
        "unknown",
        "user_requested",
    }
)


def _rfc3339(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _resolve_pseudonym_key(
    value: bytes | None, *, protocol: str, sink_id: str | None, tenant_id: str
) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("pseudonym_key must be at least 32 bytes")
    if (
        not isinstance(sink_id, str)
        or not sink_id.strip()
        or len(sink_id.encode("utf-8")) > 1024
    ):
        raise ValueError("sink_id must identify one stable receiving boundary")
    return hmac.new(
        value,
        (
            "agentserver-export-sink-v2\0"
            f"{protocol}\0{sink_id.strip()}\0{tenant_id}"
        ).encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _pseudonym_digest(
    namespace: str, value: str, *, key: bytes, length: int
) -> str:
    return hmac.new(
        key,
        f"agentserver-export-v1\0{namespace}\0{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:length]


def _pseudonym(
    namespace: str, value: str, *, key: bytes, length: int = 32
) -> str:
    digest = _pseudonym_digest(namespace, value, key=key, length=length)
    return f"{namespace}-{digest}"


def _safe_machine_code(value: object, *, default: str = "unknown") -> str:
    result = str(value or "").strip().lower()
    if not result or len(result) > 120 or not _MACHINE_CODE.fullmatch(result):
        return default
    return result


def _private_payload(
    event: StoredEvent, *, pseudonym_key: bytes | None
) -> dict[str, Any]:
    payload = event.payload
    result: dict[str, Any] = {}
    for name, allowed in (
        ("activity", _ACTIVITIES),
        ("wait_reason", _WAIT_REASONS),
        ("outcome", _OUTCOMES),
        ("kind", _KINDS),
        ("provider_status", _PROVIDER_STATUSES),
    ):
        value = str(payload.get(name) or "").strip().lower()
        if value in allowed:
            result[name] = value
    for name, allowed in (("code", _PUBLIC_CODES), ("reason", _PUBLIC_REASONS)):
        value = payload.get(name)
        if value is not None:
            code = _safe_machine_code(value, default="")
            if code in allowed:
                result[name] = code
    for name in ("current", "total"):
        value = payload.get(name)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            result[name] = value
    progress = payload.get("progress")
    if (
        isinstance(progress, (int, float))
        and not isinstance(progress, bool)
        and math.isfinite(progress)
        and 0 <= progress <= 1
    ):
        result["progress"] = progress
    wait_target = payload.get("wait_target_run_id")
    if isinstance(wait_target, str) and wait_target:
        result["wait_target_run_id"] = (
            wait_target
            if pseudonym_key is None
            else _pseudonym("run", wait_target, key=pseudonym_key)
        )
    return result


def _private_scope(
    event: StoredEvent, *, pseudonym_key: bytes
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name, value in event.scope.as_dict().items():
        if value is None:
            result[name] = None
            continue
        namespace = {
            "owner_id": "owner",
            "agent_instance_id": "agent",
            "parent_run_id": "run",
        }.get(name, name.removesuffix("_id"))
        result[name] = _pseudonym(namespace, value, key=pseudonym_key)
    return result


def _private_event_data(
    event: StoredEvent, *, pseudonym_key: bytes
) -> dict[str, Any]:
    envelope = event.envelope
    producer = envelope.producer
    aggregate_kind = _safe_machine_code(event.aggregate_kind, default="unknown")
    aggregate_id = (
        _pseudonym(aggregate_kind, event.aggregate_id, key=pseudonym_key)
        if event.aggregate_id
        else None
    )
    return {
        "schema": envelope.schema,
        "event_id": _pseudonym("event", event.id, key=pseudonym_key),
        "type": _safe_machine_code(event.type),
        "scope": _private_scope(event, pseudonym_key=pseudonym_key),
        "producer": {
            "id": _pseudonym("producer", producer.id, key=pseudonym_key),
            "epoch": _pseudonym("epoch", producer.epoch, key=pseudonym_key),
            "seq": producer.seq,
            "adapter": (
                producer.adapter
                if producer.adapter in _PUBLIC_ADAPTERS
                else "unknown"
            ),
            "mode": producer.mode.value,
        },
        "expected_revision": envelope.expected_revision,
        "occurred_at": (
            envelope.occurred_at
            if isinstance(envelope.occurred_at, (int, float))
            and not isinstance(envelope.occurred_at, bool)
            else None
        ),
        "causation_id": (
            _pseudonym("event", envelope.causation_id, key=pseudonym_key)
            if envelope.causation_id
            else None
        ),
        "correlation_id": (
            _pseudonym(
                "correlation", envelope.correlation_id, key=pseudonym_key
            )
            if envelope.correlation_id
            else None
        ),
        "traceparent": None,
        "evidence": envelope.evidence.as_dict() if envelope.evidence else None,
        "payload": _private_payload(event, pseudonym_key=pseudonym_key),
        "stream_version": event.stream_version,
        "recorded_at": event.recorded_at,
        "aggregate_kind": aggregate_kind,
        "aggregate_id": aggregate_id,
    }


def to_cloudevent(
    event: StoredEvent,
    *,
    include_sensitive_data: bool = False,
    pseudonym_key: bytes | None = None,
    sink_id: str | None = None,
) -> dict[str, Any]:
    """Wrap an immutable event in CloudEvents 1.0 structured JSON.

    The default keeps the envelope/CAS/evidence fields but allowlists only
    categorical and numeric payload values.  Full payload export is an explicit
    opt-in because summaries, artifact paths and provider metadata may contain
    tenant data; callers choosing it must enforce native owner authorization.
    Default/private export requires a persistent secret ``pseudonym_key`` of at
    least 32 bytes so replay identity survives restarts. ``sink_id`` must name
    one stable receiving boundary; the key is domain-separated by protocol,
    receiving boundary, tenant and identifier kind.
    """
    if include_sensitive_data:
        key = None
        aggregate = (
            f"{event.aggregate_kind}/{event.aggregate_id}"
            if event.aggregate_kind and event.aggregate_id
            else f"owner/{event.scope.owner_id}"
        )
        event_id = event.id
        source_owner = quote(event.scope.owner_id, safe="")
        data = event.as_dict()
    else:
        key = _resolve_pseudonym_key(
            pseudonym_key,
            protocol="cloudevent",
            sink_id=sink_id,
            tenant_id=event.scope.owner_id,
        )
        aggregate_kind = _safe_machine_code(event.aggregate_kind, default="owner")
        aggregate_value = event.aggregate_id or event.scope.owner_id
        aggregate = (
            f"{aggregate_kind}/"
            f"{_pseudonym(aggregate_kind, aggregate_value, key=key)}"
        )
        event_id = _pseudonym("event", event.id, key=key)
        source_owner = _pseudonym("owner", event.scope.owner_id, key=key)
        data = _private_event_data(event, pseudonym_key=key)
    exported_type = (
        event.type if include_sensitive_data else _safe_machine_code(event.type)
    )
    result = {
        "specversion": "1.0",
        "id": event_id,
        "source": f"urn:agentserver:owner:{source_owner}",
        "type": f"dev.agentserver.{exported_type}",
        "subject": aggregate,
        "time": _rfc3339(event.recorded_at),
        "datacontenttype": "application/json",
        "dataschema": "https://agentserver.dev/schemas/event-1.json",
        "data": data,
    }
    if include_sensitive_data:
        result["sequence"] = event.global_sequence
    return result


def _trace_ids(
    event: StoredEvent,
    *,
    include_sensitive_data: bool,
    pseudonym_key: bytes | None,
) -> tuple[str, str, str | None]:
    traceparent = event.envelope.traceparent
    if traceparent:
        fields = traceparent.split("-")
        if (
            len(fields) == 4
            and len(fields[1]) == 32
            and len(fields[2]) == 16
            and all(character in "0123456789abcdefABCDEF" for character in fields[1] + fields[2])
        ):
            if include_sensitive_data:
                return fields[1].lower(), fields[2].lower(), traceparent
            return (
                _pseudonym_digest(
                    "trace", fields[1].lower(), key=pseudonym_key, length=32
                ),
                _pseudonym_digest(
                    "span", fields[2].lower(), key=pseudonym_key, length=16
                ),
                None,
            )
    trace_seed = event.scope.run_id or event.scope.task_id or event.id
    span_seed = event.scope.span_id or event.id
    if include_sensitive_data:
        return (
            hashlib.sha256(f"trace\0{trace_seed}".encode("utf-8")).hexdigest()[:32],
            hashlib.sha256(f"span\0{span_seed}".encode("utf-8")).hexdigest()[:16],
            None,
        )
    assert pseudonym_key is not None
    return (
        _pseudonym_digest("trace", trace_seed, key=pseudonym_key, length=32),
        _pseudonym_digest("span", span_seed, key=pseudonym_key, length=16),
        None,
    )


def to_otel_log_record(
    event: StoredEvent,
    *,
    include_sensitive_data: bool = False,
    pseudonym_key: bytes | None = None,
    sink_id: str | None = None,
) -> dict[str, Any]:
    """Map an event to a privacy-minimized OTLP-compatible log record.

    The record excludes summary, title, descriptions, prompts, terminal output,
    commands, tool arguments and artifacts. Default/private export requires a
    persistent secret 32-byte ``pseudonym_key`` and a stable receiving-boundary
    ``sink_id``; IDs are domain-separated HMAC pseudonyms suitable for replay
    correlation within that boundary only.
    """
    key = (
        None
        if include_sensitive_data
        else _resolve_pseudonym_key(
            pseudonym_key,
            protocol="otel",
            sink_id=sink_id,
            tenant_id=event.scope.owner_id,
        )
    )
    trace_id, span_id, traceparent = _trace_ids(
        event,
        include_sensitive_data=include_sensitive_data,
        pseudonym_key=key,
    )
    scope = event.scope
    event_type = _safe_machine_code(event.type)
    attributes: dict[str, Any] = {
        "agentserver.event.type": event_type,
        "agentserver.event.id": (
            event.id
            if include_sensitive_data
            else _pseudonym("event", event.id, key=key)
        ),
        "agentserver.owner.id": (
            scope.owner_id
            if include_sensitive_data
            else _pseudonym("owner", scope.owner_id, key=key)
        ),
        "agentserver.producer.mode": event.producer.mode.value,
    }
    for name, namespace, value in (
        ("device.id", "device", scope.device_id),
        ("terminal.id", "terminal", scope.terminal_id),
        ("agent.id", "agent", scope.agent_instance_id),
        ("task.id", "task", scope.task_id),
        ("assignment.id", "assignment", scope.assignment_id),
        ("run.id", "run", scope.run_id),
        ("run.parent_id", "run", scope.parent_run_id),
        ("span.id", "span", scope.span_id),
    ):
        if value:
            attributes[f"agentserver.{name}"] = (
                value
                if include_sensitive_data
                else _pseudonym(namespace, value, key=key)
            )
    for name, value in _private_payload(event, pseudonym_key=key).items():
        attributes[f"agentserver.{name}"] = value
    if traceparent:
        attributes["w3c.traceparent"] = traceparent
    failed = event.type.endswith(".failed") or event.payload.get("outcome") == "failed"
    return {
        "timeUnixNano": int(event.recorded_at * 1_000_000_000),
        "observedTimeUnixNano": int(event.recorded_at * 1_000_000_000),
        "severityNumber": 17 if failed else 9,
        "severityText": "ERROR" if failed else "INFO",
        "body": {"stringValue": event_type},
        "attributes": [
            {"key": key, "value": _otel_value(value)}
            for key, value in sorted(attributes.items())
        ],
        "traceId": trace_id,
        "spanId": span_id,
    }


def _otel_value(value: str | int | float) -> dict[str, Any]:
    if isinstance(value, int) and not isinstance(value, bool):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


_A2A_TASK_STATES = {
    "submitted": "TASK_STATE_SUBMITTED",
    "assigned": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "input_required": "TASK_STATE_INPUT_REQUIRED",
    "auth_required": "TASK_STATE_AUTH_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "canceled": "TASK_STATE_CANCELED",
    "rejected": "TASK_STATE_REJECTED",
}


def to_a2a_task(
    task: Mapping[str, Any],
    *,
    include_sensitive_data: bool = False,
    pseudonym_key: bytes | None = None,
    sink_id: str | None = None,
) -> dict[str, Any]:
    """Map an execution-view Task to A2A without replacing tenant ACLs.

    IDs and owner metadata use sink- and tenant-scoped HMAC pseudonyms. Default
    export requires a persistent secret 32-byte ``pseudonym_key`` and a stable
    receiving-boundary ``sink_id``. A caller may opt into lossless identifiers
    only after authenticating the peer and enforcing the native owner/tenant
    authorization boundary. This mapper does not replace the caller's tenant
    ACL enforcement.
    """
    attributes = task.get("attributes")
    state = task.get("state")
    if not isinstance(attributes, Mapping) or not isinstance(state, Mapping):
        raise ValueError("execution Task requires attributes and state objects")
    lifecycle = str(state.get("lifecycle") or "")
    task_id = str(task.get("id") or "")
    if not task_id or lifecycle not in _A2A_TASK_STATES:
        raise ValueError("execution Task has no mappable identity/lifecycle")
    timestamp = float(task.get("updated_at") or task.get("created_at") or 0)
    context_id = str(attributes.get("context_id") or task_id)
    owner_id = str(task.get("owner_id") or "")
    if not owner_id:
        raise ValueError("execution Task requires an owner identity for tenant ACLs")
    key = (
        None
        if include_sensitive_data
        else _resolve_pseudonym_key(
            pseudonym_key,
            protocol="a2a",
            sink_id=sink_id,
            tenant_id=owner_id,
        )
    )
    result = {
        "id": (
            task_id
            if include_sensitive_data
            else _pseudonym("task", task_id, key=key)
        ),
        "contextId": (
            context_id
            if include_sensitive_data
            else _pseudonym("context", context_id, key=key)
        ),
        "status": {
            "state": _A2A_TASK_STATES[lifecycle],
            "timestamp": _rfc3339(timestamp),
        },
        "metadata": {
            "agentserver.owner_id": (
                owner_id
                if include_sensitive_data
                else _pseudonym("owner", owner_id, key=key)
            ),
            "agentserver.revision": int(task.get("revision") or 0),
        },
    }
    # Task text and conversation history remain opt-in at an authenticated A2A
    # boundary; this core mapper never leaks them by default.
    return result


_MCP_TASK_STATES = {
    "submitted": "working",
    "assigned": "working",
    "working": "working",
    "input_required": "input_required",
    "auth_required": "input_required",
    "completed": "completed",
    "failed": "failed",
    "canceled": "cancelled",
    "rejected": "failed",
}


def to_mcp_task(
    task: Mapping[str, Any],
    *,
    ttl_ms: int = 60_000,
    poll_interval_ms: int = 1_000,
    include_sensitive_data: bool = False,
    pseudonym_key: bytes | None = None,
    sink_id: str | None = None,
) -> dict[str, Any]:
    """Map to the experimental MCP task shape without replacing tenant ACLs.

    Default export requires a persistent secret 32-byte ``pseudonym_key`` and
    stable receiving-boundary ``sink_id``. It emits a protocol-, boundary- and
    tenant-scoped HMAC task ID. Lossless identifiers require an authenticated,
    owner-authorized caller. This mapper does not replace the caller's tenant
    ACL enforcement.
    """
    state = task.get("state")
    if not isinstance(state, Mapping):
        raise ValueError("execution Task requires a state object")
    lifecycle = str(state.get("lifecycle") or "")
    task_id = str(task.get("id") or "")
    if not task_id or lifecycle not in _MCP_TASK_STATES:
        raise ValueError("execution Task has no mappable identity/lifecycle")
    owner_id = str(task.get("owner_id") or "")
    if not owner_id:
        raise ValueError("execution Task requires an owner identity for tenant ACLs")
    key = (
        None
        if include_sensitive_data
        else _resolve_pseudonym_key(
            pseudonym_key,
            protocol="mcp",
            sink_id=sink_id,
            tenant_id=owner_id,
        )
    )
    created = float(task.get("created_at") or task.get("updated_at") or 0)
    updated = float(task.get("updated_at") or created)
    return {
        "taskId": (
            task_id
            if include_sensitive_data
            else _pseudonym("task", task_id, key=key)
        ),
        "status": _MCP_TASK_STATES[lifecycle],
        "createdAt": _rfc3339(created),
        "lastUpdatedAt": _rfc3339(updated),
        "ttl": max(1, int(ttl_ms)),
        "pollInterval": max(1, int(poll_interval_ms)),
    }


def to_mcp_progress(
    run: Mapping[str, Any], *, progress_token: str | int
) -> dict[str, Any] | None:
    """Create an MCP progress notification only when numeric progress exists."""
    state = run.get("state")
    if not isinstance(state, Mapping) or "progress" not in state:
        return None
    progress = state.get("current", state.get("progress"))
    total = state.get("total")
    if not isinstance(progress, (int, float)) or isinstance(progress, bool):
        return None
    params: dict[str, Any] = {
        "progressToken": progress_token,
        "progress": progress,
    }
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        params["total"] = total
    return {
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": params,
    }
