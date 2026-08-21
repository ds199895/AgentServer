from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Mapping

from .connectors import (
    AgentLaunchSpec,
    DeviceConnector,
    ProviderRegistryConnector,
)
from .events import AgentEvent, event, new_id
from .models import AgentActivity, AgentMessage, AgentRequest, AgentSession, AgentTurn
from .store import AgentEventStore

# Replay page size. `AgentEventStore.events` caps a single read at 5000 rows, so
# rehydration pages through the log rather than assuming one read covers it.
_REPLAY_PAGE = 1000


class AgentSessionService:
    def __init__(
        self,
        store: AgentEventStore,
        connector: DeviceConnector | None = None,
    ) -> None:
        self.store = store
        self.connector = connector or ProviderRegistryConnector()
        self.registry = getattr(self.connector, "registry", None)
        self._sessions: dict[str, AgentSession] = {}
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = {}
        self._consumers: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def _spec(session: AgentSession) -> AgentLaunchSpec:
        stable_turn_id = session.active_turn_id
        if not stable_turn_id:
            pending = next(
                (
                    turn
                    for turn in reversed(session.turns)
                    if turn.state in {"queued", "running"}
                ),
                None,
            )
            stable_turn_id = pending.id if pending else None
        return AgentLaunchSpec(
            owner_id=session.owner_id,
            device_id=session.device_id or "local-test",
            session_id=session.id,
            provider=session.provider,
            cwd=session.cwd,
            permission_mode=session.permission_mode,
            model=session.model,
            resume_cursor=session.resume_cursor,
            stable_turn_id=stable_turn_id,
        )

    async def create(self, *, owner_id: str, provider: str, cwd: str, device_id: str | None = None, permission_mode: str = "workspace-write", model: str | None = None, session_id: str | None = None) -> AgentSession:
        now = time.time()
        sid = session_id or new_id()
        session = AgentSession(sid, owner_id, device_id, provider, cwd, permission_mode, model, created_at=now, updated_at=now)
        restored = False
        async with self._lock:
            current = self._sessions.get(sid)
            if current is None:
                raw = self.store.load_session(owner_id, sid)
                if raw is not None:
                    current = self._rehydrate(AgentSession.from_dict(raw))
                    self._sessions[sid] = current
                    self._subscribers.setdefault(sid, set())
                    restored = True
            if current is not None:
                if (
                    current.owner_id == owner_id
                    and current.device_id == device_id
                    and current.provider == provider
                    and current.cwd == cwd
                    and current.permission_mode == permission_mode
                    and current.model == model
                ):
                    session = current
                    if not restored:
                        return current
                else:
                    raise ValueError("agent session id is bound to different contents")
            else:
                self._persist(session)
                self._sessions[sid] = session
                self._subscribers[sid] = set()
        if restored:
            if session.state in {"stopped", "failed"}:
                return session
            binding = await self.connector.start(self._spec(session))
            session.executor_id = binding.executor_id
            session.bridge_instance_id = binding.bridge_instance_id
            session.transport = binding.transport
            session.device_generation = binding.device_generation
            session.platform = dict(binding.platform)
            session.capabilities = dict(binding.capabilities)
            self._persist(session)
            task = asyncio.create_task(self._consume(session))
            self._consumers.add(task)
            task.add_done_callback(self._consumers.discard)
            return session
        await self._dispatch(session, event(sid, "session.created", {"provider": provider, "device_id": device_id, "cwd": cwd}))
        try:
            binding = await self.connector.start(self._spec(session))
            session.executor_id = binding.executor_id
            session.bridge_instance_id = binding.bridge_instance_id
            session.transport = binding.transport
            session.device_generation = binding.device_generation
            session.platform = dict(binding.platform)
            session.capabilities = dict(binding.capabilities)
            self._persist(session)
        except BaseException as error:
            await self._dispatch(session, event(sid, "session.failed", {"error": str(error)[:1000]}))
            raise
        task = asyncio.create_task(self._consume(session))
        self._consumers.add(task)
        task.add_done_callback(self._consumers.discard)
        return session

    def _persist(self, session: AgentSession) -> None:
        # Metadata only. Messages, activities, requests and turns are derived by
        # replaying `agent_events`, so persisting them here would rewrite the
        # whole transcript on every delta and make one turn cost O(events^2).
        self.store.save_session(
            session.owner_id, session.id, session.as_dict(include_history=False)
        )

    def _rehydrate(self, session: AgentSession) -> AgentSession:
        """Rebuild a session's history from its durable event log."""
        session.messages.clear()
        session.activities.clear()
        session.requests.clear()
        session.turns.clear()
        cursor = 0
        while True:
            values = self.store.events(
                session.owner_id, session.id, cursor, limit=_REPLAY_PAGE
            )
            if not values:
                break
            for value in values:
                self._apply(session, value)
                cursor = value.sequence
            session.sequence = max(session.sequence, cursor)
        return session

    async def _dispatch(self, session: AgentSession, value: AgentEvent) -> AgentEvent:
        committed, created = self.store.append_once(value)
        if not created:
            return committed
        session.sequence = committed.sequence
        session.updated_at = committed.occurred_at
        self._apply(session, committed)
        self._persist(session)
        for queue in tuple(self._subscribers.get(session.id, ())):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(committed)
        return committed

    async def _consume(self, session: AgentSession) -> None:
        try:
            async for value in self.connector.events(
                self._spec(session), session.connector_sequence
            ):
                source_sequence = value.payload.get("source_sequence")
                await self._dispatch(session, value)
                if isinstance(source_sequence, int) and not isinstance(
                    source_sequence, bool
                ):
                    session.connector_sequence = max(
                        session.connector_sequence, source_sequence
                    )
                    self._persist(session)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if session.state not in {"stopped", "failed"}:
                with contextlib.suppress(BaseException):
                    await self._dispatch(
                        session,
                        event(
                            session.id,
                            "session.failed",
                            {"error": f"device connector failed: {error}"[:1000]},
                        ),
                    )

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
        elif typ == "session.state.changed":
            state = str(p.get("state") or "")
            if state in {"starting", "ready", "running", "waiting", "disconnected", "stopping", "stopped", "failed"}:
                session.state = state
        elif typ == "turn.queued":
            turn_id = str(p.get("turn_id") or new_id())
            if not any(turn.id == turn_id for turn in session.turns):
                session.turns.append(AgentTurn(turn_id, session.id, str(p.get("input") or ""), "queued", value.occurred_at))
                session.messages.append(AgentMessage(f"user-{turn_id}", session.id, "user", str(p.get("input") or ""), turn_id, None, value.occurred_at, False, value.sequence))
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
        elif typ == "turn.failed":
            session.state = "ready"; session.active_turn_id = None
            turn = next((t for t in reversed(session.turns) if t.id == p.get("turn_id")), None)
            if turn:
                turn.state, turn.completed_at = "failed", value.occurred_at
                turn.error = str(p.get("error") or "provider turn failed")
        elif typ in {"message.created", "message.delta"}:
            mid = str(p.get("message_id") or new_id()); text = str(p.get("text") or "")
            current = next((m for m in session.messages if m.id == mid), None)
            if current:
                if typ == "message.delta":
                    current.text += text
                    current.streaming = True
                else:
                    if text:
                        current.text = text
                    current.streaming = False
            else: session.messages.append(AgentMessage(mid, session.id, str(p.get("role") or "assistant"), text, p.get("turn_id"), p.get("item_id"), value.occurred_at, typ == "message.delta", value.sequence))
        elif typ == "activity.started":
            aid = str(p.get("activity_id") or new_id())
            current = next((a for a in reversed(session.activities) if a.id == aid), None)
            if current is None:
                session.activities.append(AgentActivity(aid, session.id, str(p.get("kind") or "status"), str(p.get("title") or "Working"), status=str(p.get("status") or "running"), detail=str(p.get("detail") or ""), input=p.get("input"), output=p.get("output"), turn_id=p.get("turn_id"), item_id=p.get("item_id"), created_at=value.occurred_at, updated_at=value.occurred_at, collapsed=bool(p.get("collapsed", True)), sequence=value.sequence))
        elif typ in {"activity.updated", "activity.completed"}:
            aid = str(p.get("activity_id") or "")
            current = next((a for a in reversed(session.activities) if a.id == aid), None)
            if current:
                current.status = str(p.get("status") or ("completed" if typ.endswith("completed") else current.status))
                if p.get("title"):
                    current.title = str(p["title"])
                if p.get("detail"):
                    current.detail = str(p["detail"])
                if p.get("detail_delta"):
                    current.detail += str(p["detail_delta"])
                if "input" in p and p.get("input") is not None:
                    current.input = p["input"]
                if "output" in p and p.get("output") is not None:
                    current.output = p["output"]
                if p.get("output_delta"):
                    fragment = str(p["output_delta"])
                    current.output = f"{current.output or ''}{fragment}"
                current.updated_at = value.occurred_at
            elif aid:
                session.activities.append(AgentActivity(aid, session.id, str(p.get("kind") or "output"), str(p.get("title") or "Tool output"), status=str(p.get("status") or ("completed" if typ.endswith("completed") else "running")), detail=str(p.get("detail") or p.get("detail_delta") or ""), input=p.get("input"), output=p.get("output") if p.get("output") is not None else p.get("output_delta"), turn_id=p.get("turn_id"), item_id=p.get("item_id"), created_at=value.occurred_at, updated_at=value.occurred_at, sequence=value.sequence))
            if typ == "activity.completed" and p.get("item_id"):
                reasoning = next(
                    (
                        message
                        for message in reversed(session.messages)
                        if message.role == "reasoning"
                        and message.item_id == p.get("item_id")
                    ),
                    None,
                )
                if reasoning:
                    reasoning.streaming = False
        elif typ == "request.created":
            session.state = "waiting"; session.requests.append(AgentRequest(str(p.get("request_id") or new_id()), session.id, str(p.get("kind") or "user_input"), str(p.get("title") or "Input required"), str(p.get("detail") or ""), list(p.get("options") or []), turn_id=p.get("turn_id"), created_at=value.occurred_at, input=p.get("input"), sequence=value.sequence))
        elif typ == "request.resolved":
            req = next((r for r in reversed(session.requests) if r.id == p.get("request_id")), None)
            if req:
                req.status = "resolved"
                req.response = p.get("resolution", p.get("response"))
                req.resolved_at = value.occurred_at
            session.state = "running" if session.active_turn_id else "ready"
        elif typ == "plan.updated":
            session.activities.append(AgentActivity(str(p.get("activity_id") or value.id), session.id, "plan", "Plan updated", status="completed", detail=str(p.get("detail") or ""), input=p.get("plan"), turn_id=p.get("turn_id"), created_at=value.occurred_at, updated_at=value.occurred_at, sequence=value.sequence))

    async def get(self, owner_id: str, session_id: str) -> AgentSession | None:
        session = self._sessions.get(session_id)
        if session and session.owner_id == owner_id: return session
        raw = self.store.load_session(owner_id, session_id)
        if raw:
            session = self._rehydrate(AgentSession.from_dict(raw))
            self._sessions[session_id] = session
            self._subscribers.setdefault(session_id, set())
            if session.state not in {"stopped", "failed"}:
                try:
                    binding = await self.connector.start(self._spec(session))
                    session.executor_id = binding.executor_id
                    session.bridge_instance_id = binding.bridge_instance_id
                    session.transport = binding.transport
                    session.device_generation = binding.device_generation
                    session.platform = dict(binding.platform)
                    session.capabilities = dict(binding.capabilities)
                    task = asyncio.create_task(self._consume(session))
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

    async def send_turn(
        self,
        owner_id: str,
        session_id: str,
        text: str,
        turn_id: str | None = None,
    ) -> AgentTurn:
        session = await self.get(owner_id, session_id)
        if session is None: raise KeyError(session_id)
        resolved_turn_id = turn_id or new_id()
        existing = next((item for item in session.turns if item.id == resolved_turn_id), None)
        if existing is not None:
            if existing.input != text:
                raise ValueError("turn id is bound to different input")
            return existing
        turn = AgentTurn(resolved_turn_id, session_id, text, created_at=time.time())
        await self._dispatch(
            session,
            AgentEvent(
                0,
                f"agent-turn-queued:{session_id}:{turn.id}",
                session_id,
                "turn.queued",
                {"turn_id": turn.id, "input": text},
                turn.created_at,
            ),
        )
        try:
            await self.connector.turn(self._spec(session), turn.id, text)
        except BaseException as error:
            await self._dispatch(session, event(session_id, "turn.failed", {"turn_id": turn.id, "error": str(error)[:1000]}))
            raise
        return turn

    async def command(self, owner_id: str, session_id: str, action: str, payload: Mapping[str, object] | None = None) -> None:
        session = await self.get(owner_id, session_id)
        if session is None: raise KeyError(session_id)
        if action == "interrupt": await self.connector.interrupt(self._spec(session), session.active_turn_id)
        elif action == "respond": await self.connector.respond(self._spec(session), str((payload or {}).get("request_id") or ""), payload or {})
        elif action == "close":
            session.state = "stopping"
            self._persist(session)
            await self.connector.stop(self._spec(session))
        else: raise ValueError("unsupported action")

    async def device_status(self, owner_id: str, device_id: str) -> Mapping[str, Any]:
        return await self.connector.status(owner_id, device_id)

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
        await self.connector.close()
        self.store.close()
