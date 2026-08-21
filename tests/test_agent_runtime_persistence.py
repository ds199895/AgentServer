from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.agent_runtime.events import event
from app.agent_runtime.service import AgentSessionService
from app.agent_runtime.store import AgentEventStore


class AgentRuntimePersistenceTests(unittest.IsolatedAsyncioTestCase):
    """The durable session row holds metadata; history lives in the event log."""

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "agent.db"
        self.service = AgentSessionService(AgentEventStore(self.path))

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.directory.cleanup()

    def _stored_row(self, session_id: str) -> dict:
        raw = self.service.store.load_session("alice", session_id)
        assert raw is not None
        return raw

    async def test_session_row_excludes_transcript_history(self) -> None:
        session = await self.service.create(
            owner_id="alice", provider="generic", device_id="device-a", cwd="/workspace"
        )
        await self.service.send_turn("alice", session.id, "inspect files")
        await asyncio.sleep(0.02)

        current = await self.service.get("alice", session.id)
        assert current is not None
        self.assertTrue(current.messages, "in-memory projection still carries history")

        row = self._stored_row(session.id)
        for field in ("messages", "activities", "requests", "turns"):
            self.assertNotIn(field, row, f"{field} must not be duplicated into the session row")
        self.assertEqual(row["cwd"], "/workspace")
        self.assertEqual(row["sequence"], current.sequence)

    async def test_stored_row_does_not_grow_with_transcript_length(self) -> None:
        session = await self.service.create(owner_id="alice", provider="generic", cwd=".")
        await asyncio.sleep(0)
        baseline = len(json.dumps(self._stored_row(session.id)))

        for index in range(40):
            await self.service._dispatch(
                session,
                event(
                    session.id,
                    "message.delta",
                    {"message_id": "msg-1", "role": "assistant", "text": f"chunk {index} "},
                ),
            )

        current = await self.service.get("alice", session.id)
        assert current is not None
        self.assertIn("chunk 39", current.messages[-1].text)
        # Row size is bounded by metadata, not by how much text streamed through.
        self.assertLess(len(json.dumps(self._stored_row(session.id))), baseline + 512)

    async def test_restart_replays_history_from_the_event_log(self) -> None:
        session = await self.service.create(
            owner_id="alice", provider="generic", device_id="device-a", cwd="/workspace"
        )
        await self.service.send_turn("alice", session.id, "persist this timeline")
        await asyncio.sleep(0.02)
        before = await self.service.get("alice", session.id)
        assert before is not None
        expected = before.as_dict()

        await self.service.close()
        self.service = AgentSessionService(AgentEventStore(self.path))
        recovered = await self.service.get("alice", session.id)
        assert recovered is not None

        # A rehydrated session must be indistinguishable from the live one.
        self.assertEqual(recovered.as_dict(), expected)

    async def test_rehydration_is_idempotent(self) -> None:
        session = await self.service.create(owner_id="alice", provider="generic", cwd=".")
        await self.service.send_turn("alice", session.id, "hello")
        await asyncio.sleep(0.02)

        first = self.service._rehydrate(session).as_dict()
        second = self.service._rehydrate(session).as_dict()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
