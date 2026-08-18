from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, WebSocket
from pydantic import BaseModel, Field, model_validator
from starlette.websockets import WebSocketDisconnect

from .errors import (
    CommandConflict,
    EntityNotFound,
    ExecutionError,
    IdempotencyConflict,
    InvalidTransition,
    LeaseConflict,
    RelationConstraintError,
    RevisionConflict,
    ValidationError,
)
from .events import EventEnvelope
from .models import CommandStatus, EntityKind, ResyncRequired
from .security import ReporterClaims, ReporterTokenError
from .service import ExecutionService


MAX_BATCH_EVENTS = 100
MAX_BATCH_BYTES = 256 * 1024
MAX_COMMAND_ACK_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096


class CreateTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=16_000)
    context_id: str | None = Field(default=None, max_length=255)
    parent_run_id: str | None = Field(default=None, max_length=255)
    deadline_at: float | None = Field(default=None, gt=0)
    token_budget: int | None = Field(default=None, gt=0)
    cost_budget_micros: int | None = Field(default=None, gt=0)
    max_child_runs: int | None = Field(default=None, ge=1, le=256)
    cancel_propagates: bool = True


class CreateTerminalTarget(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    workspace_root: str | None = Field(default=None, max_length=2048)
    cols: int = Field(default=120, ge=2, le=500)
    rows: int = Field(default=32, ge=1, le=300)


class AssignmentTarget(BaseModel):
    terminal_id: str | None = Field(default=None, max_length=255)
    device_id: str | None = Field(default=None, max_length=255)
    create_terminal: CreateTerminalTarget | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> "AssignmentTarget":
        existing = bool(self.terminal_id)
        create = self.create_terminal is not None
        if existing == create:
            raise ValueError("target must select an existing terminal or create_terminal")
        if create and not self.device_id:
            raise ValueError("create_terminal requires target.device_id")
        if existing and (self.device_id or self.create_terminal):
            raise ValueError("existing terminal target cannot include device creation fields")
        return self


class CreateAssignmentBody(BaseModel):
    expected_task_revision: int = Field(ge=0)
    agent_kind: str = Field(default="generic", min_length=1, max_length=40)
    target: AssignmentTarget
    parent_run_id: str | None = Field(default=None, max_length=255)


class RunInputBody(BaseModel):
    value: str = Field(max_length=65_536)


class CommandAckBody(BaseModel):
    status: Literal["accepted", "rejected", "completed"]
    ack_id: str | None = Field(default=None, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)


class HeartbeatBody(BaseModel):
    producer_id: str = Field(default="", max_length=255)


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, EntityNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ReporterTokenError):
        return HTTPException(status_code=401, detail=str(error))
    if isinstance(error, RevisionConflict):
        return HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": str(error),
                "expected_revision": error.expected,
                "current_revision": error.actual,
                "current_state": error.state,
            },
        )
    if isinstance(
        error,
        (InvalidTransition, IdempotencyConflict, LeaseConflict, RelationConstraintError, CommandConflict),
    ):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (ValidationError, ValueError)):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail="execution service failed")


def _service(request: Request) -> ExecutionService:
    return request.app.state.execution


async def _cleanup_created_terminal(
    request: Request,
    *,
    owner_id: str,
    terminal_id: str,
    launch_id: str,
) -> bool:
    """Best-effort exact-launch compensation for assignment orchestration.

    A terminal ID alone is not a safe deletion capability because a recovered
    or relaunched session may reuse that logical ID.  Both owner and launch ID
    must still match the just-created managed session.
    """
    cleanup = getattr(request.app.state, "delete_managed_terminal", None)
    if cleanup is not None:
        return bool(
            await cleanup(
                owner_id=owner_id,
                terminal_id=terminal_id,
                launch_id=launch_id,
            )
        )
    manager = getattr(request.app.state, "terminals", None)
    if manager is None:
        return False
    session = manager.get_for_owner(terminal_id, owner_id)
    if session is None:
        return False
    managed = (
        session.get("managed")
        if isinstance(session, Mapping)
        else getattr(session, "managed", False)
    )
    actual_launch_id = (
        session.get("launch_id")
        if isinstance(session, Mapping)
        else getattr(session, "launch_id", None)
    )
    if not managed or str(actual_launch_id or "") != launch_id:
        return False
    previews = getattr(request.app.state, "previews", None)
    if previews is not None:
        with contextlib.suppress(BaseException):
            await previews.delete_for_terminal(terminal_id)
    workspaces = getattr(request.app.state, "workspaces", None)
    if workspaces is not None:
        with contextlib.suppress(BaseException):
            await asyncio.to_thread(workspaces.unbind, owner_id, terminal_id)
    deleted = bool(await manager.delete(terminal_id))
    rate_windows = getattr(request.app.state, "artifact_rate_windows", None)
    if rate_windows is not None:
        rate_windows.pop(terminal_id, None)
    return deleted


