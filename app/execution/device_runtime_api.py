from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .device_runtime import (
    MAX_EVENT_BATCH_BYTES,
    MAX_EVENT_BATCH_SIZE,
    MAX_SQLITE_INTEGER,
    DeviceRuntimeAuthenticationError,
    DeviceRuntimeConflict,
    DeviceRuntimeError,
    DeviceRuntimeFenceError,
    DeviceRuntimeNotFound,
    DeviceRuntimeService,
    RuntimeSession,
)
from .errors import CommandConflict, ExecutionError, ValidationError
from .models import CommandStatus


MAX_DEVICE_BODY_BYTES = 64 * 1024
MAX_BROWSER_BODY_BYTES = 64 * 1024
MAX_CREDENTIAL_BYTES = 4096


class EnrollmentTokenBody(BaseModel):
    ttl_seconds: float = Field(default=300, ge=1, le=24 * 60 * 60)


class DeviceEnrollBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enrollment_token: str = Field(min_length=1, max_length=MAX_CREDENTIAL_BYTES)
    device_id: str | None = Field(default=None, max_length=255)
    instance_id: str | None = Field(default=None, max_length=255)
    boot_id: str | None = Field(default=None, max_length=255)


class DeviceHeartbeatBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    runtime_session_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1, le=MAX_SQLITE_INTEGER)
    protocol_version: int = Field(default=1, ge=1, le=1_000_000)
    runtime_version: str = Field(default="", max_length=100)
    health: Literal["healthy", "degraded"] = "healthy"
    last_error: str = Field(default="", max_length=1000)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    platform: dict[str, Any] = Field(default_factory=dict)
    instance_id: str = Field(min_length=1, max_length=255)
    boot_id: str = Field(min_length=1, max_length=255)


class CredentialRotateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    runtime_session_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1, le=MAX_SQLITE_INTEGER)
    request_id: str = Field(min_length=1, max_length=255)


class DeviceCommandAckBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    runtime_session_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1, le=MAX_SQLITE_INTEGER)
    status: Literal["accepted", "rejected", "completed"]
    ack_id: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)


class DeviceEventBatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    runtime_session_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1, le=MAX_SQLITE_INTEGER)
    events: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_EVENT_BATCH_SIZE)


class RuntimeSessionCreateBody(BaseModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str = Field(default="codex", min_length=1, max_length=64)
    cwd: str = Field(default=".", min_length=1, max_length=4096)
    permission_mode: Literal[
        "approval-required", "workspace-write", "full-access", "auto"
    ] = "workspace-write"
    model: str | None = Field(default=None, max_length=255)
    service_tier: str | None = Field(default=None, max_length=100)
    resume_cursor: dict[str, Any] | None = None


class RuntimeTurnBody(BaseModel):
    input: str = Field(min_length=1, max_length=49_152)
    turn_id: str | None = Field(default=None, max_length=255)
    # Model selection is session-scoped in v1. It remains accepted here so an
    # older/newer UI can call the same endpoint without leaking it into text.
    model: str | None = Field(default=None, max_length=255)


class RuntimeInterruptBody(BaseModel):
    turn_id: str | None = Field(default=None, max_length=255)


def _service(request: Request) -> DeviceRuntimeService:
    service = getattr(request.app.state, "device_runtime", None)
    if not isinstance(service, DeviceRuntimeService):
        raise HTTPException(status_code=503, detail="device runtime is unavailable")
    return service


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, DeviceRuntimeAuthenticationError):
        return HTTPException(status_code=401, detail=str(error))
    if isinstance(error, DeviceRuntimeNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(
        error,
        (DeviceRuntimeFenceError, DeviceRuntimeConflict, CommandConflict),
    ):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (ValidationError, ValueError)):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, (DeviceRuntimeError, ExecutionError)):
        return HTTPException(status_code=500, detail="device runtime operation failed")
    return HTTPException(status_code=500, detail="device runtime operation failed")


def _bearer(request: Request, service: DeviceRuntimeService):
    authorization = request.headers.get("authorization", "")
    if len(authorization.encode("utf-8")) > MAX_CREDENTIAL_BYTES + 16:
        raise HTTPException(status_code=401, detail="device credential is malformed")
    try:
        return service.authenticate(authorization, bearer=True)
    except (DeviceRuntimeError, ExecutionError, ValueError) as error:
        raise _http_error(error) from error


