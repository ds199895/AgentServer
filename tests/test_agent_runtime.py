from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.agent_runtime.service import AgentSessionService
from app.agent_runtime.store import AgentEventStore


class AgentRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.service = AgentSessionService(AgentEventStore(Path(self.directory.name) / "agent.db"))

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.directory.cleanup()

    async def test_session_projects_provider_events(self) -> None:
        session = await self.service.create(owner_id="alice", provider="generic", device_id="device-a", cwd="/workspace")
        await asyncio.sleep(0)
        await self.service.send_turn("alice", session.id, "inspect files")
        await asyncio.sleep(0.02)
        current = await self.service.get("alice", session.id)
        assert current is not None
        self.assertEqual(current.session_kind, "agent")
        self.assertEqual(current.state, "ready")
        self.assertEqual(current.turns[-1].state, "completed")
        self.assertEqual(current.messages[-1].role, "assistant")
        self.assertEqual(current.activities[-1].status, "completed")
        self.assertEqual([event.sequence for event in self.service.store.events("alice", session.id)], list(range(1, current.sequence + 1)))

    async def test_replay_is_scoped_and_monotonic(self) -> None:
        session = await self.service.create(owner_id="alice", provider="generic", cwd=".")
        await asyncio.sleep(0)
        await self.service.send_turn("alice", session.id, "hello")
        await asyncio.sleep(0.02)
        events = self.service.store.events("alice", session.id, after=2)
        self.assertTrue(events)
        self.assertEqual(events[0].sequence, 3)
        self.assertEqual(self.service.store.events("bob", session.id), [])
        self.assertIsNone(await self.service.get("bob", session.id))

    async def test_interrupt_and_close_are_normalized(self) -> None:
        session = await self.service.create(owner_id="alice", provider="claude", cwd=".")
        await self.service.send_turn("alice", session.id, "long task")
        await self.service.command("alice", session.id, "interrupt")
        await asyncio.sleep(0.01)
        current = await self.service.get("alice", session.id)
        assert current is not None
        self.assertIn(current.state, {"ready", "running"})
        await self.service.command("alice", session.id, "close")
        await asyncio.sleep(0.01)
        self.assertEqual((await self.service.get("alice", session.id)).state, "stopped")

    async def test_durable_snapshot_can_be_rehydrated(self) -> None:
        session = await self.service.create(owner_id="alice", provider="generic", cwd="/workspace")
        await asyncio.sleep(0.01)
        path = Path(self.directory.name) / "agent.db"
        await self.service.close()
        self.service = AgentSessionService(AgentEventStore(path))
        recovered = await self.service.get("alice", session.id)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.cwd, "/workspace")
        self.assertGreaterEqual(recovered.sequence, 2)


if __name__ == "__main__":
    unittest.main()