def _bearer(request: Request, capability: str) -> ReporterClaims:
    authorization = request.headers.get("authorization", "")
    if len(authorization.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise HTTPException(status_code=401, detail="reporter token is too large")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing reporter bearer token")
    registry = request.app.state.reporter_tokens
    try:
        return registry.verify(token, capability=capability)
    except ReporterTokenError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error


def _projection_delta(view: dict[str, Any], event: Any) -> dict[str, Any]:
    keys = ("tasks", "assignments", "runs", "agents", "terminals")
    identifiers = {
        "tasks": event.scope.task_id,
        "assignments": event.scope.assignment_id,
        "runs": event.scope.run_id,
        "agents": event.scope.agent_instance_id,
        "terminals": event.scope.terminal_id,
    }
    key_by_kind = dict(
        task="tasks",
        assignment="assignments",
        run="runs",
        agent_instance="agents",
        terminal="terminals",
    )
    aggregate_key = key_by_kind.get(event.aggregate_kind)
    if aggregate_key and event.aggregate_id:
        identifiers[aggregate_key] = event.aggregate_id
    result: dict[str, Any] = {
        key: [
            item
            for item in view[key]
            if identifiers[key] is not None and item["id"] == identifiers[key]
        ]
        for key in keys
    }
    terminal_id = event.scope.terminal_id
    result["terminal_bindings"] = [
        item
        for item in view["terminal_bindings"]
        if terminal_id is not None and item["terminal_id"] == terminal_id
    ]
    result["relations"] = view["relations"]
    result["unattributed_observations"] = (
        view["unattributed_observations"]
        if event.type.startswith("observation.")
        and event.scope.terminal_id is None
        and event.scope.agent_instance_id is None
        and event.scope.run_id is None
        else []
    )
    return result


def build_execution_router(
    browser_user_dependency: Callable[..., str],
    verify_browser_session: Callable[[str | None], str | None],
    *,
    cookie_name: str,
) -> APIRouter:
    router = APIRouter()
    # All execution WebSockets for an owner observe the same committed log.
    # Cache a view by its durable cursor so replaying N events (or broadcasting
    # one event to N browser tabs) builds the expensive evidence view once.
    projection_views: dict[str, dict[str, Any]] = {}
    projection_view_locks: dict[str, asyncio.Lock] = {}

    async def projection_delta(
        service: ExecutionService,
        owner_id: str,
        event: Any,
    ) -> dict[str, Any]:
        required_sequence = int(event.global_sequence)
        view = projection_views.get(owner_id)
        if view is None or int(view.get("as_of_sequence", 0)) < required_sequence:
            lock = projection_view_locks.setdefault(owner_id, asyncio.Lock())
            async with lock:
                view = projection_views.get(owner_id)
                if view is None or int(view.get("as_of_sequence", 0)) < required_sequence:
                    view = await asyncio.to_thread(
                        service.execution_view,
                        owner_id=owner_id,
                    )
                    projection_views[owner_id] = view
        return _projection_delta(view, event)

    @router.post("/api/tasks", status_code=201)
    async def create_task(
        body: CreateTaskBody,
        request: Request,
        username: str = Depends(browser_user_dependency),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 255:
            raise HTTPException(status_code=422, detail="Idempotency-Key must be 1..255 characters")
        try:
            return await asyncio.to_thread(
                _service(request).create_task,
                owner_id=username,
                title=body.title,
                description=body.description,
                context_id=body.context_id,
                parent_run_id=body.parent_run_id,
                deadline_at=body.deadline_at,
                token_budget=body.token_budget,
                cost_budget_micros=body.cost_budget_micros,
                max_child_runs=body.max_child_runs,
                cancel_propagates=body.cancel_propagates,
                idempotency_key=idempotency_key,
            )
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.post("/api/tasks/{task_id}/assignments", status_code=201)
    async def assign_task(
        task_id: str,
        body: CreateAssignmentBody,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        created_terminal: tuple[str, str] | None = None
        try:
            terminal_id = body.target.terminal_id
            device_id: str | None = None
            if body.target.create_terminal is not None:
                await asyncio.to_thread(
                    _service(request).preflight_assignment,
                    owner_id=username,
                    task_id=task_id,
                    expected_task_revision=body.expected_task_revision,
                    parent_run_id=body.parent_run_id,
                )
                launch = getattr(request.app.state, "create_managed_terminal", None)
                if launch is None:
                    raise ValidationError("managed terminal launcher is unavailable")
                terminal = await launch(
                    owner_id=username,
                    device_id=body.target.device_id,
                    config=body.target.create_terminal.model_dump(),
                    agent_kind=body.agent_kind,
                )
                terminal_id = str(terminal["id"])
                launch_id = str(terminal.get("launch_id") or "")
                if not terminal_id or not launch_id:
                    raise ValidationError(
                        "managed terminal launcher did not return id and launch_id"
                    )
                created_terminal = (terminal_id, launch_id)
                device_id = body.target.device_id
            if terminal_id is None:  # protected by model validation
                raise ValidationError("terminal target is required")
            return await asyncio.to_thread(
                _service(request).assign_task,
                owner_id=username,
                task_id=task_id,
                terminal_id=terminal_id,
                device_id=device_id,
                agent_kind=body.agent_kind,
                expected_task_revision=body.expected_task_revision,
                parent_run_id=body.parent_run_id,
            )
        except BaseException as error:
            if created_terminal is not None:
                cleanup_error: BaseException | None = None
                try:
                    await _cleanup_created_terminal(
                        request,
                        owner_id=username,
                        terminal_id=created_terminal[0],
                        launch_id=created_terminal[1],
                    )
                except BaseException as cleanup_failure:
                    cleanup_error = cleanup_failure
                if cleanup_error is not None:
                    raise RuntimeError(
                        "assignment failed and created terminal cleanup also failed"
                    ) from cleanup_error
            if isinstance(error, HTTPException):
                raise
            if isinstance(error, (ExecutionError, ValueError)):
                raise _http_error(error) from error
            raise

    @router.get("/api/tasks/{task_id}")
    async def get_task(
        task_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                _service(request).get_task, owner_id=username, task_id=task_id
            )
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.get("/api/runs/{run_id}")
    async def get_run(
        run_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                _service(request).get_run, owner_id=username, run_id=run_id
            )
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.get("/api/runs/{run_id}/events")
    async def get_run_events(
        run_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                _service(request).run_event_page,
                owner_id=username,
                run_id=run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(
        run_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            command = await asyncio.to_thread(
                _service(request).request_cancel, owner_id=username, run_id=run_id
            )
            return {"command": command.as_dict()}
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.post("/api/runs/{run_id}/retry", status_code=201)
    async def retry_run(
        run_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                _service(request).retry_run, owner_id=username, run_id=run_id
            )
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.post("/api/runs/{run_id}/input", status_code=202)
    async def run_input(
        run_id: str,
        body: RunInputBody,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            command = await asyncio.to_thread(
                _service(request).provide_input,
                owner_id=username,
                run_id=run_id,
                value=body.value,
            )
            return {"command": command.as_dict()}
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.post("/api/runs/{run_id}/reporter-token", status_code=201)
    async def issue_reporter_token(
        run_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            token = await asyncio.to_thread(
                _service(request).issue_reporter_token,
                owner_id=username,
                run_id=run_id,
            )
            return {"token": token, "token_type": "Bearer"}
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.post("/api/runs/{run_id}/bridge-tokens", status_code=201)
    async def issue_bridge_tokens(
        run_id: str,
        request: Request,
        username: str = Depends(browser_user_dependency),
    ) -> dict[str, Any]:
        try:
            tokens = await asyncio.to_thread(
                _service(request).issue_bridge_tokens,
                owner_id=username,
                run_id=run_id,
            )
            return {**tokens, "token_type": "Bearer"}
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.get("/api/execution/snapshot")
    async def execution_snapshot(
        request: Request, username: str = Depends(browser_user_dependency)
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            _service(request).execution_view, owner_id=username
        )

    @router.get("/api/runtime/v1/context")
    async def runtime_context(request: Request, response: Response) -> dict[str, Any]:
        claims = _bearer(request, "context")
        try:
            context = await asyncio.to_thread(
                _service(request).runtime_context, claims=claims
            )
        except ExecutionError as error:
            raise _http_error(error) from error
        etag = f'"{context["context_revision"]}"'
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, no-cache"
        if request.headers.get("if-none-match") == etag:
            response.status_code = 304
            return {}
        return context

    @router.post("/api/runtime/v1/events:batch")
    async def runtime_events(request: Request) -> dict[str, Any]:
        claims = _bearer(request, "report")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BATCH_BYTES:
                raise HTTPException(
                    status_code=413, detail="runtime batch exceeds 256 KiB"
                )
        try:
            body = json.loads(bytes(raw) or b"{}")
            if not isinstance(body, Mapping):
                raise ValidationError("runtime batch body must be an object")
            values = body.get("events")
            if not isinstance(values, list) or not 1 <= len(values) <= MAX_BATCH_EVENTS:
                raise ValidationError("runtime batch must contain 1..100 events")
            if any(not isinstance(item, Mapping) for item in values):
                raise ValidationError("runtime batch events must be objects")
            events = [EventEnvelope.from_dict(item) for item in values]
        except (
            json.JSONDecodeError,
            AttributeError,
            TypeError,
            ExecutionError,
            ValueError,
        ) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        producer_keys = {(item.producer.id, item.producer.epoch) for item in events}
        if len(producer_keys) != 1:
            raise HTTPException(status_code=422, detail="one batch must use one producer epoch")
        try:
            for event in events:
                _service(request)._require_claim_scope(event, claims)
        except ValidationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        results: list[dict[str, Any]] = []
        for event in events:
            try:
                result = await asyncio.to_thread(
                    _service(request).ingest_runtime_event,
                    event,
                    claims=claims,
                )
                results.append(
                    {
                        "event_id": event.event_id,
                        "producer_seq": event.producer.seq,
                        "status": result.status.value,
                        "global_sequence": result.event.global_sequence,
                        "stream_version": result.event.stream_version,
                    }
                )
            except RevisionConflict as error:
                results.append(
                    {
                        "event_id": event.event_id,
                        "producer_seq": event.producer.seq,
                        "status": "rejected",
                        "code": "revision_conflict",
                        "current_revision": error.actual,
                    }
                )
            except (ExecutionError, RuntimeError, ValueError) as error:
                retryable = _service(request).store.event_effect_pending(
                    event_id=event.event_id
                )
                if not retryable:
                    try:
                        await asyncio.to_thread(
                            _service(request).record_runtime_rejection,
                            event,
                            claims=claims,
                            code=type(error).__name__,
                            message=str(error),
                        )
                    except (ExecutionError, ValueError):
                        # A conflicting idempotency tuple is already durable and
                        # therefore already consumes this producer sequence.
                        pass
                results.append(
                    {
                        "event_id": event.event_id,
                        "producer_seq": event.producer.seq,
                        "status": "rejected",
                        "code": type(error).__name__,
                        "message": str(error)[:1000],
                        "retryable": retryable,
                    }
                )
        producer_id, producer_epoch = next(iter(producer_keys))
        producer_id = _service(request).runtime_producer_id(
            claims=claims, reported_producer_id=producer_id
        )
        acknowledgement = await asyncio.to_thread(
            _service(request).store.producer_ack,
            producer_id=producer_id,
            producer_epoch=producer_epoch,
        )
        context = await asyncio.to_thread(
            _service(request).runtime_context, claims=claims
        )
        return {
            "results": results,
            "accepted_through_seq": acknowledgement.accepted_through_seq if acknowledgement else 0,
            "missing_ranges": [list(item) for item in acknowledgement.missing_ranges] if acknowledgement else [],
            "context_revision": context["context_revision"],
        }

    @router.post("/api/runtime/v1/heartbeat")
    async def runtime_heartbeat(
        body: HeartbeatBody, request: Request
    ) -> dict[str, Any]:
        claims = _bearer(request, "heartbeat")
        try:
            lease = await asyncio.to_thread(
                _service(request).heartbeat,
                claims=claims,
                holder_id=claims.token_id,
            )
            return {"lease": lease}
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.post("/api/runtime/v1/token:refresh")
    async def runtime_token_refresh(
        request: Request, response: Response
    ) -> dict[str, Any]:
        claims = _bearer(request, "context")
        try:
            value = await asyncio.to_thread(
                _service(request).refresh_runtime_token,
                claims=claims,
            )
        except (ExecutionError, RuntimeError, ValueError) as error:
            raise _http_error(error) from error
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return value

    @router.get("/api/runtime/v1/commands")
    async def runtime_commands(
        request: Request,
        after_sequence: int = Query(default=0, ge=0),
        wait_ms: int = Query(default=0, ge=0, le=30_000),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        claims = _bearer(request, "commands")
        if not claims.agent_instance_id:
            raise HTTPException(status_code=422, detail="command token has no agent scope")
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        commands = []
        scope = None
        while True:
            try:
                scope = await asyncio.to_thread(
                    _service(request).authorize_runtime_command_access,
                    claims=claims,
                )
            except ExecutionError as error:
                raise _http_error(error) from error
            commands = await asyncio.to_thread(
                _service(request).store.commands,
                owner_id=claims.owner_id,
                target_kind=EntityKind.AGENT_INSTANCE,
                target_id=claims.agent_instance_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            if commands or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
        try:
            scope = await asyncio.to_thread(
                _service(request).authorize_runtime_command_access,
                claims=claims,
            )
        except ExecutionError as error:
            raise _http_error(error) from error
        delivered = []
        for command in commands:
            try:
                await asyncio.to_thread(
                    _service(request).authorize_runtime_command_access,
                    claims=claims,
                    command=command,
                )
            except ExecutionError as error:
                raise _http_error(error) from error
            delivered.append(
                await asyncio.to_thread(
                    _service(request).store.mark_command_delivered,
                    owner_id=claims.owner_id,
                    command_id=command.id,
                )
            )
        return {"commands": [item.as_dict() for item in delivered]}

    @router.post("/api/runtime/v1/commands/{command_id}/ack")
    async def runtime_command_ack(
        command_id: str, request: Request
    ) -> dict[str, Any]:
        claims = _bearer(request, "ack")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_COMMAND_ACK_BYTES:
                raise HTTPException(
                    status_code=413, detail="runtime command ACK exceeds 64 KiB"
                )
        try:
            value = json.loads(
                bytes(raw) or b"{}",
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number is not allowed: {constant}")
                ),
            )
            if not isinstance(value, Mapping):
                raise ValueError("runtime command ACK body must be an object")
            body = CommandAckBody.model_validate(value)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            scope = await asyncio.to_thread(
                _service(request).authorize_runtime_command_access,
                claims=claims,
            )
            existing = await asyncio.to_thread(
                _service(request).store.command_queue.get,
                owner_id=claims.owner_id,
                command_id=command_id,
            )
            if existing is None:
                raise CommandConflict("command is outside reporter agent scope")
            await asyncio.to_thread(
                _service(request).authorize_runtime_command_access,
                claims=claims,
                command=existing,
            )
            command = await asyncio.to_thread(
                _service(request).store.ack_command,
                owner_id=claims.owner_id,
                command_id=command_id,
                status=CommandStatus(body.status),
                ack_id=body.ack_id,
                payload={**body.payload, "agent_instance_id": claims.agent_instance_id},
            )
            return {"command": command.as_dict()}
        except ExecutionError as error:
            raise _http_error(error) from error

    @router.websocket("/ws/execution")
    async def execution_socket(websocket: WebSocket) -> None:
        username = verify_browser_session(websocket.cookies.get(cookie_name))
        if not username:
            await websocket.accept()
            await websocket.close(code=4401)
            return
        try:
            after_sequence = max(
                0, int(websocket.query_params.get("after_sequence", "0"))
            )
        except ValueError:
            await websocket.accept()
            await websocket.close(code=4400)
            return
        service: ExecutionService = websocket.app.state.execution
        subscription = service.store.subscribe(
            owner_id=username,
            after_sequence=after_sequence,
            replay_limit=1000,
        )

        async def send_events() -> None:
            if subscription.snapshot.resync_required:
                await websocket.send_json(
                    {
                        "type": "resync_required",
                        "after_sequence": after_sequence,
                        "latest_sequence": subscription.snapshot.as_of_sequence,
                        "oldest_available_sequence": 0,
                    }
                )
                return
            for event in subscription.snapshot.events:
                await websocket.send_json(
                    {
                        "type": "event",
                        "cursor": event.global_sequence,
                        "event": event.as_dict(),
                        "projection": await projection_delta(
                            service, username, event
                        ),
                    }
                )
            async for item in subscription:
                if isinstance(item, ResyncRequired):
                    await websocket.send_json(
                        {
                            "type": "resync_required",
                            "after_sequence": item.after_sequence,
                            "latest_sequence": item.latest_sequence,
                            "oldest_available_sequence": 0,
                        }
                    )
                    return
                await websocket.send_json(
                    {
                        "type": "event",
                        "cursor": item.global_sequence,
                        "event": item.as_dict(),
                        "projection": await projection_delta(
                            service, username, item
                        ),
                    }
                )

        async def receive_until_disconnect() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(message.get("code", 1000))

        try:
            await websocket.accept()
            sender = asyncio.create_task(send_events())
            receiver = asyncio.create_task(receive_until_disconnect())
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        finally:
            await subscription.aclose()

    return router
