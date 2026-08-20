from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI

from app.execution.device_runtime import DeviceRuntimeService, DeviceRuntimeStore
from app.execution.device_runtime_api import build_device_runtime_router
from app.execution.store import ExecutionStore


class DeviceRuntimeAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database = Path(self.temporary.name) / "agentserver.db"
        self.devices = {("alice", "device-1")}
        self.execution_store = ExecutionStore(database)
        self.runtime_store = DeviceRuntimeStore(database)
        self.service = DeviceRuntimeService(
            self.runtime_store,
            self.execution_store,
            device_exists=lambda owner_id, device_id: (
                owner_id,
                device_id,
            )
            in self.devices,
        )
        application = FastAPI()
        application.state.device_runtime = self.service

        async def current_user() -> str:
            return "alice"

        application.include_router(build_device_runtime_router(current_user))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="https://agentserver.test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    async def enroll(self) -> tuple[str, str]:
        issued = await self.client.post(
            "/api/devices/device-1/runtime/enrollment-tokens",
            json={},
        )
        self.assertEqual(201, issued.status_code, issued.text)
        enrollment_token = issued.json()["enrollment_token"]
        enrolled = await self.client.post(
            "/api/device-runtime/v1/enroll",
            json={
                "device_id": "device-1",
                "enrollment_token": enrollment_token,
            },
        )
        self.assertEqual(201, enrolled.status_code, enrolled.text)
        return enrollment_token, enrolled.json()["credential"]

    async def heartbeat(
        self,
        credential: str,
        *,
        runtime_session_id: str = "host-session-1",
        generation: int = 1,
    ) -> httpx.Response:
        return await self.client.post(
            "/api/device-runtime/v1/heartbeat",
            headers={"Authorization": f"Bearer {credential}"},
            json={
                "instance_id": "stable-instance-1",
                "boot_id": f"boot-{generation}",
                "runtime_session_id": runtime_session_id,
                "generation": generation,
                "protocol_version": 1,
                "runtime_version": "test",
                "health": "healthy",
                "capabilities": {"providers": [], "features": []},
                "platform": {
                    "os": "linux",
                    "arch": "x86_64",
                    "hostname": "device-1",
                },
            },
        )

    @staticmethod
    def runtime_event(
        session_id: str,
        event_id: str,
        producer_seq: int,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        device_id: str = "device-1",
        runtime_session_id: str = "host-session-1",
        generation: int = 1,
    ) -> dict[str, object]:
        return {
            "schema": "agentserver.device-runtime-event/1",
            "event_id": event_id,
            "type": event_type,
            "device_id": device_id,
            "runtime_session_id": runtime_session_id,
            "generation": generation,
            "session_id": session_id,
            "producer": {"epoch": "api-test-epoch", "seq": producer_seq},
            "occurred_at": 1_000.0,
            "payload": dict(payload or {}),
        }

    async def post_events(
        self,
        credential: str,
        events: list[dict[str, object]],
        *,
        runtime_session_id: str = "host-session-1",
        generation: int = 1,
    ) -> httpx.Response:
        return await self.client.post(
            "/api/device-runtime/v1/events:batch",
            headers={"Authorization": f"Bearer {credential}"},
            json={
                "runtime_session_id": runtime_session_id,
                "generation": generation,
                "events": events,
            },
        )

    async def test_secret_responses_are_no_store_and_enrollment_replays(self) -> None:
        issued = await self.client.post(
            "/api/devices/device-1/runtime/enrollment-tokens",
            json={},
        )
        self.assertEqual("no-store", issued.headers.get("cache-control"))
        self.assertEqual("no-cache", issued.headers.get("pragma"))
        enrollment_token = issued.json()["enrollment_token"]
        request = {
            "device_id": "device-1",
            "enrollment_token": enrollment_token,
        }
        first = await self.client.post("/api/device-runtime/v1/enroll", json=request)
        replay = await self.client.post("/api/device-runtime/v1/enroll", json=request)
        self.assertEqual(201, first.status_code, first.text)
        self.assertEqual(201, replay.status_code, replay.text)
        self.assertEqual(first.json()["credential"], replay.json()["credential"])
        self.assertEqual("no-store", replay.headers.get("cache-control"))
        database_bytes = self.runtime_store.database_path.read_bytes()
        self.assertNotIn(first.json()["credential"].encode(), database_bytes)
        self.assertNotIn(enrollment_token.encode(), database_bytes)

    async def test_rotation_replay_uses_old_bearer_and_same_fence(self) -> None:
        _enrollment, credential = await self.enroll()
        heartbeat = await self.heartbeat(credential)
        self.assertEqual(200, heartbeat.status_code, heartbeat.text)
        self.assertEqual("stable-instance-1", heartbeat.json()["runtime"]["instance_id"])
        self.assertEqual("boot-1", heartbeat.json()["runtime"]["boot_id"])
        self.assertGreater(
            heartbeat.json()["credential_expires_at"],
            heartbeat.json()["server_time"],
        )
        body = {
            "runtime_session_id": "host-session-1",
            "generation": 1,
            "request_id": "stable-api-rotation",
        }
        headers = {"Authorization": f"Bearer {credential}"}
        first = await self.client.post(
            "/api/device-runtime/v1/credential:rotate",
            headers=headers,
            json=body,
        )
        replay = await self.client.post(
            "/api/device-runtime/v1/credential:rotate",
            headers=headers,
            json=body,
        )
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(200, replay.status_code, replay.text)
        replacement = first.json()["credential"]
        self.assertEqual(replacement, replay.json()["credential"])
        self.assertEqual("no-store", replay.headers.get("cache-control"))
        rejected = await self.client.post(
            "/api/device-runtime/v1/credential:rotate",
            headers=headers,
            json={**body, "request_id": "different-api-rotation"},
        )
        self.assertEqual(401, rejected.status_code, rejected.text)
        self.assertEqual(200, (await self.heartbeat(replacement)).status_code)

    async def test_int64_bounds_and_request_body_limit_fail_closed(self) -> None:
        _enrollment, credential = await self.enroll()
        oversized_generation = await self.heartbeat(
            credential,
            generation=2**63,
        )
        self.assertEqual(422, oversized_generation.status_code)
        oversized_body = await self.client.post(
            "/api/device-runtime/v1/heartbeat",
            content=b'{"padding":"' + b"x" * (65 * 1024) + b'"}',
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(413, oversized_body.status_code)

    async def test_unknown_inventory_device_is_owner_scoped(self) -> None:
        response = await self.client.post(
            "/api/devices/not-owned/runtime/enrollment-tokens",
            json={},
        )
        self.assertEqual(404, response.status_code)

    async def test_browser_session_create_replays_a_client_session_id(self) -> None:
        _enrollment, credential = await self.enroll()
        heartbeat = await self.heartbeat(credential)
        self.assertEqual(200, heartbeat.status_code, heartbeat.text)
        body = {
            "session_id": "stable-browser-session",
            "provider": "codex",
            "cwd": "/workspace",
            "permission_mode": "workspace-write",
        }
        first = await self.client.post(
            "/api/devices/device-1/runtime/sessions", json=body
        )
        replay = await self.client.post(
            "/api/devices/device-1/runtime/sessions", json=body
        )
        self.assertEqual(201, first.status_code, first.text)
        self.assertEqual(201, replay.status_code, replay.text)
        self.assertEqual("stable-browser-session", first.json()["session"]["id"])
        self.assertEqual(first.json()["session"], replay.json()["session"])
        self.assertEqual(first.json()["command"], replay.json()["command"])

    async def test_device_command_ack_requires_a_stable_ack_id(self) -> None:
        _enrollment, credential = await self.enroll()
        heartbeat = await self.heartbeat(credential)
        self.assertEqual(200, heartbeat.status_code, heartbeat.text)
        queued = await self.client.post("/api/devices/device-1/runtime/probe", json={})
        self.assertEqual(202, queued.status_code, queued.text)
        command_id = queued.json()["command"]["command_id"]
        headers = {"Authorization": f"Bearer {credential}"}
        body = {
            "runtime_session_id": "host-session-1",
            "generation": 1,
            "status": "completed",
            "payload": {},
        }
        missing = await self.client.post(
            f"/api/device-runtime/v1/commands/{command_id}/ack",
            headers=headers,
            json=body,
        )
        self.assertEqual(422, missing.status_code, missing.text)
        accepted = await self.client.post(
            f"/api/device-runtime/v1/commands/{command_id}/ack",
            headers=headers,
            json={**body, "ack_id": "stable-device-ack"},
        )
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual("completed", accepted.json()["command"]["status"])

    async def test_event_batch_mixes_success_duplicate_and_permanent_rejections(
        self,
    ) -> None:
        _enrollment, credential = await self.enroll()
        heartbeat = await self.heartbeat(credential)
        self.assertEqual(200, heartbeat.status_code, heartbeat.text)
        claims = self.service.authenticate(credential)

        for session_id in (
            "accepted-session",
            "duplicate-session",
            "terminal-session",
            "invalid-session",
            "identity-session",
        ):
            self.service.create_session(
                owner_id="alice",
                device_id="device-1",
                provider="codex",
                workspace="/workspace",
                session_id=session_id,
            )

        duplicate_event = {
            "event_id": "duplicate-event",
            "producer_seq": 1,
            "type": "session.started",
            "payload": {},
            "occurred_at": 1_000.0,
        }
        self.service.ingest_session_events(
            claims,
            runtime_session_id="host-session-1",
            generation=1,
            session_id="duplicate-session",
            events=[duplicate_event],
        )
        self.service.ingest_session_events(
            claims,
            runtime_session_id="host-session-1",
            generation=1,
            session_id="terminal-session",
            events=[
                {
                    "event_id": "terminal-original",
                    "producer_seq": 1,
                    "type": "session.failed",
                    "payload": {"error": "provider_crashed"},
                }
            ],
        )
        self.service.ingest_session_events(
            claims,
            runtime_session_id="host-session-1",
            generation=1,
            session_id="identity-session",
            events=[
                {
                    "event_id": "identity-original",
                    "producer_seq": 2,
                    "type": "session.started",
                    "payload": {},
                }
            ],
        )

        events = [
            self.runtime_event(
                "accepted-session",
                "accepted-event",
                10,
                "session.started",
            ),
            self.runtime_event(
                "duplicate-session",
                "duplicate-event",
                1,
                "session.started",
            ),
            self.runtime_event(
                "terminal-session",
                "terminal-rejected-event",
                3,
                "runtime.warning",
            ),
            self.runtime_event(
                "invalid-session",
                "invalid-transition-event",
                4,
                "turn.completed",
                {"turn_id": "turn-without-reservation"},
            ),
            self.runtime_event(
                "identity-session",
                "identity-reused-position",
                2,
                "runtime.warning",
            ),
            self.runtime_event(
                "missing-session",
                "missing-session-event",
                5,
                "runtime.warning",
            ),
        ]
        response = await self.post_events(credential, events)
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual(10, body["accepted_through_seq"])
        self.assertEqual([], body["missing_ranges"])
        results = {item["event_id"]: item for item in body["results"]}
        self.assertEqual({event["event_id"] for event in events}, set(results))

        for event_id, status in (
            ("accepted-event", "accepted"),
            ("duplicate-event", "duplicate"),
        ):
            result = results[event_id]
            self.assertEqual(status, result["status"])
            self.assertFalse(result["permanent"])
            self.assertEqual("", result["error_code"])
            self.assertEqual("", result["reason"])
            self.assertIsInstance(result["sequence"], int)
            self.assertIsInstance(result["session_revision"], int)

        rejection_codes = {
            "terminal-rejected-event": (
                "session_terminal",
                "server_session_terminal",
            ),
            "invalid-transition-event": (
                "invalid_transition",
                "server_invalid_transition",
            ),
            "identity-reused-position": (
                "identity_conflict",
                "server_event_identity_conflict",
            ),
            "missing-session-event": (
                "session_not_found",
                "server_session_not_found",
            ),
        }
        for event_id, (error_code, reason) in rejection_codes.items():
            result = results[event_id]
            self.assertEqual("rejected", result["status"])
            self.assertTrue(result["permanent"])
            self.assertIsNone(result["sequence"])
            self.assertEqual(error_code, result["error_code"])
            self.assertEqual(reason, result["reason"])
            self.assertIsInstance(result["session_revision"], int)

        self.assertEqual(
            "ready",
            self.service.get_session(
                owner_id="alice", session_id="accepted-session"
            ).lifecycle,
        )
        self.assertEqual(
            "ready",
            self.service.get_session(
                owner_id="alice", session_id="duplicate-session"
            ).lifecycle,
        )
        terminal = self.service.get_session(
            owner_id="alice", session_id="terminal-session"
        )
        self.assertEqual("failed", terminal.lifecycle)
        self.assertEqual("provider_crashed", terminal.last_error)
        for session_id, reason in (
            ("invalid-session", "server_invalid_transition"),
            ("identity-session", "server_event_identity_conflict"),
        ):
            failed = self.service.get_session(
                owner_id="alice", session_id=session_id
            )
            self.assertEqual("failed", failed.lifecycle)
            self.assertEqual(reason, failed.last_error)

        stored_event_ids = {
            event.event_id
            for session_id in (
                "accepted-session",
                "duplicate-session",
                "terminal-session",
                "invalid-session",
                "identity-session",
            )
            for event in self.service.session_events(
                owner_id="alice", session_id=session_id
            )
        }
        self.assertIn("accepted-event", stored_event_ids)
        self.assertIn("duplicate-event", stored_event_ids)
        self.assertTrue(set(rejection_codes).isdisjoint(stored_event_ids))

    async def test_cross_session_event_batch_rolls_back_before_fence_error(
        self,
    ) -> None:
        _enrollment, credential = await self.enroll()
        heartbeat = await self.heartbeat(credential)
        self.assertEqual(200, heartbeat.status_code, heartbeat.text)
        self.service.create_session(
            owner_id="alice",
            device_id="device-1",
            provider="codex",
            workspace="/workspace/stale",
            session_id="stale-fence-session",
        )

        takeover = await self.heartbeat(
            credential,
            runtime_session_id="host-session-2",
            generation=2,
        )
        self.assertEqual(200, takeover.status_code, takeover.text)
        self.service.create_session(
            owner_id="alice",
            device_id="device-1",
            provider="codex",
            workspace="/workspace/current",
            session_id="current-fence-session",
        )
        before = self.service.get_session(
            owner_id="alice", session_id="current-fence-session"
        )
        self.assertEqual("starting", before.lifecycle)
        self.assertEqual(
            [],
            self.service.session_events(
                owner_id="alice", session_id="current-fence-session"
            ),
        )

        response = await self.post_events(
            credential,
            [
                self.runtime_event(
                    "current-fence-session",
                    "would-be-accepted-event",
                    1,
                    "session.started",
                    runtime_session_id="host-session-2",
                    generation=2,
                ),
                self.runtime_event(
                    "stale-fence-session",
                    "stale-fence-event",
                    2,
                    "runtime.warning",
                    runtime_session_id="host-session-2",
                    generation=2,
                ),
            ],
            runtime_session_id="host-session-2",
            generation=2,
        )

        self.assertEqual(409, response.status_code, response.text)
        self.assertNotIn("results", response.json())
        after = self.service.get_session(
            owner_id="alice", session_id="current-fence-session"
        )
        self.assertEqual(before.as_dict(), after.as_dict())
        self.assertEqual(
            [],
            self.service.session_events(
                owner_id="alice", session_id="current-fence-session"
            ),
        )
        self.assertEqual(
            [],
            self.service.session_events(
                owner_id="alice", session_id="stale-fence-session"
            ),
        )

    async def test_event_quota_is_permanent_and_request_auth_fence_stay_http_errors(
        self,
    ) -> None:
        _enrollment, credential = await self.enroll()
        heartbeat = await self.heartbeat(credential)
        self.assertEqual(200, heartbeat.status_code, heartbeat.text)
        self.service.create_session(
            owner_id="alice",
            device_id="device-1",
            provider="codex",
            workspace="/workspace",
            session_id="quota-session",
        )
        quota_event = self.runtime_event(
            "quota-session",
            "quota-rejected-event",
            1,
            "session.started",
        )
        with mock.patch("app.execution.device_runtime.MAX_SESSION_EVENTS", 0):
            quota = await self.post_events(credential, [quota_event])
        self.assertEqual(200, quota.status_code, quota.text)
        [result] = quota.json()["results"]
        self.assertEqual("rejected", result["status"])
        self.assertTrue(result["permanent"])
        self.assertIsNone(result["sequence"])
        self.assertEqual("retention_quota", result["error_code"])
        self.assertEqual("server_retention_quota", result["reason"])
        quota_session = self.service.get_session(
            owner_id="alice", session_id="quota-session"
        )
        self.assertEqual("failed", quota_session.lifecycle)
        self.assertEqual("server_retention_quota", quota_session.last_error)
        self.assertEqual(
            [],
            self.service.session_events(
                owner_id="alice", session_id="quota-session"
            ),
        )

        request_body = {
            "runtime_session_id": "host-session-1",
            "generation": 1,
            "events": [
                self.runtime_event(
                    "does-not-exist",
                    "request-level-event",
                    2,
                    "runtime.warning",
                )
            ],
        }
        missing_auth = await self.client.post(
            "/api/device-runtime/v1/events:batch", json=request_body
        )
        invalid_auth = await self.client.post(
            "/api/device-runtime/v1/events:batch",
            headers={"Authorization": "Bearer invalid-device-credential"},
            json=request_body,
        )
        stale_fence = await self.post_events(
            credential,
            request_body["events"],
            runtime_session_id="stale-host-session",
        )
        wrong_device = await self.post_events(
            credential,
            [
                self.runtime_event(
                    "does-not-exist",
                    "wrong-device-event",
                    3,
                    "runtime.warning",
                    device_id="device-other",
                )
            ],
        )
        stale_event_session = await self.post_events(
            credential,
            [
                self.runtime_event(
                    "does-not-exist",
                    "stale-event-runtime-session",
                    4,
                    "runtime.warning",
                    runtime_session_id="stale-host-session",
                )
            ],
        )
        stale_event_generation = await self.post_events(
            credential,
            [
                self.runtime_event(
                    "does-not-exist",
                    "stale-event-generation",
                    5,
                    "runtime.warning",
                    generation=2,
                )
            ],
        )
        self.assertEqual(401, missing_auth.status_code, missing_auth.text)
        self.assertEqual(401, invalid_auth.status_code, invalid_auth.text)
        self.assertEqual(409, stale_fence.status_code, stale_fence.text)
        self.assertEqual(409, wrong_device.status_code, wrong_device.text)
        self.assertEqual(
            409, stale_event_session.status_code, stale_event_session.text
        )
        self.assertEqual(
            409, stale_event_generation.status_code, stale_event_generation.text
        )
        for request_error in (
            missing_auth,
            invalid_auth,
            stale_fence,
            wrong_device,
            stale_event_session,
            stale_event_generation,
        ):
            self.assertNotIn("results", request_error.json())


if __name__ == "__main__":
    unittest.main()
