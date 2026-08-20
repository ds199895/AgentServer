from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from app.execution.runtime_adapters.base import (
    ApprovalDecision,
    RuntimeSessionSpec,
    RuntimeTurnInput,
)
from app.execution.runtime_adapters.codex import CodexRuntimeAdapter

from ..bridge import ProviderBridge
from ..events import AgentEvent, event, new_id


class CodexProviderBridge(ProviderBridge):
    """Translate the provider app-server protocol into Agent Runtime events.

    This is the only layer allowed to know provider method names and payloads.
    The durable service, HTTP contract, WebSocket and UI remain provider-neutral.
    """

    provider = "codex"

    def __init__(self) -> None:
        self.adapter = CodexRuntimeAdapter(
            isolation_enabled=os.getenv("AGENT_PROVIDER_ISOLATION", "1") == "1",
            host_state_dir=Path(os.getenv("DATA_DIR", "data")) / "agent-runtime",
        )
        self._queues: dict[str, asyncio.Queue[AgentEvent]] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._turns: dict[tuple[str, str], str] = {}

    def _queue(self, session_id: str) -> asyncio.Queue[AgentEvent]:
        return self._queues.setdefault(session_id, asyncio.Queue(maxsize=1024))

    async def start(self, session_id: str, *, cwd: str, options: Mapping[str, Any]) -> None:
        try:
            await self.adapter.start_session(RuntimeSessionSpec(
                session_id=session_id,
                cwd=cwd,
                permission_mode=str(options.get("permission_mode") or "workspace-write"),
                model=str(options["model"]) if options.get("model") else None,
                resume_cursor=options.get("resume_cursor") if isinstance(options.get("resume_cursor"), Mapping) else None,
            ))
        except BaseException:
            raise
        self._pumps[session_id] = asyncio.create_task(self._pump(session_id))

    async def turn(self, session_id: str, turn_id: str, text: str) -> None:
        value = await self.adapter.send_turn(session_id, RuntimeTurnInput(text=text))
        self._turns[(session_id, value.turn_id)] = turn_id

    async def interrupt(self, session_id: str, turn_id: str | None = None) -> None:
        provider_turn = next((provider for (sid, provider), local in self._turns.items() if sid == session_id and local == turn_id), None)
        await self.adapter.interrupt_turn(session_id, provider_turn)

    async def respond(self, session_id: str, request_id: str, payload: Mapping[str, Any]) -> None:
        if "answers" in payload:
            answers = payload.get("answers")
            await self.adapter.respond_to_user_input(session_id, request_id, answers if isinstance(answers, Mapping) else {})
            return
        decision = str(payload.get("decision") or "deny").replace("approve", "approve_once")
        if decision not in {item.value for item in ApprovalDecision}:
            decision = "deny"
        await self.adapter.respond_to_approval(session_id, request_id, decision)

    async def stop(self, session_id: str) -> None:
        await self.adapter.stop_session(session_id)

    async def events(self, session_id: str) -> AsyncIterator[AgentEvent]:
        queue = self._queue(session_id)
        while True:
            yield await queue.get()

    def _local_turn(self, session_id: str, provider_turn: object) -> str | None:
        value = str(provider_turn or "")
        return self._turns.get((session_id, value), value or None)

    async def _pump(self, session_id: str) -> None:
        async for value in self.adapter.events(session_id):
            payload = dict(value.payload)
            local_turn = self._local_turn(session_id, value.turn_id)
            typ = value.type
            normalized: AgentEvent | None = None
            if typ in {"session.started", "session.state.changed"}:
                state = str(payload.get("state") or "")
                target = "session.ready" if state == "ready" else "session.stopped" if state == "stopped" else "session.failed" if state == "error" else None
                if target: normalized = event(session_id, target, payload)
            elif typ == "thread.started":
                normalized = event(session_id, "session.ready", {"provider_thread_id": payload.get("provider_thread_id")})
            elif typ == "turn.started":
                normalized = event(session_id, "turn.started", {**payload, "turn_id": local_turn})
            elif typ in {"turn.completed", "turn.failed"}:
                target = "turn.failed" if typ.endswith("failed") else "turn.interrupted" if payload.get("state") == "interrupted" else "turn.completed"
                normalized = event(session_id, target, {**payload, "turn_id": local_turn})
            elif typ in {"message.delta", "message.completed"}:
                normalized = event(session_id, "message.delta" if typ.endswith("delta") else "message.created", {"message_id": value.item_id or new_id(), "role": "assistant", "text": str(payload.get("text") or ""), "turn_id": local_turn, "item_id": value.item_id})
            elif typ == "item.started":
                normalized = event(session_id, "activity.started", {"activity_id": value.item_id or new_id(), "kind": "tool", "title": str(payload.get("item_type") or "Tool call").replace("_", " ").title(), "turn_id": local_turn, "item_id": value.item_id})
            elif typ == "item.completed":
                normalized = event(session_id, "activity.completed", {"activity_id": value.item_id, "status": str(payload.get("status") or "completed"), "turn_id": local_turn, "item_id": value.item_id})
            elif typ == "interaction.requested":
                normalized = event(session_id, "request.created", {"request_id": value.interaction_id, "kind": "user_input" if "input" in str(payload.get("kind") or "") else "approval", "title": str(payload.get("title") or "Action requires confirmation"), "detail": str(payload.get("detail") or ""), "options": payload.get("questions") or [], "turn_id": local_turn})
            elif typ == "interaction.resolved":
                normalized = event(session_id, "request.resolved", {"request_id": value.interaction_id})
            if normalized is not None:
                await self._queue(session_id).put(normalized)

    async def close(self) -> None:
        await self.adapter.close()
