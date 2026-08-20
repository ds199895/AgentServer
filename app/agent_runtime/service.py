from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping

from .bridge import InMemoryProviderBridge, ProviderBridge, ProviderBridgeRegistry
from .events import AgentEvent, event, new_id
from .models import AgentActivity, AgentMessage, AgentRequest, AgentSession, AgentTurn
from .store import AgentEventStore


class AgentSessionService:
    def __init__(self, store: AgentEventStore, registry: ProviderBridgeRegistry | None = None) -> None:
        self.store = store
        self.registry = registry or ProviderBridgeRegistry()
        self.registry.register("generic", InMemoryProviderBridge, replace=True)
        # Provider names are registrations, not branches in the session/UI
        # contract. Deployments can replace any factory with a native bridge.
        for provider in ("claude", "kimi"):
            self.registry.register(provider, InMemoryProviderBridge, replace=True)
        self._sessions: dict[str, AgentSession] = {}
        self._bridges: dict[str, ProviderBridge] = {}
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = {}
        self._consumers: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    async def create(self, *, owner_id: str, provider: str, cwd: str, device_id: str | None = None, permission_mode: str = "workspace-write", model: str | None = None, session_id: str | None = None) -> AgentSession:
        now = time.time()
        sid = session_id or new_id()
        session = AgentSession(sid, owner_id, device_id, provider, cwd, permission_mode, model, created_at=now, updated_at=now)
        bridge = self.registry.create(provider)
        async with self._lock:
            self._sessions[sid] = session
            self._bridges[sid] = bridge
            self._subscribers[sid] = set()
            self._persist(session)
        await self._dispatch(session, event(sid, "session.created", {"provider": provider, "device_id": device_id, "cwd": cwd}))
        try:
            await bridge.start(sid, cwd=cwd, options={"permission_mode": permission_mode, "model": model})
        except BaseException as error:
            await self._dispatch(session, event(sid, "session.failed", {"error": str(error)[:1000]}))
            raise
        task = asyncio.create_task(self._consume(sid, bridge))
        self._consumers.add(task)
        task.add_done_callback(self._consumers.discard)
        return session

    def _persist(self, session: AgentSession) -> None:
        self.store.save_session(session.owner_id, session.id, session.as_dict())

    async def _dispatch(self, session: AgentSession, value: AgentEvent) -> AgentEvent:
        committed = self.store.append(value)
        session.sequence = committed.sequence
        session.updated_at = committed.occurred_at
        self._apply(session, committed)
        self._persist(session)
        for queue in tuple(self._subscribers.get(session.id, ())):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(committed)
        return committed

    async def _consume(self, session_id: str, bridge: ProviderBridge) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        async for value in bridge.events(session_id):
            await self._dispatch(session, value)

    def _apply(self, session: AgentSession, value: AgentEvent) -> None:
        p = value.payload
        typ = value.type
        if typ == "session.created": session.state = "starting"
        elif typ == "session.ready":
            session.state = "ready"
            provider_session_id = p.get("provider_session_id") or p.get("provider_thread_id")
            if provider_session_id:
                session.resume_cursor = {"thread_id": str(provider_session_id)}
        elif typ == "session.stopped": session.state = "stopped"
        elif typ == "session.failed": session.state, session.last_error = "failed", str(p.get("error") or "provider failed")
        elif typ == "turn.queued":
            turn_id = str(p.get("turn_id") or new_id())
            if not any(turn.id == turn_id for turn in session.turns):
                session.turns.append(AgentTurn(turn_id, session.id, str(p.get("input") or ""), "queued", value.occurred_at))
        elif typ == "turn.started":
            session.state = "running"; session.active_turn_id = str(p.get("turn_id") or "")
            turn = next((item for item in reversed(session.turns) if item.id == session.active_turn_id), None)
            if turn:
                turn.state = "running"
            else:
                session.turns.append(AgentTurn(session.active_turn_id, session.id, str(p.get("input") or ""), "running", value.occurred_at))
        elif typ == "turn.completed":
            session.state = "ready"; session.active_turn_id = None
            turn = next((t for t in reversed(session.turns) if t.id == p.get("turn_id")), None)
            if turn: turn.state, turn.completed_at = "completed", value.occurred_at
        elif typ == "turn.interrupted":
            session.state = "ready"; session.active_turn_id = None
            turn = next((t for t in reversed(session.turns) if t.id == p.get("turn_id")), None)
            if turn: turn.state, turn.completed_at = "interrupted", value.occurred_at
        elif typ in {"message.created", "message.delta"}:
            mid = str(p.get("message_id") or new_id()); text = str(p.get("text") or "")
            current = next((m for m in session.messages if m.id == mid), None)
            if current: current.text += text; current.streaming = typ == "message.delta"
            else: session.messages.append(AgentMessage(mid, session.id, str(p.get("role") or "assistant"), text, p.get("turn_id"), p.get("item_id"), value.occurred_at, typ == "message.delta"))
        elif typ == "activity.started":
            session.activities.append(AgentActivity(str(p.get("activity_id") or new_id()), session.id, str(p.get("kind") or "status"), str(p.get("title") or "Working"), turn_id=p.get("turn_id"), item_id=p.get("item_id"), created_at=value.occurred_at, updated_at=value.occurred_at))
        elif typ in {"activity.updated", "activity.completed"}:
            aid = str(p.get("activity_id") or "")
            current = next((a for a in reversed(session.activities) if a.id == aid), None)
            if current:
                current.status = str(p.get("status") or ("completed" if typ.endswith("completed") else current.status)); current.detail = str(p.get("detail") or current.detail); current.output = p.get("output", current.output); current.updated_at = value.occurred_at
        elif typ == "request.created":
            session.state = "waiting"; session.requests.append(AgentRequest(str(p.get("request_id") or new_id()), session.id, str(p.get("kind") or "user_input"), str(p.get("title") or "Input required"), str(p.get("detail") or ""), list(p.get("options") or []), turn_id=p.get("turn_id"), created_at=value.occurred_at))
        elif typ == "request.resolved":
            req = next((r for r in reversed(session.requests) if r.id == p.get("request_id")), None)
            if req: req.status = "resolved"
            session.state = "running" if session.active_turn_id else "ready"

    async def get(self, owner_id: str, session_id: str) -> AgentSession | None:
        session = self._sessions.get(session_id)
        if session and session.owner_id == owner_id: return session
        raw = self.store.load_session(owner_id, session_id)
        if raw:
            session = AgentSession.from_dict(raw)
            self._sessions[session_id] = session
            self._subscribers.setdefault(session_id, set())
            bridge = self._bridges.setdefault(session_id, self.registry.create(session.provider))
            if session.state not in {"stopped", "failed"}:
                try:
                    await bridge.start(session.id, cwd=session.cwd, options={"permission_mode": session.permission_mode, "model": session.model, "resume_cursor": session.resume_cursor})
                    task = asyncio.create_task(self._consume(session.id, bridge))
                    self._consumers.add(task)
                    task.add_done_callback(self._consumers.discard)
                except BaseException as error:
                    await self._dispatch(session, event(session.id, "session.failed", {"error": str(error)[:1000]}))
            return session
        return None

    async def list(self, owner_id: str) -> list[AgentSession]:
        values = {s.id: s for s in self._sessions.values() if s.owner_id == owner_id}
        for raw in self.store.list_sessions(owner_id):
            if raw.get("id") not in values:
                values[str(raw["id"])] = AgentSession.from_dict(raw)
        return sorted(values.values(), key=lambda value: value.updated_at, reverse=True)

    async def send_turn(self, owner_id: str, session_id: str, text: str) -> AgentTurn:
        session = await self.get(owner_id, session_id)
        if session is None: raise KeyError(session_id)
        turn = AgentTurn(new_id(), session_id, text, created_at=time.time())
        await self._dispatch(session, event(session_id, "turn.queued", {"turn_id": turn.id, "input": text}))
        await self._bridges[session_id].turn(session_id, turn.id, text)
        return turn

    async def command(self, owner_id: str, session_id: str, action: str, payload: Mapping[str, object] | None = None) -> None:
        session = await self.get(owner_id, session_id)
        if session is None: raise KeyError(session_id)
        bridge = self._bridges[session_id]
        if action == "interrupt": await bridge.interrupt(session_id, session.active_turn_id)
        elif action == "respond": await bridge.respond(session_id, str((payload or {}).get("request_id") or ""), payload or {})
        elif action == "close": await bridge.stop(session_id)
        else: raise ValueError("unsupported action")

    async def events(self, owner_id: str, session_id: str, after: int = 0) -> AsyncIterator[AgentEvent]:
        session = await self.get(owner_id, session_id)
        if session is None: raise KeyError(session_id)
        for value in self.store.events(owner_id, session_id, after): yield value
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(session_id, set()).add(queue)
        try:
            while True: yield await queue.get()
        finally: self._subscribers.get(session_id, set()).discard(queue)

    async def close(self) -> None:
        for task in tuple(self._consumers):
            task.cancel()
        if self._consumers:
            await asyncio.gather(*self._consumers, return_exceptions=True)
        for bridge in set(self._bridges.values()): await bridge.close()


import contextlib
