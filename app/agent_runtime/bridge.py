from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from .events import AgentEvent, event, new_id


class ProviderBridge(ABC):
    """Provider adapter boundary. Provider details must stay behind this API."""

    provider: str = "generic"

    @abstractmethod
    async def start(self, session_id: str, *, cwd: str, options: Mapping[str, Any]) -> None: ...

    @abstractmethod
    async def turn(self, session_id: str, turn_id: str, text: str) -> None: ...

    @abstractmethod
    async def interrupt(self, session_id: str, turn_id: str | None = None) -> None: ...

    @abstractmethod
    async def respond(self, session_id: str, request_id: str, payload: Mapping[str, Any]) -> None: ...

    @abstractmethod
    async def stop(self, session_id: str) -> None: ...

    @abstractmethod
    def events(self, session_id: str) -> AsyncIterator[AgentEvent]: ...

    async def close(self) -> None:  # pragma: no cover - optional provider hook
        return None


@dataclass
class _Queue:
    value: asyncio.Queue[AgentEvent]


class InMemoryProviderBridge(ProviderBridge):
    """Deterministic bridge used by local development and contract tests.

    Real providers implement the same methods and emit the same normalized
    events; UI and persistence never depend on provider wire messages.
    """

    provider = "generic"

    def __init__(self) -> None:
        self._queues: dict[str, _Queue] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def _queue(self, session_id: str) -> asyncio.Queue[AgentEvent]:
        return self._queues.setdefault(session_id, _Queue(asyncio.Queue())).value

    async def _emit(self, value: AgentEvent) -> None:
        await self._queue(value.session_id).put(value)

    async def start(self, session_id: str, *, cwd: str, options: Mapping[str, Any]) -> None:
        await self._emit(event(session_id, "session.ready", {"cwd": cwd, "capabilities": ["turn", "interrupt", "approval", "user_input"]}))

    async def turn(self, session_id: str, turn_id: str, text: str) -> None:
        async def run() -> None:
            await self._emit(event(session_id, "turn.started", {"turn_id": turn_id, "input": text}))
            activity_id = new_id()
            await self._emit(event(session_id, "activity.started", {"activity_id": activity_id, "kind": "command", "title": "Working", "turn_id": turn_id}))
            await asyncio.sleep(0)
            await self._emit(event(session_id, "message.delta", {"message_id": new_id(), "role": "assistant", "text": f"{text}" , "turn_id": turn_id}))
            await self._emit(event(session_id, "activity.completed", {"activity_id": activity_id, "status": "completed", "turn_id": turn_id}))
            await self._emit(event(session_id, "turn.completed", {"turn_id": turn_id}))
        task = asyncio.create_task(run())
        self._tasks[turn_id] = task

    async def interrupt(self, session_id: str, turn_id: str | None = None) -> None:
        if turn_id and (task := self._tasks.get(turn_id)):
            task.cancel()
        await self._emit(event(session_id, "turn.interrupted", {"turn_id": turn_id}))

    async def respond(self, session_id: str, request_id: str, payload: Mapping[str, Any]) -> None:
        await self._emit(event(session_id, "request.resolved", {"request_id": request_id, "payload": dict(payload)}))

    async def stop(self, session_id: str) -> None:
        await self._emit(event(session_id, "session.stopped"))

    async def events(self, session_id: str) -> AsyncIterator[AgentEvent]:
        queue = self._queue(session_id)
        while True:
            yield await queue.get()


class SubprocessProviderBridge(ProviderBridge):
    """Line-delimited normalized bridge for an installed provider process."""

    def __init__(self, *, provider: str, command: str | list[str] | None = None) -> None:
        self.provider = provider
        raw = command or os.getenv("AGENT_PROVIDER_COMMAND", "")
        self.command = shlex.split(raw) if isinstance(raw, str) else list(raw or [])
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._queues: dict[str, asyncio.Queue[AgentEvent]] = {}

    async def start(self, session_id: str, *, cwd: str, options: Mapping[str, Any]) -> None:
        if not self.command:
            raise RuntimeError(f"provider command is not configured: {self.provider}")
        process = await asyncio.create_subprocess_exec(*self.command, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._processes[session_id] = process
        self._queues[session_id] = asyncio.Queue()
        asyncio.create_task(self._read(session_id, process))
        await self._send(session_id, {"op": "start", "session_id": session_id, "cwd": cwd, "options": dict(options)})

    async def _send(self, session_id: str, value: dict[str, Any]) -> None:
        process = self._processes.get(session_id)
        if not process or not process.stdin:
            raise RuntimeError("provider process is not running")
        process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()

    async def _read(self, session_id: str, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping) and isinstance(value.get("type"), str):
                await self._queues[session_id].put(event(session_id, str(value["type"]), dict(value.get("payload") or {})))

    async def turn(self, session_id: str, turn_id: str, text: str) -> None:
        await self._send(session_id, {"op": "turn", "session_id": session_id, "turn_id": turn_id, "input": text})

    async def interrupt(self, session_id: str, turn_id: str | None = None) -> None:
        await self._send(session_id, {"op": "interrupt", "session_id": session_id, "turn_id": turn_id})

    async def respond(self, session_id: str, request_id: str, payload: Mapping[str, Any]) -> None:
        await self._send(session_id, {"op": "respond", "session_id": session_id, "request_id": request_id, "payload": dict(payload)})

    async def stop(self, session_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._send(session_id, {"op": "stop", "session_id": session_id})
        process = self._processes.pop(session_id, None)
        if process:
            process.terminate()

    async def events(self, session_id: str) -> AsyncIterator[AgentEvent]:
        queue = self._queues.setdefault(session_id, asyncio.Queue())
        while True:
            yield await queue.get()

    async def close(self) -> None:
        for session_id in tuple(self._processes):
            await self.stop(session_id)


class ProviderBridgeRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Any] = {}

    def register(self, provider: str, factory: Any, *, replace: bool = False) -> None:
        name = str(provider).strip().lower()
        if not name or (name in self._factories and not replace):
            raise ValueError("provider is already registered or empty")
        self._factories[name] = factory

    def create(self, provider: str, **options: Any) -> ProviderBridge:
        try:
            return self._factories[str(provider).strip().lower()](**options)
        except KeyError as error:
            raise ValueError(f"provider is unavailable: {provider}") from error
