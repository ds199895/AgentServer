from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.agent_runtime.connectors import DeviceRuntimeConnector
from app.agent_runtime.service import AgentSessionService
from app.agent_runtime.store import AgentEventStore
from app.execution.device_runtime import DeviceRuntimeService, DeviceRuntimeStore
from app.execution.device_runtime_api import build_device_runtime_router
from app.execution.runtime_adapters.base import (
    ApprovalDecision,
    RuntimeAdapter,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeProbe,
    RuntimeSession,
    RuntimeSessionSpec,
    RuntimeThreadSnapshot,
    RuntimeTurn,
    RuntimeTurnInput,
)
from app.execution.runtime_host import DeviceRuntimeHost
from app.execution.store import ExecutionStore


_END = object()


class EchoCodexAdapter(RuntimeAdapter):
    """Typed device-side provider fixture exercising the complete Host wire path."""

    provider = "codex"
    capabilities = RuntimeCapabilities(
        interrupt=True,
        approvals=True,
        user_input=True,
    )

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.session_id = ""
        self.queue: asyncio.Queue[RuntimeEvent | object] = asyncio.Queue()
        self.turn_count = 0
        self.pending_turn_id: str | None = None

    async def probe(self) -> RuntimeProbe:
        return RuntimeProbe(available=True, version="echo-codex/1")

    async def start_session(self, spec: RuntimeSessionSpec) -> RuntimeSession:
        self.session_id = spec.session_id
        await self.emit(
            "session.started",
            {"provider_session_id": f"thread-{self.device_id}-{spec.session_id}"},
        )
        return RuntimeSession(
            session_id=spec.session_id,
            provider=self.provider,
            state="ready",
            cwd=str(spec.cwd),
        )

    async def send_turn(
        self, session_id: str, turn: RuntimeTurnInput
    ) -> RuntimeTurn:
        self.turn_count += 1
        turn_id = f"provider-{self.device_id}-{self.turn_count}"
        await self.emit("turn.started", {}, turn_id=turn_id)
        if turn.text == "needs approval":
            self.pending_turn_id = turn_id
            await self.emit(
                "interaction.opened",
                {
                    "interaction_id": "approval-1",
                    "request_type": "command_execution_approval",
                },
                turn_id=turn_id,
                interaction_id="approval-1",
            )
            return RuntimeTurn(session_id=session_id, turn_id=turn_id)
        await self.emit(
            "message.completed",
            {
                "role": "assistant",
                "text": f"{self.device_id}:{turn.text}",
            },
            turn_id=turn_id,
            item_id=f"message-{self.device_id}-{self.turn_count}",
        )
        await self.emit(
            "turn.completed", {"state": "completed"}, turn_id=turn_id
        )
        return RuntimeTurn(session_id=session_id, turn_id=turn_id)

    async def interrupt_turn(
        self, session_id: str, turn_id: str | None = None
    ) -> None:
        await self.emit(
            "turn.completed", {"state": "interrupted"}, turn_id=turn_id
        )

    async def respond_to_approval(
        self,
        session_id: str,
        interaction_id: str,
        decision: ApprovalDecision | str,
    ) -> None:
        del session_id
        resolved = decision.value if isinstance(decision, ApprovalDecision) else str(decision)
        turn_id = self.pending_turn_id
        await self.emit(
            "interaction.resolved",
            {"interaction_id": interaction_id, "resolution": resolved},
            turn_id=turn_id,
            interaction_id=interaction_id,
        )
        await self.emit(
            "message.completed",
            {"role": "assistant", "text": f"approved:{resolved}"},
            turn_id=turn_id,
            item_id=f"approval-message-{self.device_id}",
        )
        await self.emit(
            "turn.completed", {"state": "completed"}, turn_id=turn_id
        )
        self.pending_turn_id = None

    async def respond_to_user_input(
        self,
        session_id: str,
        interaction_id: str,
        answers: Mapping[str, str | Sequence[str]],
    ) -> None:
        del session_id, interaction_id, answers

    async def read_thread(self, session_id: str) -> RuntimeThreadSnapshot:
        return RuntimeThreadSnapshot(thread_id=f"thread-{self.device_id}-{session_id}")

    async def rollback_thread(
        self, session_id: str, num_turns: int
    ) -> RuntimeThreadSnapshot:
        del num_turns
        return await self.read_thread(session_id)

    async def stop_session(self, session_id: str) -> None:
        del session_id
        await self.emit("session.stopped", {})

    async def list_sessions(self) -> tuple[RuntimeSession, ...]:
        return ()

    async def events(
        self, session_id: str | None = None
    ) -> AsyncIterator[RuntimeEvent]:
        del session_id
        while True:
            value = await self.queue.get()
            if value is _END:
                return
            assert isinstance(value, RuntimeEvent)
            yield value

    async def close(self) -> None:
        await self.queue.put(_END)

    async def emit(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        turn_id: str | None = None,
        item_id: str | None = None,
        interaction_id: str | None = None,
    ) -> None:
        await self.queue.put(
            RuntimeEvent(
                event_id=uuid.uuid4().hex,
                provider=self.provider,
                session_id=self.session_id,
                type=event_type,
                payload=dict(payload),
                turn_id=turn_id,
                item_id=item_id,
                interaction_id=interaction_id,
                occurred_at=time.time(),
            )
        )


class AgentRuntimeDeviceHostEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "server.db"
        execution = ExecutionStore(self.database)
        self.runtime = DeviceRuntimeService(
            DeviceRuntimeStore(self.database),
            execution,
            device_exists=lambda owner, device: (
                owner == "alice" and device in {"device-a", "device-b"}
            ),
            offline_after=30,
        )
        app = FastAPI()

        def browser_user() -> str:
            return "alice"

        app.include_router(build_device_runtime_router(browser_user))
        app.state.device_runtime = self.runtime
        self.transport = httpx.ASGITransport(app=app)
        self.hosts: dict[str, DeviceRuntimeHost] = {}
        self.adapters: dict[str, list[EchoCodexAdapter]] = {
            "device-a": [],
            "device-b": [],
        }
        for device_id in self.adapters:
            enrollment = self.runtime.issue_enrollment(
                owner_id="alice", device_id=device_id
            )
            token_path = self.root / f"{device_id}.token"
            token_path.write_text(enrollment.token + "\n", encoding="utf-8")
            os.chmod(token_path, 0o600)

            def factory(*, selected: str = device_id) -> EchoCodexAdapter:
                adapter = EchoCodexAdapter(selected)
                self.adapters[selected].append(adapter)
                return adapter

            factory.capabilities = EchoCodexAdapter.capabilities  # type: ignore[attr-defined]
            factory.transport = "typed-fixture"  # type: ignore[attr-defined]
            host = DeviceRuntimeHost(
                device_id=device_id,
                base_url="http://localhost",
                state_dir=self.root / f"host-{device_id}",
                adapter_registry={"codex": factory},
                http_transport=self.transport,
            )
            await host.enroll_from_file(token_path)
            await host.heartbeat()
            self.hosts[device_id] = host
        self.service = AgentSessionService(
            AgentEventStore(self.database),
            DeviceRuntimeConnector(self.runtime, poll_interval=0.01),
        )

    async def asyncTearDown(self) -> None:
        await self.service.close()
        await asyncio.gather(*(host.close() for host in self.hosts.values()))
        await self.transport.aclose()
        self.directory.cleanup()

    async def cycle(self, device_id: str) -> None:
        host = self.hosts[device_id]
        await host.poll_commands()
        for _ in range(100):
            await asyncio.sleep(0)
            if len(host.event_spool):
                await host.flush_events()
            if not len(host.event_spool):
                await asyncio.sleep(0)
                if not len(host.event_spool):
                    return
        self.fail(f"{device_id} event spool did not drain")

    async def wait_for(self, predicate, timeout: float = 2.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.01)
        self.fail("condition did not converge")

    async def test_agent_sessions_execute_on_two_exact_device_hosts(self) -> None:
        first, second = await asyncio.gather(
            self.service.create(
                owner_id="alice",
                device_id="device-a",
                provider="codex",
                cwd=str(self.root),
                session_id="agent-a",
            ),
            self.service.create(
                owner_id="alice",
                device_id="device-b",
                provider="codex",
                cwd=str(self.root),
                session_id="agent-b",
            ),
        )
        self.assertNotEqual(first.executor_id, second.executor_id)
        self.assertEqual(first.transport, "outbound-agent")
        await asyncio.gather(self.cycle("device-a"), self.cycle("device-b"))

        async def ready():
            values = await asyncio.gather(
                self.service.get("alice", "agent-a"),
                self.service.get("alice", "agent-b"),
            )
            return values if all(value and value.state == "ready" for value in values) else None

        await self.wait_for(ready)
        await asyncio.gather(
            self.service.send_turn("alice", "agent-a", "alpha", "client-a"),
            self.service.send_turn("alice", "agent-b", "beta", "client-b"),
        )
        await asyncio.gather(self.cycle("device-a"), self.cycle("device-b"))

        async def completed():
            values = await asyncio.gather(
                self.service.get("alice", "agent-a"),
                self.service.get("alice", "agent-b"),
            )
            return values if all(value and value.turns[-1].state == "completed" for value in values) else None

        completed_first, completed_second = await self.wait_for(completed)
        assert completed_first is not None and completed_second is not None
        self.assertEqual(completed_first.messages[-1].text, "device-a:alpha")
        self.assertEqual(completed_second.messages[-1].text, "device-b:beta")
        self.assertEqual(completed_first.messages[-1].turn_id, "client-a")
        self.assertEqual(completed_second.messages[-1].turn_id, "client-b")
        self.assertEqual(self.adapters["device-a"][0].turn_count, 1)
        self.assertEqual(self.adapters["device-b"][0].turn_count, 1)

    async def test_server_service_restart_attaches_without_duplicate_start(self) -> None:
        await self.service.create(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            cwd=str(self.root),
            session_id="restart-agent",
        )
        await self.cycle("device-a")

        async def ready():
            value = await self.service.get("alice", "restart-agent")
            return value if value and value.state == "ready" else None

        await self.wait_for(ready)
        self.assertEqual(len(self.adapters["device-a"]), 1)
        await self.service.close()
        self.service = AgentSessionService(
            AgentEventStore(self.database),
            DeviceRuntimeConnector(self.runtime, poll_interval=0.01),
        )

        restored = await self.service.create(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            cwd=str(self.root),
            session_id="restart-agent",
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.state, "ready")
        await self.hosts["device-a"].poll_commands()
        self.assertEqual(len(self.adapters["device-a"]), 1)

        await self.service.send_turn(
            "alice", "restart-agent", "after restart", "stable-restart-turn"
        )
        await self.cycle("device-a")

        async def completed():
            value = await self.service.get("alice", "restart-agent")
            return (
                value
                if value and value.turns[-1].state == "completed"
                else None
            )

        restored = await self.wait_for(completed)
        self.assertEqual(restored.messages[-1].text, "device-a:after restart")
        self.assertEqual(restored.messages[-1].turn_id, "stable-restart-turn")

    async def test_approval_request_round_trips_through_agent_contract(self) -> None:
        await self.service.create(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            cwd=str(self.root),
            session_id="approval-agent",
        )
        await self.cycle("device-a")

        async def ready():
            value = await self.service.get("alice", "approval-agent")
            return value if value and value.state == "ready" else None

        await self.wait_for(ready)
        await self.service.send_turn(
            "alice", "approval-agent", "needs approval", "approval-turn"
        )
        await self.cycle("device-a")

        async def waiting():
            value = await self.service.get("alice", "approval-agent")
            return (
                value
                if value
                and value.state == "waiting"
                and value.requests
                and value.requests[-1].status == "pending"
                else None
            )

        waiting_session = await self.wait_for(waiting)
        self.assertEqual(waiting_session.requests[-1].id, "approval-1")
        self.assertEqual(waiting_session.requests[-1].kind, "approval")
        await self.service.command(
            "alice",
            "approval-agent",
            "respond",
            {"request_id": "approval-1", "decision": "approve_once"},
        )
        await self.cycle("device-a")

        async def completed():
            value = await self.service.get("alice", "approval-agent")
            return (
                value
                if value
                and value.turns[-1].state == "completed"
                and value.requests[-1].status == "resolved"
                else None
            )

        completed_session = await self.wait_for(completed)
        self.assertEqual(completed_session.messages[-1].text, "approved:approve_once")
        self.assertEqual(completed_session.messages[-1].turn_id, "approval-turn")


if __name__ == "__main__":
    unittest.main()