async def _bounded_json(request: Request, *, maximum: int) -> Mapping[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum:
            raise HTTPException(status_code=413, detail="request body is too large")
    try:
        value = json.loads(
            bytes(raw) or b"{}",
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number is not allowed: {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from error
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=422, detail="request body must be an object")
    return value


def public_runtime_status(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only display-safe host metadata and stable compatibility aliases."""

    capabilities = value.get("capabilities")
    if not isinstance(capabilities, Mapping):
        capabilities = {"providers": [], "features": []}
    providers = capabilities.get("providers")
    if not isinstance(providers, list):
        providers = []
    state = str(value.get("state") or "unregistered")
    if value.get("health") == "revoked":
        state = "revoked"
    if state not in {"unregistered", "online", "degraded", "offline", "revoked"}:
        state = "offline"
    return {
        "state": state,
        "online": bool(value.get("online", state == "online")),
        "device_id": str(value.get("device_id") or ""),
        "runtime_session_id": str(value.get("runtime_session_id") or ""),
        "instance_id": str(value.get("instance_id") or ""),
        "boot_id": str(value.get("boot_id") or ""),
        "generation": int(value.get("generation") or 0),
        "protocol_version": int(value.get("protocol_version") or 0),
        "runtime_version": str(value.get("runtime_version") or ""),
        "host_version": str(value.get("runtime_version") or ""),
        "health": str(value.get("health") or ""),
        "last_error": str(value.get("last_error") or ""),
        "capabilities": dict(capabilities),
        "providers": providers,
        "platform": dict(value.get("platform") or {}),
        "revision": int(value.get("revision") or 0),
        "connected_at": value.get("connected_at"),
        "last_seen_at": value.get("last_seen_at"),
        "online_until": value.get("online_until"),
        "lease_expires_at": value.get("online_until"),
    }


def public_runtime_session(session: RuntimeSession) -> dict[str, Any]:
    value = session.as_dict()
    attributes = value.get("attributes")
    options = attributes.get("options") if isinstance(attributes, Mapping) else None
    if not isinstance(options, Mapping):
        options = {}
    return {
        **value,
        "id": session.session_id,
        "state": session.lifecycle,
        "cwd": session.workspace,
        "permission_mode": str(options.get("permission_mode") or "workspace-write"),
        "model": str(options["model"]) if options.get("model") else None,
        "resume_cursor": options.get("resume_cursor"),
        "active_turn_id": (
            str(attributes["active_turn_id"])
            if isinstance(attributes, Mapping) and attributes.get("active_turn_id")
            else None
        ),
    }


def _event_for_ingest(
    event: Mapping[str, Any],
    *,
    claims_device_id: str,
    runtime_session_id: str,
    generation: int,
) -> tuple[str, int, dict[str, Any]]:
    if event.get("schema") != "agentserver.device-runtime-event/1":
        raise ValidationError("runtime event schema is unsupported")
    event_id = event.get("event_id")
    if (
        not isinstance(event_id, str)
        or event_id != event_id.strip()
        or not 1 <= len(event_id) <= 255
    ):
        raise ValidationError("runtime event_id must contain 1..255 characters")
    event_type = event.get("type")
    if (
        not isinstance(event_type, str)
        or event_type != event_type.strip()
        or not 1 <= len(event_type) <= 120
    ):
        raise ValidationError("runtime event type must contain 1..120 characters")
    device_id = str(event.get("device_id") or "")
    if not device_id:
        raise ValidationError("runtime event requires device_id")
    if device_id != claims_device_id:
        raise DeviceRuntimeFenceError("runtime event is outside device scope")
    event_runtime_session_id = str(event.get("runtime_session_id") or "")
    if not event_runtime_session_id:
        raise ValidationError("runtime event requires runtime_session_id")
    if event_runtime_session_id != runtime_session_id:
        raise DeviceRuntimeFenceError("runtime event host session fence is stale")
    event_generation = event.get("generation")
    if (
        not isinstance(event_generation, int)
        or isinstance(event_generation, bool)
        or not 1 <= event_generation <= MAX_SQLITE_INTEGER
    ):
        raise ValidationError("runtime event generation must be a positive int64")
    if event_generation != generation:
        raise DeviceRuntimeFenceError("runtime event host generation is stale")
    session_id_value = event.get("session_id")
    if (
        not isinstance(session_id_value, str)
        or session_id_value != session_id_value.strip()
        or not 1 <= len(session_id_value) <= 255
    ):
        raise ValidationError("runtime event session_id must contain 1..255 characters")
    session_id = session_id_value
    producer = event.get("producer")
    if not isinstance(producer, Mapping):
        raise ValidationError("runtime event requires producer metadata")
    producer_epoch = producer.get("epoch")
    if (
        not isinstance(producer_epoch, str)
        or producer_epoch != producer_epoch.strip()
        or not 1 <= len(producer_epoch) <= 255
    ):
        raise ValidationError("runtime event producer epoch is invalid")
    producer_seq = producer.get("seq")
    if (
        not isinstance(producer_seq, int)
        or isinstance(producer_seq, bool)
        or producer_seq < 0
        or producer_seq > MAX_SQLITE_INTEGER
    ):
        raise ValidationError("runtime event producer seq must be non-negative")
    payload = event.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise ValidationError("runtime event payload must be an object")
    canonical = {
        "event_id": event_id,
        "producer_seq": producer_seq,
        "type": event_type,
        "payload": dict(payload),
        "occurred_at": event.get("occurred_at"),
    }
    return session_id, producer_seq, canonical


def build_device_runtime_router(
    browser_user_dependency: Callable[..., str],
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/devices/{device_id}/runtime/enrollment-tokens", status_code=201)
    async def issue_enrollment(
        device_id: str,
        body: EnrollmentTokenBody,
        request: Request,
        response: Response,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            grant = await asyncio.to_thread(
                _service(request).issue_enrollment,
                owner_id=username,
                device_id=device_id,
                ttl=body.ttl_seconds,
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return grant.as_dict()
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.get("/api/devices/{device_id}/runtime")
    async def runtime_status(
        device_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            value = await asyncio.to_thread(
                _service(request).runtime_status,
                owner_id=username,
                device_id=device_id,
            )
            return public_runtime_status(value)
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.delete("/api/devices/{device_id}/runtime/credential")
    async def revoke_runtime(
        device_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            # Checking status first preserves a 404 for an unknown inventory
            # device while revoke itself remains idempotent.
            await asyncio.to_thread(
                _service(request).runtime_status,
                owner_id=username,
                device_id=device_id,
            )
            count = await asyncio.to_thread(
                _service(request).revoke_device,
                owner_id=username,
                device_id=device_id,
            )
            return {"ok": True, "revoked_credentials": count}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post("/api/devices/{device_id}/runtime/probe", status_code=202)
    async def probe_runtime(
        device_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            command = await asyncio.to_thread(
                _service(request).enqueue_device_command,
                owner_id=username,
                device_id=device_id,
                command_type="runtime.probe",
                payload={},
                ttl=60,
            )
            return {"command": command.as_dict()}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.get("/api/devices/{device_id}/runtime/sessions")
    async def list_runtime_sessions(
        device_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        try:
            # Do not let an arbitrary device id become an owner-side filter
            # oracle; require the inventory device before listing.
            await asyncio.to_thread(
                _service(request).runtime_status,
                owner_id=username,
                device_id=device_id,
            )
            sessions = await asyncio.to_thread(
                _service(request).list_sessions,
                owner_id=username,
                device_id=device_id,
                limit=limit,
            )
            return {"sessions": [public_runtime_session(item) for item in sessions]}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post("/api/devices/{device_id}/runtime/sessions", status_code=201)
    async def create_runtime_session(
        device_id: str,
        body: RuntimeSessionCreateBody,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            session = await asyncio.to_thread(
                _service(request).create_session,
                owner_id=username,
                device_id=device_id,
                provider=body.provider,
                workspace=body.cwd,
                session_id=body.session_id,
                options={
                    "permission_mode": body.permission_mode,
                    "model": body.model,
                    "service_tier": body.service_tier,
                    "resume_cursor": body.resume_cursor,
                },
            )
            command = await asyncio.to_thread(
                _service(request).execution_store.command_queue.get,
                owner_id=username,
                command_id=session.start_command_id,
            )
            return {
                "session": public_runtime_session(session),
                "command": command.as_dict() if command is not None else None,
            }
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.get("/api/runtime-sessions/{session_id}")
    async def get_runtime_session(
        session_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            session = await asyncio.to_thread(
                _service(request).get_session,
                owner_id=username,
                session_id=session_id,
            )
            return {"session": public_runtime_session(session)}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post("/api/runtime-sessions/{session_id}/turns", status_code=202)
    async def start_runtime_turn(
        session_id: str,
        body: RuntimeTurnBody,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            command = await asyncio.to_thread(
                _service(request).send_turn,
                owner_id=username,
                session_id=session_id,
                input=body.input,
                turn_id=body.turn_id,
            )
            return {"command": command.as_dict()}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post("/api/runtime-sessions/{session_id}/interrupt", status_code=202)
    async def interrupt_runtime_turn(
        session_id: str,
        body: RuntimeInterruptBody,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            command = await asyncio.to_thread(
                _service(request).interrupt_session,
                owner_id=username,
                session_id=session_id,
                turn_id=body.turn_id,
            )
            return {"command": command.as_dict()}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post(
        "/api/runtime-sessions/{session_id}/interactions/{interaction_id}/respond",
        status_code=202,
    )
    async def respond_runtime_interaction(
        session_id: str,
        interaction_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        value = await _bounded_json(request, maximum=MAX_BROWSER_BODY_BYTES)
        try:
            command = await asyncio.to_thread(
                _service(request).respond_to_request,
                owner_id=username,
                session_id=session_id,
                request_id=interaction_id,
                response=value,
            )
            return {"command": command.as_dict()}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.delete("/api/runtime-sessions/{session_id}", status_code=202)
    async def stop_runtime_session(
        session_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            command = await asyncio.to_thread(
                _service(request).stop_session,
                owner_id=username,
                session_id=session_id,
            )
            return {"command": command.as_dict()}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.get("/api/runtime-sessions/{session_id}/events")
    async def runtime_session_events(
        session_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
        after_sequence: int = Query(default=0, ge=0, le=MAX_SQLITE_INTEGER),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        try:
            events = await asyncio.to_thread(
                _service(request).session_events,
                owner_id=username,
                session_id=session_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            return {"events": [item.as_dict() for item in events]}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post("/api/device-runtime/v1/enroll", status_code=201)
    async def device_enroll(request: Request, response: Response) -> dict[str, Any]:
        value = await _bounded_json(request, maximum=MAX_DEVICE_BODY_BYTES)
        try:
            body = DeviceEnrollBody.model_validate(value)
            grant = await asyncio.to_thread(
                _service(request).consume_enrollment,
                body.enrollment_token,
            )
            if body.device_id and body.device_id != grant.claims.device_id:
                # The credential has already been created; revoke it before
                # rejecting the mismatched enrollment response.
                await asyncio.to_thread(
                    _service(request).revoke_device,
                    owner_id=grant.claims.owner_id,
                    device_id=grant.claims.device_id,
                    credential_id=grant.claims.credential_id,
                )
                raise DeviceRuntimeFenceError("enrollment device id does not match")
            result = grant.as_dict()
            result.update(
                credential=grant.token,
                device_credential=grant.token,
                server_time=_service(request).clock(),
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return result
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post("/api/device-runtime/v1/heartbeat")
    async def device_heartbeat(request: Request) -> dict[str, Any]:
        value = await _bounded_json(request, maximum=MAX_DEVICE_BODY_BYTES)
        service = _service(request)
        claims = _bearer(request, service)
        try:
            body = DeviceHeartbeatBody.model_validate(value)
            host = await asyncio.to_thread(
                service.heartbeat,
                claims,
                instance_id=body.instance_id,
                boot_id=body.boot_id,
                runtime_session_id=body.runtime_session_id,
                generation=body.generation,
                capabilities=body.capabilities,
                protocol_version=body.protocol_version,
                runtime_version=body.runtime_version,
                platform=body.platform,
                health=body.health,
                last_error=body.last_error,
            )
            return {
                "runtime": public_runtime_status(host.as_dict(now=service.clock())),
                "credential_expires_at": claims.expires_at,
                "server_time": service.clock(),
            }
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.get("/api/device-runtime/v1/commands")
    async def device_commands(
        request: Request,
        runtime_session_id: str = Query(min_length=1, max_length=255),
        generation: int = Query(ge=1, le=MAX_SQLITE_INTEGER),
        after_sequence: int = Query(default=0, ge=0, le=MAX_SQLITE_INTEGER),
        wait_ms: int = Query(default=0, ge=0, le=30_000),
        limit: int = Query(default=100, ge=1, le=100),
        instance_id: str | None = Query(default=None, max_length=255),
        boot_id: str | None = Query(default=None, max_length=255),
    ) -> dict[str, Any]:
        del instance_id, boot_id  # display-only metadata, never an auth fence
        service = _service(request)
        claims = _bearer(request, service)
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        page = None
        while True:
            try:
                page = await asyncio.to_thread(
                    service.poll_commands,
                    claims,
                    runtime_session_id=runtime_session_id,
                    generation=generation,
                    after_sequence=after_sequence,
                    limit=limit,
                )
            except (DeviceRuntimeError, ExecutionError, ValueError) as error:
                raise _http_error(error) from error
            if page.commands or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(
                min(0.25, max(0.0, deadline - asyncio.get_running_loop().time()))
            )
        assert page is not None
        return {**page.as_dict(), "server_time": service.clock()}

    @router.post("/api/device-runtime/v1/commands/{command_id}/ack")
    async def device_command_ack(command_id: str, request: Request) -> dict[str, Any]:
        value = await _bounded_json(request, maximum=MAX_DEVICE_BODY_BYTES)
        service = _service(request)
        claims = _bearer(request, service)
        try:
            body = DeviceCommandAckBody.model_validate(value)
            command = await asyncio.to_thread(
                service.ack_command,
                claims,
                runtime_session_id=body.runtime_session_id,
                generation=body.generation,
                command_id=command_id,
                status=CommandStatus(body.status),
                ack_id=body.ack_id,
                payload=body.payload,
            )
            return {"command": command.as_dict(), "server_time": service.clock()}
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post("/api/device-runtime/v1/events:batch")
    async def device_events(request: Request) -> dict[str, Any]:
        value = await _bounded_json(request, maximum=MAX_EVENT_BATCH_BYTES)
        service = _service(request)
        claims = _bearer(request, service)
        try:
            body = DeviceEventBatchBody.model_validate(value)
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            producer_sequences: list[int] = []
            seen_sequences: set[int] = set()
            for raw in body.events:
                session_id, producer_seq, canonical = _event_for_ingest(
                    raw,
                    claims_device_id=claims.device_id,
                    runtime_session_id=body.runtime_session_id,
                    generation=body.generation,
                )
                if producer_seq in seen_sequences:
                    raise ValidationError(
                        "runtime event batch contains a duplicate producer seq"
                    )
                seen_sequences.add(producer_seq)
                groups[session_id].append(canonical)
                producer_sequences.append(producer_seq)
            accepted = await asyncio.to_thread(
                service.ingest_event_batch,
                claims,
                runtime_session_id=body.runtime_session_id,
                generation=body.generation,
                groups=groups,
            )
            results_by_sequence = {}
            for item in accepted:
                if item.producer_seq in results_by_sequence:
                    raise DeviceRuntimeError(
                        "device event ingestion returned duplicate results"
                    )
                results_by_sequence[item.producer_seq] = item.as_dict()
            if set(results_by_sequence) != seen_sequences:
                raise DeviceRuntimeError(
                    "device event ingestion did not classify every event"
                )
            results = [
                results_by_sequence[producer_seq]
                for producer_seq in producer_sequences
            ]
            accepted_through = max(producer_sequences, default=0)
            return {
                "accepted_through_seq": accepted_through,
                # v1 Hosts settle exclusively by the exact results above.
                # A sequence gap in a FIFO live delivery represents an event
                # already settled locally, not a server-side missing range.
                "missing_ranges": [],
                "results": results,
                "server_time": service.clock(),
            }
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    @router.post("/api/device-runtime/v1/credential:rotate")
    async def rotate_device_credential(
        request: Request, response: Response
    ) -> dict[str, Any]:
        value = await _bounded_json(request, maximum=MAX_DEVICE_BODY_BYTES)
        service = _service(request)
        authorization = request.headers.get("authorization", "")
        try:
            body = CredentialRotateBody.model_validate(value)
            grant = await asyncio.to_thread(
                service.rotate_credential,
                authorization,
                bearer=True,
                request_id=body.request_id,
                runtime_session_id=body.runtime_session_id,
                generation=body.generation,
            )
            result = grant.as_dict()
            result.update(
                credential=grant.token,
                device_credential=grant.token,
                server_time=service.clock(),
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return result
        except (DeviceRuntimeError, ExecutionError, ValueError) as error:
            raise _http_error(error) from error

    return router


__all__ = [
    "build_device_runtime_router",
    "public_runtime_session",
    "public_runtime_status",
]
