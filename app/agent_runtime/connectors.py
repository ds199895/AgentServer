from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from app.execution.device_runtime import (
    DeviceRuntimeNotFound,
    DeviceRuntimeService,
    RuntimeSessionEvent,
)

from .bridge import InMemoryProviderBridge, ProviderBridge, ProviderBridgeRegistry
from .events import AgentEvent


class AgentConnectorError(RuntimeError):
    """A target device or provider cannot accept an AgentSession operation."""


@dataclass(frozen=True)
class AgentLaunchSpec:
    owner_id: str
    device_id: str
    session_id: str
    provider: str
    cwd: str
    permission_mode: str
    model: str | None = None
    resume_cursor: Mapping[str, Any] | None = None
    stable_turn_id: str | None = None


@dataclass(frozen=True)
class ConnectorBinding:
    executor_id: str
    bridge_instance_id: str
    transport: str
    device_generation: int
    platform: Mapping[str, Any]
    capabilities: Mapping[str, Any]


class DeviceConnector(ABC):
    """Route provider-neutral AgentSession operations to one explicit device."""

    @abstractmethod
    async def status(self, owner_id: str, device_id: str) -> Mapping[str, Any]: ...

    @abstractmethod
    async def start(self, spec: AgentLaunchSpec) -> ConnectorBinding: ...

    @abstractmethod
    async def turn(
        self,
        spec: AgentLaunchSpec,
        turn_id: str,
        text: str,
        options: Mapping[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    async def interrupt(
        self, spec: AgentLaunchSpec, turn_id: str | None
    ) -> None: ...

    @abstractmethod
    async def respond(
        self, spec: AgentLaunchSpec, request_id: str, payload: Mapping[str, Any]
    ) -> None: ...

    @abstractmethod
    async def stop(self, spec: AgentLaunchSpec) -> None: ...

    @abstractmethod
    def events(
        self, spec: AgentLaunchSpec, after_sequence: int = 0
    ) -> AsyncIterator[AgentEvent]: ...

    async def close(self) -> None:
        return None


class ProviderRegistryConnector(DeviceConnector):
    """In-process connector used only by isolated tests and local development."""

    def __init__(self, registry: ProviderBridgeRegistry | None = None) -> None:
        self.registry = registry or ProviderBridgeRegistry()
        self.registry.register("generic", InMemoryProviderBridge, replace=True)
        self.registry.register("claude", InMemoryProviderBridge, replace=True)
        self.registry.register("kimi", InMemoryProviderBridge, replace=True)
        self._bridges: dict[str, ProviderBridge] = {}

    async def status(self, owner_id: str, device_id: str) -> Mapping[str, Any]:
        del owner_id
        return {
            "device_id": device_id,
            "online": True,
            "transport": "in-process-test",
            "capabilities": {
                "providers": [
                    {"id": name, "available": True}
                    for name in sorted(self.registry._factories)
                ]
            },
            "platform": {},
        }

    async def start(self, spec: AgentLaunchSpec) -> ConnectorBinding:
        bridge = self._bridges.get(spec.session_id)
        if bridge is None:
            bridge = self.registry.create(spec.provider)
            self._bridges[spec.session_id] = bridge
            await bridge.start(
                spec.session_id,
                cwd=spec.cwd,
                options={
                    "permission_mode": spec.permission_mode,
                    "model": spec.model,
                    "resume_cursor": spec.resume_cursor,
                },
            )
        return ConnectorBinding(
            executor_id="in-process-test",
            bridge_instance_id="in-process-test",
            transport="in-process-test",
            device_generation=1,
            platform={},
            capabilities=(await self.status(spec.owner_id, spec.device_id))[
                "capabilities"
            ],
        )

    def _bridge(self, spec: AgentLaunchSpec) -> ProviderBridge:
        try:
            return self._bridges[spec.session_id]
        except KeyError as error:
            raise AgentConnectorError("agent session bridge is not active") from error

    async def turn(
        self,
        spec: AgentLaunchSpec,
        turn_id: str,
        text: str,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        del options  # The in-process test bridge has no provider options.
        await self._bridge(spec).turn(spec.session_id, turn_id, text)

    async def interrupt(self, spec: AgentLaunchSpec, turn_id: str | None) -> None:
        await self._bridge(spec).interrupt(spec.session_id, turn_id)

    async def respond(
        self, spec: AgentLaunchSpec, request_id: str, payload: Mapping[str, Any]
    ) -> None:
        await self._bridge(spec).respond(spec.session_id, request_id, payload)

    async def stop(self, spec: AgentLaunchSpec) -> None:
        await self._bridge(spec).stop(spec.session_id)

    async def events(
        self, spec: AgentLaunchSpec, after_sequence: int = 0
    ) -> AsyncIterator[AgentEvent]:
        del after_sequence
        async for value in self._bridge(spec).events(spec.session_id):
            yield value

    async def close(self) -> None:
        for bridge in set(self._bridges.values()):
            await bridge.close()


class DeviceRuntimeConnector(DeviceConnector):
    """Authenticated outbound connector for Agent bridges running on devices.

    The wire transport is the existing hardened device channel. AgentSession is
    the only browser-facing contract; legacy RuntimeSession routes are not used.
    """

    def __init__(self, service: DeviceRuntimeService, *, poll_interval: float = 0.2):
        self.service = service
        self.poll_interval = max(0.02, float(poll_interval))
        self._closed = asyncio.Event()
        self._active_turns: dict[str, str] = {}

    async def status(self, owner_id: str, device_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            self.service.runtime_status, owner_id=owner_id, device_id=device_id
        )

    @staticmethod
    def _provider(status: Mapping[str, Any], provider: str) -> Mapping[str, Any] | None:
        capabilities = status.get("capabilities")
        values = capabilities.get("providers") if isinstance(capabilities, Mapping) else []
        if not isinstance(values, list):
            return None
        for value in values:
            if isinstance(value, Mapping) and str(value.get("id") or "") == provider:
                return value
        return None

    async def _require_available(self, spec: AgentLaunchSpec) -> Mapping[str, Any]:
        status = await self.status(spec.owner_id, spec.device_id)
        if not status.get("online"):
            raise AgentConnectorError("target device Agent bridge is offline")
        provider = self._provider(status, spec.provider)
        if provider is None or provider.get("available") is False:
            raise AgentConnectorError(
                f"provider is unavailable on target device: {spec.provider}"
            )
        return status

    async def start(self, spec: AgentLaunchSpec) -> ConnectorBinding:
        status = await self._require_available(spec)
        try:
            existing = await asyncio.to_thread(
                self.service.get_session,
                owner_id=spec.owner_id,
                session_id=spec.session_id,
            )
        except DeviceRuntimeNotFound:
            existing = None
        if existing is None:
            await asyncio.to_thread(
                self.service.create_session,
                owner_id=spec.owner_id,
                device_id=spec.device_id,
                provider=spec.provider,
                workspace=spec.cwd,
                session_id=spec.session_id,
                options={
                    "permission_mode": spec.permission_mode,
                    "model": spec.model,
                    "resume_cursor": (
                        dict(spec.resume_cursor)
                        if spec.resume_cursor is not None
                        else None
                    ),
                },
            )
        else:
            if (
                existing.device_id != spec.device_id
                or existing.provider != spec.provider
                or existing.workspace != spec.cwd
            ):
                raise AgentConnectorError(
                    "agent session is bound to a different device runtime"
                )
            if existing.lifecycle in {"stopped", "failed", "lost"}:
                raise AgentConnectorError(
                    f"device provider session is {existing.lifecycle}"
                )
            if (
                existing.runtime_session_id
                != str(status.get("runtime_session_id") or "")
                or existing.runtime_generation != int(status.get("generation") or 0)
            ):
                raise AgentConnectorError(
                    "device provider session belongs to a stale bridge generation"
                )
        return ConnectorBinding(
            executor_id=str(status.get("instance_id") or ""),
            bridge_instance_id=str(status.get("runtime_session_id") or ""),
            transport="outbound-agent",
            device_generation=int(status.get("generation") or 0),
            platform=dict(status.get("platform") or {}),
            capabilities=dict(status.get("capabilities") or {}),
        )

    async def turn(
        self,
        spec: AgentLaunchSpec,
        turn_id: str,
        text: str,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        self._active_turns[spec.session_id] = turn_id
        await asyncio.to_thread(
            self.service.send_turn,
            owner_id=spec.owner_id,
            session_id=spec.session_id,
            input=text,
            turn_id=turn_id,
            options=dict(options) if options else None,
        )

    async def interrupt(self, spec: AgentLaunchSpec, turn_id: str | None) -> None:
        await asyncio.to_thread(
            self.service.interrupt_session,
            owner_id=spec.owner_id,
            session_id=spec.session_id,
            turn_id=turn_id,
        )

    async def respond(
        self, spec: AgentLaunchSpec, request_id: str, payload: Mapping[str, Any]
    ) -> None:
        await asyncio.to_thread(
            self.service.respond_to_request,
            owner_id=spec.owner_id,
            session_id=spec.session_id,
            request_id=request_id,
            response=dict(payload),
        )

    async def stop(self, spec: AgentLaunchSpec) -> None:
        await asyncio.to_thread(
            self.service.stop_session,
            owner_id=spec.owner_id,
            session_id=spec.session_id,
        )

    def _normalize(self, source: RuntimeSessionEvent) -> AgentEvent | None:
        payload = dict(source.payload)
        source_turn_id = str(payload.get("turn_id") or "") or None
        item_id = str(payload.get("item_id") or "") or None
        request_id = str(
            payload.get("interaction_id") or payload.get("request_id") or ""
        ) or None
        typ = source.type
        target: str | None = None
        body: dict[str, Any] = dict(payload)

        if typ == "turn.input":
            if source_turn_id:
                self._active_turns[source.session_id] = source_turn_id
            return None
        local_turn_id = self._active_turns.get(source.session_id) or source_turn_id

        if typ in {"session.started", "thread.started"}:
            target = "session.ready"
            body["provider_session_id"] = (
                payload.get("provider_session_id")
                or payload.get("provider_thread_id")
            )
        elif typ == "session.state.changed":
            state = str(payload.get("state") or "")
            target = {
                "ready": "session.ready",
                "stopped": "session.stopped",
                "error": "session.failed",
                "failed": "session.failed",
            }.get(state, "session.state.changed")
        elif typ == "turn.started":
            target = "turn.started"
            body["turn_id"] = local_turn_id
        elif typ in {"turn.completed", "turn.failed"}:
            state = str(payload.get("state") or "")
            target = (
                "turn.failed"
                if typ == "turn.failed" or state == "failed"
                else "turn.interrupted"
                if state == "interrupted"
                else "turn.completed"
            )
            body["turn_id"] = local_turn_id
            self._active_turns.pop(source.session_id, None)
        elif typ in {"message.delta", "message.completed"}:
            target = "message.delta" if typ.endswith("delta") else "message.created"
            body.update(
                message_id=item_id or source.event_id,
                role=str(payload.get("role") or "assistant"),
                text=str(payload.get("text") or ""),
                turn_id=local_turn_id,
                item_id=item_id,
            )
        elif typ == "reasoning.delta":
            target = "message.delta"
            body.update(
                message_id=f"reasoning-{item_id or source.event_id}",
                role="reasoning",
                text=str(payload.get("text") or ""),
                turn_id=local_turn_id,
                item_id=item_id,
            )
        elif typ in {"tool.output.delta", "file.output.delta"}:
            target = "activity.updated"
            body.update(
                activity_id=item_id or source.event_id,
                status="running",
                output_delta=str(payload.get("text") or ""),
                turn_id=local_turn_id,
                item_id=item_id,
            )
        elif typ in {"item.started", "item.completed"}:
            item_type = str(payload.get("item_type") or "tool")
            if item_type in {"user_message", "assistant_message"}:
                return None
            target = "activity.started" if typ.endswith("started") else "activity.completed"
            kind = {
                "reasoning": "status",
                "plan": "plan",
                "command_execution": "command",
                "file_change": "file",
                "mcp_tool_call": "tool",
                "dynamic_tool_call": "tool",
                "web_search": "tool",
            }.get(item_type, "status")
            body.update(
                activity_id=item_id or source.event_id,
                kind=kind,
                title=str(payload.get("title") or item_type.replace("_", " ").title()),
                status=str(
                    payload.get("status")
                    or ("completed" if typ.endswith("completed") else "running")
                ),
                detail=str(payload.get("detail") or ""),
                input=payload.get("input"),
                output=payload.get("output"),
                turn_id=local_turn_id,
                item_id=item_id,
            )
        elif typ == "turn.plan.updated":
            target = "plan.updated"
            body["turn_id"] = local_turn_id
            # One plan row per turn. Without a stable id every plan revision
            # appends another "Plan updated" card instead of replacing the
            # previous one, so a turn that re-plans five times shows five cards.
            body["activity_id"] = f"plan-{local_turn_id or source.event_id}"
        elif typ in {
            "interaction.opened",
            "interaction.requested",
            "request.opened",
            "user-input.requested",
        }:
            target = "request.created"
            kind = str(
                payload.get("kind")
                or payload.get("request_type")
                or ("user_input" if typ == "user-input.requested" else "approval")
            )
            body.update(
                request_id=request_id or source.event_id,
                kind="user_input" if "input" in kind else "approval",
                title=str(payload.get("title") or "Action requires confirmation"),
                detail=str(payload.get("detail") or kind.replace("_", " ")),
                input=payload.get("input"),
                options=list(payload.get("questions") or []),
                turn_id=local_turn_id,
            )
        elif typ in {
            "interaction.resolved",
            "request.closed",
            "request.resolved",
            "user-input.resolved",
        }:
            target = "request.resolved"
            body["request_id"] = request_id
        elif typ == "runtime.error":
            target = "turn.failed" if local_turn_id else "session.failed"
            body["turn_id"] = local_turn_id
            body["error"] = str(
                payload.get("error") or payload.get("code") or "provider error"
            )
        elif typ == "runtime.warning":
            target = "activity.completed"
            body.update(
                activity_id=source.event_id,
                kind="status",
                title="Provider warning",
                status="warning",
                detail=str(
                    payload.get("error")
                    or payload.get("code")
                    or "Provider warning"
                ),
                turn_id=local_turn_id,
            )
        elif typ == "session.stopped":
            target = "session.stopped"
        elif typ == "session.failed":
            target = "session.failed"
        if target is None:
            return None
        body["source_sequence"] = source.sequence
        body["device_id"] = source.device_id
        body["bridge_instance_id"] = source.runtime_session_id
        body["device_generation"] = source.runtime_generation
        return AgentEvent(
            0,
            source.event_id,
            source.session_id,
            target,
            body,
            float(source.occurred_at or source.recorded_at),
        )

    async def events(
        self, spec: AgentLaunchSpec, after_sequence: int = 0
    ) -> AsyncIterator[AgentEvent]:
        if spec.stable_turn_id:
            self._active_turns.setdefault(spec.session_id, spec.stable_turn_id)
        cursor = max(0, int(after_sequence))
        while not self._closed.is_set():
            terminal_event_seen = False
            values = await asyncio.to_thread(
                self.service.session_events,
                owner_id=spec.owner_id,
                session_id=spec.session_id,
                after_sequence=cursor,
                limit=200,
            )
            for source in values:
                cursor = max(cursor, source.sequence)
                normalized = self._normalize(source)
                if normalized is not None:
                    terminal_event_seen = terminal_event_seen or (
                        normalized.type in {"session.stopped", "session.failed"}
                    )
                    yield normalized
            if terminal_event_seen:
                return
            current = await asyncio.to_thread(
                self.service.get_session,
                owner_id=spec.owner_id,
                session_id=spec.session_id,
            )
            if current.lifecycle in {"stopped", "failed", "lost"}:
                failed = current.lifecycle in {"failed", "lost"}
                yield AgentEvent(
                    0,
                    (
                        "device-runtime-lifecycle:"
                        f"{current.session_id}:{current.runtime_session_id}:"
                        f"{current.runtime_generation}:{current.revision}:"
                        f"{current.lifecycle}"
                    ),
                    current.session_id,
                    "session.failed" if failed else "session.stopped",
                    {
                        "error": (
                            current.last_error
                            or f"device provider session is {current.lifecycle}"
                        )
                        if failed
                        else "",
                        "device_id": current.device_id,
                        "bridge_instance_id": current.runtime_session_id,
                        "device_generation": current.runtime_generation,
                        "runtime_lifecycle": current.lifecycle,
                    },
                    current.updated_at,
                )
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._closed.wait(), timeout=self.poll_interval)

    async def close(self) -> None:
        self._closed.set()
