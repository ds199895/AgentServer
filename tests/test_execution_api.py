from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.execution import (
    EventEnvelope,
    EventScope,
    EntityKind,
    ExecutionStore,
    LeaseConflict,
    ProducerMode,
    ProducerRef,
)
from app.execution.api import build_execution_router
from app.execution.security import ReporterTokenRegistry, ReporterTokenSigner
from app.execution.service import ExecutionService


class ExecutionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = Path(self.directory.name) / "execution.db"
        self.store = ExecutionStore(database)
        self.tokens = ReporterTokenRegistry(database, ReporterTokenSigner(b"a" * 32))
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
        self.command_tokens: dict[str, str] = {}
        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-1",
            launch_id="launch-1",
            device_id="device-1",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-1")

    def tearDown(self) -> None:
        self.client.close()
        self.directory.cleanup()

    def create_assignment(self) -> tuple[dict[str, object], dict[str, object], str]:
        task_response = self.client.post(
            "/api/tasks", json={"title": "Implement runtime API"}
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
        run = assignment["runs"][0]
        bridge_response = self.client.post(f"/api/runs/{run['id']}/bridge-tokens")
        self.assertEqual(201, bridge_response.status_code, bridge_response.text)
        self.command_tokens[str(run["id"])] = bridge_response.json()["command_token"]
        token_response = self.client.post(f"/api/runs/{run['id']}/reporter-token")
        self.assertEqual(201, token_response.status_code, token_response.text)
        return task, run, token_response.json()["token"]

    @staticmethod
    def reporter_event(
        run: dict[str, object],
        *,
        event_type: str = "agent.registered",
        seq: int = 1,
        expected_revision: int | None = 0,
        owner_id: str = "alice",
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        attributes = run["attributes"]
        assert isinstance(attributes, dict)
        return EventEnvelope(
            type=event_type,
            scope=EventScope(
                owner_id=owner_id,
                device_id="device-1",
                terminal_id="terminal-1",
                launch_id="launch-1",
                agent_instance_id=str(attributes["agent_instance_id"]),
                task_id=str(attributes["task_id"]),
                assignment_id=str(attributes["assignment_id"]),
                run_id=str(run["id"]),
            ),
            producer=ProducerRef(
                id="agent:kimi",
                epoch="boot-1",
                seq=seq,
                adapter="kimi",
                mode=ProducerMode.ACTIVE,
            ),
            expected_revision=expected_revision,
            payload=payload or {},
        ).as_dict()

    def test_management_endpoints_are_idempotent_and_owner_scoped(self) -> None:
        first = self.client.post(
            "/api/tasks",
            headers={"Idempotency-Key": "request-1"},
            json={"title": "One task"},
        )
        second = self.client.post(
            "/api/tasks",
            headers={"Idempotency-Key": "request-1"},
            json={"title": "One task"},
        )
        self.assertEqual(201, first.status_code)
        self.assertEqual(first.json()["id"], second.json()["id"])
        snapshot = self.client.get("/api/execution/snapshot").json()
        self.assertEqual("agentserver.execution-snapshot/1", snapshot["schema"])
        self.assertEqual(1, len(snapshot["tasks"]))

    def test_create_terminal_assignment_preflights_before_external_launch(self) -> None:
        assigned_task, _run, _token = self.create_assignment()
        launches = 0

        async def launch(**_kwargs: object) -> dict[str, object]:
            nonlocal launches
            launches += 1
            return {"id": "should-not-launch", "launch_id": "unused"}

        self.app.state.create_managed_terminal = launch
        response = self.client.post(
            f"/api/tasks/{assigned_task['id']}/assignments",
            json={
                "expected_task_revision": assigned_task["revision"],
                "agent_kind": "kimi",
                "target": {
                    "device_id": "device-2",
                    "create_terminal": {},
                },
            },
        )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(0, launches)

    def test_failed_assignment_compensates_exact_created_terminal_launch(self) -> None:
        task = self.client.post(
            "/api/tasks", json={"title": "Needs a new terminal"}
        ).json()
        deleted: list[str] = []

        async def launch(**_kwargs: object) -> dict[str, object]:
            self.service.register_terminal(
                owner_id="alice",
                terminal_id="created-terminal",
                launch_id="created-launch",
                device_id="device-2",
            )
            self.service.terminal_ready(
                owner_id="alice", terminal_id="created-terminal"
            )
            return {"id": "created-terminal", "launch_id": "created-launch"}

        class FakeTerminalManager:
            def __init__(self) -> None:
                self.session = SimpleNamespace(
                    id="created-terminal",
                    owner="alice",
                    managed=True,
                    launch_id="created-launch",
                )

            def get_for_owner(self, terminal_id: str, owner_id: str):
                if terminal_id == self.session.id and owner_id == self.session.owner:
                    return self.session
                return None

            async def delete(self, terminal_id: str) -> bool:
                deleted.append(terminal_id)
                return terminal_id == self.session.id

        def fail_assignment(**_kwargs: object) -> dict[str, object]:
            raise LeaseConflict("injected assignment race")

        self.app.state.create_managed_terminal = launch
        self.app.state.terminals = FakeTerminalManager()
        original = self.service.assign_task
        self.service.assign_task = fail_assignment  # type: ignore[method-assign]
        try:
            response = self.client.post(
                f"/api/tasks/{task['id']}/assignments",
                json={
                    "expected_task_revision": task["revision"],
                    "agent_kind": "kimi",
                    "target": {
                        "device_id": "device-2",
                        "create_terminal": {},
                    },
                },
            )
        finally:
            self.service.assign_task = original  # type: ignore[method-assign]
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(["created-terminal"], deleted)
        current = self.service.get_task(
            owner_id="alice", task_id=str(task["id"])
        )["task"]
        self.assertEqual("submitted", current["state"]["lifecycle"])

    def test_runtime_context_batch_replay_and_heartbeat(self) -> None:
        _task, run, token = self.create_assignment()
        headers = {"Authorization": f"Bearer {token}"}
        context = self.client.get("/api/runtime/v1/context", headers=headers)
        self.assertEqual(200, context.status_code, context.text)
        context_body = context.json()
        self.assertEqual(run["id"], context_body["active_run_id"])
        self.assertIsInstance(context_body["server_time"], float)
        self.assertGreaterEqual(context_body["terminal_lease"]["revision"], 1)
        self.assertGreater(
            context_body["terminal_lease"]["expires_at"],
            context_body["server_time"],
        )
        self.assertIn("ETag", context.headers)

        event = self.reporter_event(run)
        accepted = self.client.post(
            "/api/runtime/v1/events:batch",
            headers=headers,
            json={"events": [event]},
        )
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual("accepted", accepted.json()["results"][0]["status"])
        replay = self.client.post(
            "/api/runtime/v1/events:batch",
            headers=headers,
            json={"events": [event]},
        )
        self.assertEqual("duplicate", replay.json()["results"][0]["status"])
        heartbeat = self.client.post(
            "/api/runtime/v1/heartbeat", headers=headers, json={}
        )
        self.assertEqual(200, heartbeat.status_code, heartbeat.text)
        self.assertEqual("active", heartbeat.json()["lease"]["status"])

    def test_runtime_tokens_refresh_with_overlap_while_assignment_is_active(self) -> None:
        _task, run, report_token = self.create_assignment()
        command_token = self.command_tokens[str(run["id"])]

        for token, capability in (
            (report_token, "report"),
            (command_token, "commands"),
        ):
            claims = self.tokens.verify(token)
            original_clock = self.service.clock
            self.service.clock = lambda: claims.expires_at - 1
            try:
                response = self.client.post(
                    "/api/runtime/v1/token:refresh",
                    headers={"Authorization": f"Bearer {token}"},
                )
            finally:
                self.service.clock = original_clock
            self.assertEqual(200, response.status_code, response.text)
            replacement = response.json()["token"]
            self.assertNotEqual(token, replacement)
            self.tokens.verify(
                replacement,
                capability=capability,
                now=claims.expires_at - 1,
            )
            # The overlap is intentional: a crash before the atomic token-file
            # swap must not strand a Bridge.
            self.tokens.verify(token, capability=capability)
            self.assertEqual("no-store", response.headers["cache-control"])

            self.service.clock = lambda: claims.expires_at - 1
            try:
                replay = self.client.post(
                    "/api/runtime/v1/token:refresh",
                    headers={"Authorization": f"Bearer {token}"},
                )
            finally:
                self.service.clock = original_clock
            self.assertEqual(replacement, replay.json()["token"])

    def test_terminal_result_revokes_commands_but_keeps_exact_report_replay(self) -> None:
        _task, run, report_token = self.create_assignment()
        report_headers = {"Authorization": f"Bearer {report_token}"}
        command_headers = {
            "Authorization": f"Bearer {self.command_tokens[str(run['id'])]}"
        }
        registered = self.reporter_event(run)
        response = self.client.post(
            "/api/runtime/v1/events:batch",
            headers=report_headers,
            json={"events": [registered]},
        )
        self.assertEqual(200, response.status_code, response.text)
        current = self.service.projection(
            owner_id="alice", kind=EntityKind.RUN, entity_id=str(run["id"])
        )
        assert current is not None
        finished = self.reporter_event(
            run,
            event_type="run.succeeded",
            seq=2,
            expected_revision=current.revision,
        )
        response = self.client.post(
            "/api/runtime/v1/events:batch",
            headers=report_headers,
            json={"events": [finished]},
        )
        self.assertEqual("accepted", response.json()["results"][0]["status"])

        self.assertEqual(
            401,
            self.client.get(
                "/api/runtime/v1/commands", headers=command_headers
            ).status_code,
        )
        self.assertEqual(
            409,
            self.client.post(
                "/api/runtime/v1/token:refresh", headers=report_headers
            ).status_code,
        )
        replay = self.client.post(
            "/api/runtime/v1/events:batch",
            headers=report_headers,
            json={"events": [finished]},
        )
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertEqual("duplicate", replay.json()["results"][0]["status"])

    def test_cross_scope_batch_is_rejected_before_any_event_is_written(self) -> None:
        _task, run, token = self.create_assignment()
        before = self.store.snapshot(owner_id="alice").as_of_sequence
        event = self.reporter_event(run, owner_id="bob")
        response = self.client.post(
            "/api/runtime/v1/events:batch",
            headers={"Authorization": f"Bearer {token}"},
            json={"events": [event]},
        )
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual(before, self.store.snapshot(owner_id="alice").as_of_sequence)

    def test_runtime_batch_rejects_non_object_shapes_and_oversized_bodies(self) -> None:
        _task, _run, token = self.create_assignment()
        headers = {"Authorization": f"Bearer {token}"}
        for body in ([], {"events": [1]}):
            response = self.client.post(
                "/api/runtime/v1/events:batch", headers=headers, json=body
            )
            self.assertEqual(422, response.status_code, response.text)
        oversized = b'{"events":[{"payload":"' + b"x" * (257 * 1024) + b'"}]}'
        response = self.client.post(
            "/api/runtime/v1/events:batch",
            headers={**headers, "Content-Type": "application/json"},
            content=oversized,
        )
        self.assertEqual(413, response.status_code, response.text)

    def test_permanent_runtime_rejection_consumes_sequence_with_audit_tombstone(self) -> None:
        _task, run, token = self.create_assignment()
        event = self.reporter_event(
            run,
            event_type="run.activity.changed",
            expected_revision=int(run["revision"]),
            payload={"activity": "not-a-phase"},
        )
        response = self.client.post(
            "/api/runtime/v1/events:batch",
            headers={"Authorization": f"Bearer {token}"},
            json={"events": [event]},
        )
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("rejected", body["results"][0]["status"])
        self.assertFalse(body["results"][0]["retryable"])
        self.assertEqual(1, body["accepted_through_seq"])
        events = self.store.snapshot(owner_id="alice").events
        self.assertEqual("runtime.event.rejected", events[-1].type)

    def test_native_report_token_cannot_claim_adapter_authority(self) -> None:
        _task, run, _bridge_token = self.create_assignment()
        token_response = self.client.post(
            f"/api/runs/{run['id']}/reporter-token"
        )
        token = token_response.json()["token"]
        event = self.reporter_event(run, expected_revision=None)
        event["producer"] = {**event["producer"], "mode": "adapter"}
        response = self.client.post(
            "/api/runtime/v1/events:batch",
            headers={"Authorization": f"Bearer {token}"},
            json={"events": [event]},
        )
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()["results"][0]
        self.assertEqual("rejected", result["status"])
        self.assertIn("producer authority", result["message"])

    def test_cancel_command_is_delivered_and_cannot_be_acked_by_another_agent(self) -> None:
        _task, run, token = self.create_assignment()
        headers = {"Authorization": f"Bearer {token}"}
        command_headers = {
            "Authorization": f"Bearer {self.command_tokens[str(run['id'])]}"
        }
        self.client.post(
            "/api/runtime/v1/events:batch",
            headers=headers,
            json={"events": [self.reporter_event(run)]},
        )
        cancel = self.client.post(f"/api/runs/{run['id']}/cancel")
        self.assertEqual(202, cancel.status_code, cancel.text)
        command_id = cancel.json()["command"]["command_id"]
        report_token_cannot_control = self.client.get(
            "/api/runtime/v1/commands", headers=headers
        )
        self.assertEqual(401, report_token_cannot_control.status_code)
        commands = self.client.get(
            "/api/runtime/v1/commands", headers=command_headers
        )
        command = commands.json()["commands"][0]
        self.assertEqual(command_id, command["command_id"])
        self.assertIsNotNone(command["expires_at"])
        self.assertEqual(run["id"], command["payload"]["run_id"])
        self.assertEqual(
            run["attributes"]["assignment_id"],
            command["payload"]["assignment_id"],
        )
        self.assertEqual("terminal-1", command["payload"]["terminal_id"])
        self.assertEqual("launch-1", command["payload"]["launch_id"])
        self.assertIn("terminal_lease_id", command["payload"])
        self.assertIn("terminal_lease_revision", command["payload"])
        ack = self.client.post(
            f"/api/runtime/v1/commands/{command_id}/ack",
            headers=command_headers,
            json={"status": "accepted"},
        )
        self.assertEqual(200, ack.status_code, ack.text)
        restarted_bridge = self.client.get(
            "/api/runtime/v1/commands?after_sequence=0", headers=command_headers
        )
        self.assertEqual(200, restarted_bridge.status_code, restarted_bridge.text)
        self.assertEqual(command_id, restarted_bridge.json()["commands"][0]["command_id"])
        self.assertEqual(
            "accepted", restarted_bridge.json()["commands"][0]["status"]
        )

    def test_old_command_token_is_fenced_after_terminal_lease_is_lost(self) -> None:
        _task, run, report_token = self.create_assignment()
        self.client.post(
            "/api/runtime/v1/events:batch",
            headers={"Authorization": f"Bearer {report_token}"},
            json={"events": [self.reporter_event(run)]},
        )
        cancel = self.client.post(f"/api/runs/{run['id']}/cancel")
        command_id = cancel.json()["command"]["command_id"]
        command_headers = {
            "Authorization": f"Bearer {self.command_tokens[str(run['id'])]}"
        }
        attributes = run["attributes"]
        assert isinstance(attributes, dict)
        lease = self.store.get_lease(
            owner_id="alice",
            resource_kind=EntityKind.TERMINAL,
            resource_id="terminal-1",
        )
        assert lease is not None
        self.store.release_lease(
            owner_id="alice",
            lease_id=lease.id,
            holder_id=str(attributes["assignment_id"]),
            expected_revision=lease.revision,
        )

        blocked_get = self.client.get(
            "/api/runtime/v1/commands", headers=command_headers
        )
        self.assertEqual(409, blocked_get.status_code, blocked_get.text)
        blocked_ack = self.client.post(
            f"/api/runtime/v1/commands/{command_id}/ack",
            headers=command_headers,
            json={"status": "accepted", "ack_id": "late-ack"},
        )
        self.assertEqual(409, blocked_ack.status_code, blocked_ack.text)

    def test_command_ack_body_is_bounded_and_rejects_non_finite_numbers(self) -> None:
        _task, run, report_token = self.create_assignment()
        self.client.post(
            "/api/runtime/v1/events:batch",
            headers={"Authorization": f"Bearer {report_token}"},
            json={"events": [self.reporter_event(run)]},
        )
        command = self.client.post(f"/api/runs/{run['id']}/cancel").json()[
            "command"
        ]
        headers = {
            "Authorization": f"Bearer {self.command_tokens[str(run['id'])]}",
            "Content-Type": "application/json",
        }
        oversized = json.dumps(
            {"status": "accepted", "payload": {"output": "x" * (64 * 1024)}}
        ).encode("utf-8")
        response = self.client.post(
            f"/api/runtime/v1/commands/{command['command_id']}/ack",
            headers=headers,
            content=oversized,
        )
        self.assertEqual(413, response.status_code, response.text)

        response = self.client.post(
            f"/api/runtime/v1/commands/{command['command_id']}/ack",
            headers=headers,
            content=b'{"status":"accepted","payload":{"progress":NaN}}',
        )
        self.assertEqual(422, response.status_code, response.text)

    def test_browser_websocket_replays_from_cursor_and_rejects_bad_cookie(self) -> None:
        task = self.client.post("/api/tasks", json={"title": "Stream me"}).json()
        with self.client.websocket_connect(
            "/ws/execution?after_sequence=0", cookies={"session": "valid-session"}
        ) as socket:
            message = socket.receive_json()
            self.assertEqual("event", message["type"])
            self.assertGreaterEqual(message["cursor"], 1)
            self.assertIn("tasks", message["projection"])
        with self.client.websocket_connect("/ws/execution") as socket:
            with self.assertRaises(WebSocketDisconnect) as closed:
                socket.receive_json()
            self.assertEqual(4401, closed.exception.code)


if __name__ == "__main__":
    unittest.main()
