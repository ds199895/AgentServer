from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.agent_runtime.connectors import DeviceRuntimeConnector
from app.agent_runtime.service import AgentSessionService
from app.agent_runtime.store import AgentEventStore
from app.execution.device_runtime import DeviceRuntimeService, DeviceRuntimeStore
from app.execution.models import CommandStatus
from app.execution.store import ExecutionStore


class AgentRuntimeDeviceConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = Path(self.directory.name) / "agent.db"
        self.execution = ExecutionStore(database)
        self.runtime_store = DeviceRuntimeStore(database)
        self.devices = {("alice", "device-a"), ("alice", "device-b")}
        self.runtime = DeviceRuntimeService(
            self.runtime_store,
            self.execution,
            device_exists=lambda owner, device: (owner, device) in self.devices,
        )
        self.hosts = {}
        for index, device_id in enumerate(("device-a", "device-b"), 1):
            enrollment = self.runtime.issue_enrollment(
                owner_id="alice", device_id=device_id
            )
            grant = self.runtime.consume_enrollment(enrollment.token)
            runtime_session_id = f"bridge-{index}"
            self.runtime.heartbeat(
                grant.claims,
                instance_id=f"executor-{index}",
                boot_id=f"boot-{index}",
                runtime_session_id=runtime_session_id,
                generation=1,
                capabilities={
                    "providers": [
                        {
                            "id": "codex",
                            "available": True,
                            "version": "test",
                            "features": ["turn", "interrupt", "approval"],
                        }
                    ],
                    "features": [],
                },
                platform={"os": "linux", "arch": "x86_64", "hostname": device_id},
            )
            self.hosts[device_id] = (grant.claims, runtime_session_id)
        self.service = AgentSessionService(
            AgentEventStore(database),
            DeviceRuntimeConnector(self.runtime, poll_interval=0.01),
        )

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.directory.cleanup()

    def poll(self, device_id: str, after: int = 0):
        claims, runtime_session_id = self.hosts[device_id]
        return self.runtime.poll_commands(
            claims,
            runtime_session_id=runtime_session_id,
            generation=1,
            after_sequence=after,
        )

    def ack(
        self,
        device_id: str,
        command_id: str,
        ack_id: str,
        payload: dict | None = None,
    ) -> None:
        claims, runtime_session_id = self.hosts[device_id]
        self.runtime.ack_command(
            claims,
            runtime_session_id=runtime_session_id,
            generation=1,
            command_id=command_id,
            status=CommandStatus.COMPLETED,
            ack_id=ack_id,
            payload=payload or {},
        )

    def events(self, device_id: str, session_id: str, *values: dict) -> None:
        claims, runtime_session_id = self.hosts[device_id]
        self.runtime.ingest_session_events(
            claims,
            runtime_session_id=runtime_session_id,
            generation=1,
            session_id=session_id,
            events=values,
        )

    async def wait_for(self, predicate, timeout: float = 1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.01)
        self.fail("condition did not converge")

    async def test_two_devices_route_commands_and_events_to_exact_bridge(self) -> None:
        first = await self.service.create(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            cwd="/workspace/a",
            session_id="agent-a",
        )
        second = await self.service.create(
            owner_id="alice",
            device_id="device-b",
            provider="codex",
            cwd="/workspace/b",
            session_id="agent-b",
        )
        self.assertEqual(first.executor_id, "executor-1")
        self.assertEqual(second.executor_id, "executor-2")
        self.assertEqual(first.transport, "outbound-agent")

        first_page = self.poll("device-a")
        second_page = self.poll("device-b")
        self.assertEqual([item.payload["session_id"] for item in first_page.commands], ["agent-a"])
        self.assertEqual([item.payload["session_id"] for item in second_page.commands], ["agent-b"])
        self.ack("device-a", first_page.commands[0].id, "ack-start-a")
        self.ack("device-b", second_page.commands[0].id, "ack-start-b")
        self.events(
            "device-a",
            "agent-a",
            {"event_id": "a-ready", "producer_seq": 1, "type": "session.started", "payload": {"state": "ready", "provider_session_id": "thread-a"}},
        )
        self.events(
            "device-b",
            "agent-b",
            {"event_id": "b-ready", "producer_seq": 1, "type": "session.started", "payload": {"state": "ready", "provider_session_id": "thread-b"}},
        )

        async def both_ready():
            a = await self.service.get("alice", "agent-a")
            b = await self.service.get("alice", "agent-b")
            return a if a and b and a.state == b.state == "ready" else None

        await self.wait_for(both_ready)
        turn = await self.service.send_turn("alice", "agent-a", "inspect a", "turn-a")
        self.assertEqual(turn.id, "turn-a")
        turn_page = self.poll("device-a", first_page.next_sequence)
        self.assertEqual([item.type for item in turn_page.commands], ["session.turn"])
        self.assertEqual(self.poll("device-b", second_page.next_sequence).commands, ())
        self.ack(
            "device-a",
            turn_page.commands[0].id,
            "ack-turn-a",
            {"session_id": "agent-a", "turn_id": "provider-turn-a"},
        )
        self.events(
            "device-a",
            "agent-a",
            {"event_id": "a-turn-input", "producer_seq": 2, "type": "turn.input", "payload": {"turn_id": "turn-a", "text": "inspect a"}},
            {"event_id": "a-turn-start", "producer_seq": 3, "type": "turn.started", "payload": {"turn_id": "provider-turn-a"}},
            {"event_id": "a-message", "producer_seq": 4, "type": "message.completed", "payload": {"turn_id": "provider-turn-a", "item_id": "message-a", "text": "done"}},
            {"event_id": "a-turn-done", "producer_seq": 5, "type": "turn.completed", "payload": {"turn_id": "provider-turn-a", "state": "completed"}},
        )

        async def completed():
            value = await self.service.get("alice", "agent-a")
            return value if value and value.turns[-1].state == "completed" else None

        try:
            completed_session = await self.wait_for(completed)
        except AssertionError:
            current = await self.service.get("alice", "agent-a")
            source = self.runtime.session_events(owner_id="alice", session_id="agent-a")
            self.fail(
                f"session did not complete: {current.as_dict() if current else None}; "
                f"source={[item.as_dict() for item in source]}"
            )
        self.assertEqual(completed_session.messages[-1].text, "done")
        self.assertEqual(completed_session.messages[-1].turn_id, "turn-a")
        self.assertEqual(completed_session.device_id, "device-a")
        self.assertEqual(completed_session.bridge_instance_id, "bridge-1")

    async def test_offline_or_missing_provider_fails_before_session_command(self) -> None:
        with self.assertRaisesRegex(Exception, "provider is unavailable"):
            await self.service.create(
                owner_id="alice",
                device_id="device-a",
                provider="claude",
                cwd="/workspace",
            )
        self.assertEqual(self.poll("device-a").commands, ())


if __name__ == "__main__":
    unittest.main()
