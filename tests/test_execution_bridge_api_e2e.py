from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.execution import ExecutionStore
from app.execution.api import build_execution_router
from app.execution.bridge import AgentBridge
from app.execution.reporter import ReporterContext, ReporterSpool, RuntimeReporter
from app.execution.security import ReporterTokenRegistry, ReporterTokenSigner
from app.execution.service import ExecutionService


class BridgeExecutionApiE2ETests(unittest.IsolatedAsyncioTestCase):
    """Exercise the device Bridge protocol against the real execution router.

    The bridge's HTTP reporter uses a synchronous ``httpx.Client`` while its
    local protocol is asynchronous.  TestClient's in-process transport lets us
    preserve that production boundary without opening a network listener.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        database = root / "execution.db"
        self.store = ExecutionStore(database)
        self.tokens = ReporterTokenRegistry(database, ReporterTokenSigner(b"e" * 32))
        self.service = ExecutionService(self.store, reporter_tokens=self.tokens)
        self.app = FastAPI()

        def browser_user() -> str:
            return "alice"

        self.app.include_router(
            build_execution_router(
                browser_user,
                lambda value: "alice" if value == "valid-session" else None,
                cookie_name="session",
            )
        )
        self.app.state.execution = self.service
        self.app.state.reporter_tokens = self.tokens
        self.client = TestClient(self.app)

        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-1",
            launch_id="launch-1",
            device_id="device-1",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-1")
        task_response = self.client.post(
            "/api/tasks", json={"title": "Bridge integration run"}
        )
        self.assertEqual(201, task_response.status_code, task_response.text)
        task = task_response.json()
        assignment_response = self.client.post(
            f"/api/tasks/{task['id']}/assignments",
            json={
                "expected_task_revision": task["revision"],
                "agent_kind": "kimi",
                "target": {"terminal_id": "terminal-1"},
            },
        )
        self.assertEqual(201, assignment_response.status_code, assignment_response.text)
        assignment = assignment_response.json()
        self.run = assignment["runs"][0]
        self.run_id = str(self.run["id"])
        bridge_tokens = self.client.post(
            f"/api/runs/{self.run_id}/bridge-tokens"
        )
        self.assertEqual(201, bridge_tokens.status_code, bridge_tokens.text)
        bridge_payload = bridge_tokens.json()
        self.adapter_token = str(bridge_payload["report_token"])
        active_token_response = self.client.post(
            f"/api/runs/{self.run_id}/reporter-token"
        )
        self.assertEqual(201, active_token_response.status_code, active_token_response.text)
        self.active_token = str(active_token_response.json()["token"])

        attributes = self.run["attributes"]
        assert isinstance(attributes, dict)
        self.context = ReporterContext(
            owner_id="alice",
            device_id="device-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            run_id=self.run_id,
            assignment_id=str(attributes["assignment_id"]),
            task_id=str(attributes["task_id"]),
            agent_instance_id=str(attributes["agent_instance_id"]),
        )
        self.spool = ReporterSpool(root / "bridge-spool.db")
        self.reporter = RuntimeReporter(
            self.context,
            self.spool,
            producer_id="bridge:device-1",
            adapter="kimi",
            mode="adapter",
        )
        self.bridge = AgentBridge(
            self.reporter,
            address=str(root / "bridge.sock"),
            base_url="http://127.0.0.1",
            reporter_token=self.adapter_token,
            launch_root_pid=__import__("os").getpid(),
            context_provider=lambda: {"managed": True},
            heartbeat_interval=3600,
            command_interval=3600,
        )

    async def asyncTearDown(self) -> None:
        await self.bridge.close()

    def tearDown(self) -> None:
        self.client.close()
        self.directory.cleanup()

    async def bridge_request(self, payload: dict[str, object]) -> dict[str, Any]:
        return await self.bridge._handle_request(payload)

    def post_event(self, event: dict[str, Any], token: str) -> httpx.Response:
        return self.client.post(
            "/api/runtime/v1/events:batch",
            headers={"Authorization": f"Bearer {token}"},
            json={"events": [event]},
        )

    def test_adapter_bridge_event_flushes_to_real_api_and_projects_run(self) -> None:
        # The same local Bridge request is what device adapters use to enqueue
        # facts; no direct service.ingest call is involved in this path.
        queued = asyncio.run(
            self.bridge_request(
                {
                    "action": "event",
                    "event_type": "run.activity.changed",
                    "payload": {"activity": "coding", "summary": "editing"},
                }
            )
        )
        self.assertTrue(queued["ok"])
        self.assertEqual(1, queued["queued"])

        result = self.reporter.flush(
            "http://agentserver.test",
            self.adapter_token,
            transport=self.client._transport,  # type: ignore[attr-defined]
        )
        self.assertEqual("accepted", result["results"][0]["status"])
        self.assertEqual(1, result["accepted_through_seq"])
        self.assertEqual(0, len(self.spool))

        run_response = self.client.get(f"/api/runs/{self.run_id}")
        self.assertEqual(200, run_response.status_code, run_response.text)
        state = run_response.json()["state"]
        self.assertEqual("running", state["lifecycle"])
        self.assertEqual("coding", state["activity"])
        self.assertEqual("editing", state["summary"])

    def test_lost_batch_response_replays_exact_event_and_clears_spool(self) -> None:
        queued = asyncio.run(
            self.bridge_request(
                {
                    "action": "event",
                    "event_type": "run.activity.changed",
                    "payload": {"activity": "coding"},
                }
            )
        )
        self.assertTrue(queued["ok"])
        event_id = str(queued["event"]["event_id"])
        original_flush = self.reporter.flush
        transport = self.client._transport  # type: ignore[attr-defined]

        def response_lost(
            base_url: str,
            token: str,
            *,
            limit: int = 100,
            timeout: float = 10.0,
            transport: httpx.BaseTransport | None = None,
        ) -> dict[str, Any]:
            # Deliver to FastAPI but deliberately stop before acknowledge(),
            # exactly as if the response were lost after the server committed.
            events = self.spool.delivery_batch(limit=limit)
            with httpx.Client(transport=transport, timeout=timeout, trust_env=False) as client:
                response = client.post(
                    f"{base_url.rstrip('/')}/api/runtime/v1/events:batch",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"events": events},
                )
                response.raise_for_status()
            raise httpx.ReadError("simulated lost response")

        self.reporter.flush = response_lost  # type: ignore[method-assign]
        with self.assertRaises(httpx.ReadError):
            self.reporter.flush(
                "http://agentserver.test",
                self.adapter_token,
                transport=transport,
            )
        self.assertEqual(1, len(self.spool))

        self.reporter.flush = original_flush  # type: ignore[method-assign]
        replay = self.reporter.flush(
            "http://agentserver.test",
            self.adapter_token,
            transport=transport,
        )
        self.assertEqual(event_id, replay["results"][0]["event_id"])
        self.assertEqual("duplicate", replay["results"][0]["status"])
        self.assertEqual(0, len(self.spool))

    def test_active_cas_and_adapter_authority_are_enforced_at_api_boundary(self) -> None:
        adapter_event = self.reporter.emit(
            "run.activity.changed", {"activity": "coding"}
        )
        # Establish a running Run through the adapter token so the ACTIVE CAS
        # check below exercises an already-advanced aggregate revision.
        accepted = self.post_event(adapter_event, self.adapter_token)
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual("accepted", accepted.json()["results"][0]["status"])
        self.spool.acknowledge(1)

        active_reporter = RuntimeReporter(
            self.context,
            ReporterSpool(Path(self.directory.name) / "active-spool.db"),
            producer_id="active:agent-1",
            adapter="kimi",
            mode="active",
        )
        stale_cas = active_reporter.emit(
            "run.activity.changed",
            {"activity": "thinking"},
            expected_revision=0,
        )
        stale_response = self.post_event(stale_cas, self.active_token)
        self.assertEqual(200, stale_response.status_code, stale_response.text)
        self.assertEqual(
            "revision_conflict", stale_response.json()["results"][0]["code"]
        )

        wrong_authority = active_reporter.emit(
            "run.activity.changed",
            {"activity": "thinking"},
            expected_revision=self.service.projection(
                owner_id="alice", kind="run", entity_id=self.run_id
            ).revision,  # type: ignore[union-attr]
        )
        wrong_response = self.post_event(wrong_authority, self.adapter_token)
        self.assertEqual(200, wrong_response.status_code, wrong_response.text)
        self.assertEqual("rejected", wrong_response.json()["results"][0]["status"])
        self.assertEqual("ValidationError", wrong_response.json()["results"][0]["code"])


if __name__ == "__main__":
    unittest.main()
