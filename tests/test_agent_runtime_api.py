from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_runtime.api import build_agent_router
from app.agent_runtime.service import AgentSessionService
from app.agent_runtime.store import AgentEventStore


class AgentRuntimeApiTests(unittest.TestCase):
    def test_create_snapshot_turn_and_owner_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AgentSessionService(AgentEventStore(Path(directory) / "agent.db"))
            app = FastAPI()
            app.state.agent_sessions = service
            app.include_router(build_agent_router(lambda: "alice"))
            with TestClient(app) as client:
                created = client.post("/api/agent/sessions", json={"provider": "generic", "cwd": "."})
                self.assertEqual(created.status_code, 201, created.text)
                session_id = created.json()["session"]["id"]
                snapshot = client.get(f"/api/agent/sessions/{session_id}")
                self.assertEqual(snapshot.status_code, 200)
                self.assertEqual(snapshot.json()["session"]["session_kind"], "agent")
                turn = client.post(f"/api/agent/sessions/{session_id}/turns", json={"input": "hello"})
                self.assertEqual(turn.status_code, 202, turn.text)
                events = client.get(f"/api/agent/sessions/{session_id}/events")
                self.assertEqual(events.status_code, 200)
                self.assertTrue(events.json()["events"])


if __name__ == "__main__":
    unittest.main()
