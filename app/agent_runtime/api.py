from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from app.execution.device_runtime import (
    DeviceRuntimeConflict,
    DeviceRuntimeError,
    DeviceRuntimeFenceError,
    DeviceRuntimeNotFound,
)

from .connectors import AgentConnectorError
from .service import AgentSessionService


class CreateAgentSessionBody(BaseModel):
    provider: str = Field(default="codex", min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=255)
    cwd: str = Field(default=".", min_length=1, max_length=4096)
    permission_mode: str = Field(default="workspace-write", min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=255)
    session_id: str | None = Field(default=None, max_length=255)


class TurnBody(BaseModel):
    input: str = Field(min_length=1, max_length=49_152)
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)


class RespondBody(BaseModel):
    request_id: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)


def build_agent_router(browser_user_dependency: Callable[..., str]) -> APIRouter:
    router = APIRouter()

    def service(request: Request) -> AgentSessionService:
        value = getattr(request.app.state, "agent_sessions", None)
        if not isinstance(value, AgentSessionService):
            raise HTTPException(503, "agent runtime is unavailable")
        return value

    def http_error(error: BaseException) -> HTTPException:
        if isinstance(error, DeviceRuntimeNotFound):
            return HTTPException(404, str(error))
        if isinstance(error, (DeviceRuntimeFenceError, AgentConnectorError)):
            return HTTPException(409, str(error))
        if isinstance(error, DeviceRuntimeConflict):
            return HTTPException(409, str(error))
        return HTTPException(422, str(error))

    @router.post("/api/agent/sessions", status_code=201)
    async def create(body: CreateAgentSessionBody, request: Request, owner_id: str = Depends(browser_user_dependency)):
        try:
            value = await service(request).create(owner_id=owner_id, provider=body.provider, device_id=body.device_id, cwd=body.cwd, permission_mode=body.permission_mode, model=body.model, session_id=body.session_id)
            return {"session": value.as_dict()}
        except (KeyError, ValueError, DeviceRuntimeError, AgentConnectorError) as error:
            raise http_error(error) from error

    @router.get("/api/agent/devices/{device_id}/status")
    async def device_status(
        device_id: str,
        request: Request,
        owner_id: str = Depends(browser_user_dependency),
    ):
        try:
            return await service(request).device_status(owner_id, device_id)
        except (ValueError, DeviceRuntimeError, AgentConnectorError) as error:
            raise http_error(error) from error

    @router.get("/api/agent/sessions")
    async def list_sessions(request: Request, owner_id: str = Depends(browser_user_dependency)):
        return {"sessions": [value.as_dict(include_history=False) for value in await service(request).list(owner_id)]}

    @router.get("/api/agent/sessions/{session_id}")
    async def get(session_id: str, request: Request, owner_id: str = Depends(browser_user_dependency)):
        value = await service(request).get(owner_id, session_id)
        if value is None: raise HTTPException(404, "agent session not found")
        return {"session": value.as_dict()}

    @router.get("/api/agent/sessions/{session_id}/events")
    async def events(session_id: str, request: Request, after_sequence: int = Query(default=0, ge=0), owner_id: str = Depends(browser_user_dependency)):
        value = await service(request).get(owner_id, session_id)
        if value is None: raise HTTPException(404, "agent session not found")
        return {"events": [item.as_dict() for item in service(request).store.events(owner_id, session_id, after_sequence)]}

    @router.post("/api/agent/sessions/{session_id}/turns", status_code=202)
    async def turn(session_id: str, body: TurnBody, request: Request, owner_id: str = Depends(browser_user_dependency)):
        try: return {"turn": (await service(request).send_turn(owner_id, session_id, body.input, body.turn_id)).as_dict()}
        except KeyError as error: raise HTTPException(404, "agent session not found") from error
        except (ValueError, DeviceRuntimeError, AgentConnectorError) as error: raise http_error(error) from error

    @router.post("/api/agent/sessions/{session_id}/interrupt", status_code=202)
    async def interrupt(session_id: str, request: Request, owner_id: str = Depends(browser_user_dependency)):
        try: await service(request).command(owner_id, session_id, "interrupt")
        except KeyError as error: raise HTTPException(404, "agent session not found") from error
        except (ValueError, DeviceRuntimeError, AgentConnectorError) as error: raise http_error(error) from error
        return {"accepted": True}

    @router.post("/api/agent/sessions/{session_id}/requests/respond", status_code=202)
    async def respond(session_id: str, body: RespondBody, request: Request, owner_id: str = Depends(browser_user_dependency)):
        try: await service(request).command(owner_id, session_id, "respond", {"request_id": body.request_id, **body.payload})
        except KeyError as error: raise HTTPException(404, "agent session not found") from error
        except (ValueError, DeviceRuntimeError, AgentConnectorError) as error: raise http_error(error) from error
        return {"accepted": True}

    @router.delete("/api/agent/sessions/{session_id}", status_code=202)
    async def close(session_id: str, request: Request, owner_id: str = Depends(browser_user_dependency)):
        try: await service(request).command(owner_id, session_id, "close")
        except KeyError as error: raise HTTPException(404, "agent session not found") from error
        except (ValueError, DeviceRuntimeError, AgentConnectorError) as error: raise http_error(error) from error
        return {"accepted": True}

    @router.websocket("/ws/agent/sessions/{session_id}")
    async def stream(websocket: WebSocket, session_id: str, after_sequence: int = Query(default=0, ge=0)):
        username = websocket.app.state.signer.verify(websocket.cookies.get(websocket.app.state.cookie_name))
        if not username:
            await websocket.close(code=4401); return
        sessions: AgentSessionService = websocket.app.state.agent_sessions
        if await sessions.get(username, session_id) is None:
            await websocket.close(code=4404); return
        await websocket.accept()
        try:
            async for value in sessions.events(username, session_id, after_sequence):
                await websocket.send_json(value.as_dict())
        except (WebSocketDisconnect, asyncio.CancelledError):
            return

    return router
