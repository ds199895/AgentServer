from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_runtime.api import build_agent_router
from app.agent_runtime.service import AgentSessionService
from app.agent_runtime.store import AgentEventStore


class AgentRuntimeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_snapshot_turn_and_owner_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AgentSessionService(AgentEventStore(Path(directory) / "agent.db"))
            try:
                app = FastAPI()
                app.state.agent_sessions = service
                app.include_router(build_agent_router(lambda: "alice"))
                with TestClient(app) as client:
                    created = client.post("/api/agent/sessions", json={"provider": "generic", "device_id": "local-test", "cwd": "."})
                    self.assertEqual(created.status_code, 201, created.text)
                    session_id = created.json()["session"]["id"]
                    snapshot = client.get(f"/api/agent/sessions/{session_id}")
                    self.assertEqual(snapshot.status_code, 200)
                    self.assertEqual(snapshot.json()["session"]["session_kind"], "agent")
                    turn = client.post(f"/api/agent/sessions/{session_id}/turns", json={"input": "hello"})
                    self.assertEqual(turn.status_code, 202, turn.text)
                    stable = client.post(
                        f"/api/agent/sessions/{session_id}/turns",
                        json={"input": "idempotent", "turn_id": "stable-turn"},
                    )
                    repeated = client.post(
                        f"/api/agent/sessions/{session_id}/turns",
                        json={"input": "idempotent", "turn_id": "stable-turn"},
                    )
                    self.assertEqual(stable.status_code, 202, stable.text)
                    self.assertEqual(repeated.json()["turn"]["id"], "stable-turn")
                    events = client.get(f"/api/agent/sessions/{session_id}/events")
                    self.assertEqual(events.status_code, 200)
                    self.assertTrue(events.json()["events"])

                    missing_device = client.post(
                        "/api/agent/sessions", json={"provider": "generic", "cwd": "."}
                    )
                    self.assertEqual(missing_device.status_code, 422)
            finally:
                await service.close()

    async def test_session_id_cannot_cross_owner_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            alice = AgentSessionService(AgentEventStore(database))
            bob = AgentSessionService(AgentEventStore(database))
            try:
                await alice.create(
                    owner_id="alice",
                    provider="generic",
                    device_id="local-test",
                    cwd=".",
                    session_id="shared-id",
                )
                with self.assertRaisesRegex(ValueError, "another owner"):
                    await bob.create(
                        owner_id="bob",
                        provider="generic",
                        device_id="local-test",
                        cwd=".",
                        session_id="shared-id",
                    )
                self.assertIsNotNone(await alice.get("alice", "shared-id"))
                self.assertIsNone(await bob.get("bob", "shared-id"))
            finally:
                # Closing on the failure path too keeps a failing assertion from
                # stranding WAL files and turning one failure into two.
                await alice.close()
                await bob.close()


if __name__ == "__main__":
    unittest.main()
