from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.execution.device_runtime import (
    DeviceRuntimeAuthenticationError,
    DeviceRuntimeConflict,
    DeviceRuntimeFenceError,
    DeviceRuntimeNotFound,
    DeviceRuntimeService,
    DeviceRuntimeStore,
)
from app.execution.errors import CommandConflict, ValidationError
from app.execution.models import CommandStatus
from app.execution.store import ExecutionStore


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class DeviceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "runtime.db"
        self.clock = MutableClock()
        self.execution = ExecutionStore(self.database)
        self.store = DeviceRuntimeStore(self.database, clock=self.clock)
        self.devices = {
            ("alice", "device-a"),
            ("alice", "device-b"),
            ("bob", "device-a"),
        }
        self.service = DeviceRuntimeService(
            self.store,
            self.execution,
            device_exists=lambda owner, device: (owner, device) in self.devices,
            clock=self.clock,
            offline_after=30,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def enroll(
        self,
        device_id: str = "device-a",
        *,
        owner_id: str = "alice",
        runtime_session_id: str | None = None,
        generation: int = 1,
    ):
        enrollment = self.service.issue_enrollment(
            owner_id=owner_id, device_id=device_id
        )
        grant = self.service.consume_enrollment(enrollment.token)
        claims = self.service.authenticate(grant.token)
        host_session = runtime_session_id or f"host-{device_id}-{generation}"
        host = self.service.heartbeat(
            claims,
            instance_id=f"instance-{device_id}",
            boot_id=f"boot-{device_id}-{generation}",
            runtime_session_id=host_session,
            generation=generation,
            runtime_version="1.2.3",
            platform={"os": "linux", "arch": "x86_64", "hostname": device_id},
            capabilities={
                "features": ["commands.poll", "sessions"],
                "providers": [
                    {
                        "id": "codex",
                        "transport": "app-server",
                        "available": True,
                        "version": "0.1",
                        "features": ["session.start", "turn.send"],
                    }
                ],
            },
        )
        return enrollment, grant, claims, host

    def sibling_service(self) -> DeviceRuntimeService:
        return DeviceRuntimeService(
            DeviceRuntimeStore(self.database, clock=self.clock),
            ExecutionStore(self.database),
            device_exists=lambda owner, device: (owner, device) in self.devices,
            clock=self.clock,
            offline_after=30,
        )

    def make_ready_session(self, claims, host, session_id: str):
        self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id=session_id,
        )
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=session_id,
            events=[
                {
                    "event_id": f"{session_id}-started",
                    "producer_seq": 1,
                    "type": "session.started",
                    "payload": {},
                }
            ],
        )
        return self.service.get_session(owner_id="alice", session_id=session_id)

    def test_enrollment_is_one_time_hashed_and_concurrency_safe(self) -> None:
        enrollment = self.service.issue_enrollment(
            owner_id="alice", device_id="device-a", ttl=30
        )
        self.assertEqual(
            enrollment.token, enrollment.as_dict()["enrollment_token"]
        )
        with sqlite3.connect(self.database) as connection:
            stored = connection.execute(
                """
                SELECT token_hash FROM device_runtime_enrollments
                WHERE enrollment_id = ?
                """,
                (enrollment.enrollment_id,),
            ).fetchone()[0]
        self.assertNotEqual(enrollment.token, stored)
        self.assertNotIn(enrollment.token, self.database.read_bytes().decode("latin1"))

        def consume():
            try:
                return self.service.consume_enrollment(enrollment.token)
            except DeviceRuntimeAuthenticationError as error:
                return error

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: consume(), range(2)))
        grants = [item for item in results if not isinstance(item, Exception)]
        errors = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(2, len(grants))
        self.assertEqual([], errors)
        self.assertEqual(grants[0].token, grants[1].token)
        replay = self.service.consume_enrollment(enrollment.token)
        self.assertEqual(grants[0].token, replay.token)
        self.assertNotIn(replay.token, self.database.read_bytes().decode("latin1"))
        self.clock.value += 301
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.consume_enrollment(enrollment.token)

    def test_new_enrollment_invalidates_older_unused_token_and_expiry_is_server_time(self) -> None:
        old = self.service.issue_enrollment(owner_id="alice", device_id="device-a")
        fresh = self.service.issue_enrollment(owner_id="alice", device_id="device-a")
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.consume_enrollment(old.token)
        self.clock.value = fresh.expires_at
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.consume_enrollment(fresh.token)

    def test_consumed_enrollment_replay_window_does_not_follow_original_ttl(self) -> None:
        enrollment = self.service.issue_enrollment(
            owner_id="alice", device_id="device-a", ttl=3600
        )
        self.service.consume_enrollment(enrollment.token)
        self.clock.value += 301
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.consume_enrollment(enrollment.token)

    def test_credential_rotation_and_revocation_never_store_plaintext(self) -> None:
        _enrollment, grant, claims, _host = self.enroll()
        replacement = self.service.rotate_credential(
            grant.token,
            request_id="stable-rotation-request",
        )
        replay = self.service.rotate_credential(
            grant.token,
            request_id="stable-rotation-request",
        )
        self.assertEqual(replacement, replay)
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.rotate_credential(
                grant.token,
                request_id="different-rotation-request",
            )
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.authenticate(grant.token)
        replacement_claims = self.service.authenticate(replacement.token)
        self.assertEqual(claims.device_id, replacement_claims.device_id)
        self.assertNotEqual(claims.credential_id, replacement_claims.credential_id)
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.heartbeat(
                claims,
                instance_id="instance-device-a",
                boot_id="boot-device-a-1",
                runtime_session_id="host-device-a-1",
                generation=1,
            )
        replacement_host = self.service.heartbeat(
            replacement_claims,
            instance_id="instance-device-a",
            boot_id="boot-device-a-1",
            runtime_session_id="host-device-a-1",
            generation=1,
        )
        self.assertEqual(
            replacement_claims.credential_id, replacement_host.credential_id
        )
        contents = self.database.read_bytes().decode("latin1")
        self.assertNotIn(grant.token, contents)
        self.assertNotIn(replacement.token, contents)
        self.assertEqual(
            1,
            self.service.revoke_device(
                owner_id="alice",
                device_id="device-a",
                credential_id=replacement_claims.credential_id,
            ),
        )
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.authenticate(replacement.token)
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.service.heartbeat(
                replacement_claims,
                instance_id="instance-device-a",
                boot_id="boot-device-a-1",
                runtime_session_id="host-device-a-1",
                generation=1,
            )
        with self.assertRaises(DeviceRuntimeAuthenticationError):
            self.store.heartbeat(
                replacement_claims,
                instance_id="instance-device-a",
                boot_id="boot-device-a-1",
                runtime_session_id="host-device-a-1",
                generation=1,
                protocol_version=1,
                runtime_version="test",
                health="healthy",
                last_error="",
                capabilities={"providers": [], "features": []},
                platform={"os": "linux", "arch": "x86_64", "hostname": "host"},
                offline_after=30,
                now=self.clock(),
            )
        revoked = self.service.runtime_status(
            owner_id="alice", device_id="device-a"
        )
        self.assertEqual("revoked", revoked["state"])
        self.assertFalse(revoked["online"])

    def test_reenrollment_immediately_retires_old_host_and_sessions(self) -> None:
        _enrollment, _grant, _claims, old_host = self.enroll()
        session = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="reenrollment-session",
        )
        replacement_enrollment = self.service.issue_enrollment(
            owner_id="alice", device_id="device-a"
        )
        replacement = self.service.consume_enrollment(replacement_enrollment.token)

        status = self.service.runtime_status(
            owner_id="alice", device_id="device-a"
        )
        self.assertEqual("revoked", status["state"])
        self.assertFalse(status["online"])
        self.assertEqual(
            "lost",
            self.service.get_session(
                owner_id="alice", session_id=session.session_id
            ).lifecycle,
        )
        with self.assertRaises(DeviceRuntimeFenceError):
            self.service.enqueue_device_command(
                owner_id="alice",
                device_id="device-a",
                command_type="runtime.probe",
            )

        replacement_host = self.service.heartbeat(
            replacement.claims,
            instance_id="instance-device-a-reenrolled",
            boot_id="boot-device-a-reenrolled",
            runtime_session_id="host-device-a-reenrolled",
            generation=old_host.generation + 1,
            capabilities={"providers": [], "features": []},
        )
        self.assertEqual(replacement.claims.credential_id, replacement_host.credential_id)
        self.assertEqual(
            "online",
            self.service.runtime_status(
                owner_id="alice", device_id="device-a"
            )["state"],
        )
        command = self.service.enqueue_device_command(
            owner_id="alice",
            device_id="device-a",
            command_type="runtime.probe",
        )
        self.assertEqual("runtime.probe", command.type)

    def test_rotation_fence_is_checked_inside_the_credential_handoff(self) -> None:
        _enrollment, grant, claims, old_host = self.enroll(
            runtime_session_id="host-old",
            generation=1,
        )
        self.service.heartbeat(
            claims,
            instance_id="instance-device-a",
            boot_id="boot-device-a-2",
            runtime_session_id="host-new",
            generation=2,
        )
        with self.assertRaises(DeviceRuntimeFenceError):
            self.service.rotate_credential(
                grant.token,
                request_id="stale-fence-rotation",
                runtime_session_id=old_host.runtime_session_id,
                generation=old_host.generation,
            )
        self.assertEqual(
            claims,
            self.service.authenticate(grant.token),
        )

    def test_deleted_device_fails_closed_even_with_a_valid_signed_scope(self) -> None:
        _enrollment, grant, claims, _host = self.enroll()
        self.devices.remove(("alice", "device-a"))
        with self.assertRaises(DeviceRuntimeNotFound):
            self.service.authenticate(grant.token)
        with self.assertRaises(DeviceRuntimeNotFound):
            self.service.heartbeat(
                claims,
                instance_id="instance-device-a",
                boot_id="boot-device-a-1",
                runtime_session_id="host-device-a-1",
                generation=1,
            )

    def test_heartbeat_validates_capabilities_and_online_uses_server_time(self) -> None:
        _enrollment, _grant, _claims, host = self.enroll()
        status = self.service.runtime_status(owner_id="alice", device_id="device-a")
        self.assertTrue(status["online"])
        self.assertEqual("online", status["state"])
        self.assertEqual("codex", status["capabilities"]["providers"][0]["id"])
        self.assertEqual(host.online_until, status["online_until"])
        self.clock.value = host.online_until
        status = self.service.runtime_status(owner_id="alice", device_id="device-a")
        self.assertFalse(status["online"])
        self.assertEqual("offline", status["state"])

        enrollment = self.service.issue_enrollment(
            owner_id="alice", device_id="device-b"
        )
        claims = self.service.consume_enrollment(enrollment.token).claims
        with self.assertRaises(ValidationError):
            self.service.heartbeat(
                claims,
                instance_id="instance-device-b",
                boot_id="boot-device-b-1",
                runtime_session_id="host-b",
                generation=1,
                capabilities={"features": ["x" * 101], "providers": []},
            )
        with self.assertRaises(ValidationError):
            self.service.heartbeat(
                claims,
                instance_id="instance-device-b",
                boot_id="boot-device-b-1",
                runtime_session_id="host-b",
                generation=1,
                capabilities={"providers": [], "unknown": True},
            )

    def test_heartbeat_online_lease_never_outlives_credential(self) -> None:
        enrollment = self.service.issue_enrollment(
            owner_id="alice", device_id="device-a"
        )
        grant = self.service.consume_enrollment(
            enrollment.token, credential_ttl=60
        )
        self.service.offline_after = 600
        host = self.service.heartbeat(
            grant.claims,
            instance_id="short-lived-instance",
            boot_id="short-lived-boot",
            runtime_session_id="short-lived-host",
            generation=1,
            capabilities={"providers": [], "features": []},
        )
        self.assertEqual(grant.claims.expires_at, host.online_until)
        self.clock.value = grant.claims.expires_at
        self.assertEqual(
            "offline",
            self.service.runtime_status(
                owner_id="alice", device_id="device-a"
            )["state"],
        )
        with self.assertRaises(DeviceRuntimeFenceError):
            self.service.enqueue_device_command(
                owner_id="alice",
                device_id="device-a",
                command_type="runtime.probe",
            )

    def test_device_commands_are_cross_device_fenced_delivered_and_acked(self) -> None:
        _ea, _ga, claims_a, host_a = self.enroll("device-a")
        _eb, _gb, claims_b, host_b = self.enroll("device-b")
        command = self.service.enqueue_device_command(
            owner_id="alice",
            device_id="device-a",
            command_type="runtime.probe",
            payload={"reason": "acceptance"},
            command_id="probe-a",
        )
        page_b = self.service.poll_commands(
            claims_b,
            runtime_session_id=host_b.runtime_session_id,
            generation=host_b.generation,
        )
        self.assertEqual((), page_b.commands)
        with self.assertRaises(DeviceRuntimeFenceError):
            self.service.ack_command(
                claims_b,
                runtime_session_id=host_b.runtime_session_id,
                generation=host_b.generation,
                command_id=command.id,
                status="completed",
            )

        page_a = self.service.poll_commands(
            claims_a,
            runtime_session_id=host_a.runtime_session_id,
            generation=host_a.generation,
        )
        self.assertEqual(["probe-a"], [item.id for item in page_a.commands])
        self.assertEqual(CommandStatus.DELIVERED, page_a.commands[0].status)
        acknowledged = self.service.ack_command(
            claims_a,
            runtime_session_id=host_a.runtime_session_id,
            generation=host_a.generation,
            command_id=command.id,
            status="completed",
            ack_id="ack-probe-a",
            payload={"ok": True},
        )
        self.assertEqual(CommandStatus.COMPLETED, acknowledged.status)
        self.assertEqual("device-a", acknowledged.ack_payload["device_id"])
        self.assertEqual(host_a.generation, acknowledged.ack_payload["runtime_generation"])
        replay = self.service.ack_command(
            claims_a,
            runtime_session_id=host_a.runtime_session_id,
            generation=host_a.generation,
            command_id=command.id,
            status="completed",
            ack_id="ack-probe-a",
            payload={"ok": True},
        )
        self.assertEqual(CommandStatus.COMPLETED, replay.status)

    def test_command_enqueue_is_linearized_before_host_generation_takeover(self) -> None:
        _enrollment, _grant, claims, old_host = self.enroll(
            runtime_session_id="host-generation-1",
            generation=1,
        )
        sibling = self.sibling_service()
        enqueue_entered = threading.Event()
        resume_enqueue = threading.Event()
        heartbeat_started = threading.Event()
        original_enqueue = self.execution.command_queue.enqueue

        def pause_inside_enqueue(*args, **kwargs):
            connection = kwargs.get("_connection")
            self.assertIsNotNone(connection)
            self.assertTrue(connection.in_transaction)
            enqueue_entered.set()
            if not resume_enqueue.wait(5):
                raise RuntimeError("command enqueue race test did not resume")
            return original_enqueue(*args, **kwargs)

        def replace_host():
            heartbeat_started.set()
            return sibling.heartbeat(
                claims,
                instance_id="instance-device-a",
                boot_id="boot-generation-2",
                runtime_session_id="host-generation-2",
                generation=2,
            )

        with patch.object(
            self.execution.command_queue,
            "enqueue",
            side_effect=pause_inside_enqueue,
        ), concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            command_future = executor.submit(
                self.service.enqueue_device_command,
                owner_id="alice",
                device_id="device-a",
                command_type="runtime.probe",
                command_id="generation-race-command",
            )
            self.assertTrue(enqueue_entered.wait(2))
            heartbeat_future = executor.submit(replace_host)
            self.assertTrue(heartbeat_started.wait(2))
            try:
                with self.assertRaises(concurrent.futures.TimeoutError):
                    heartbeat_future.result(timeout=0.2)
            finally:
                resume_enqueue.set()
            command = command_future.result(timeout=5)
            replacement = heartbeat_future.result(timeout=5)

        self.assertEqual(old_host.runtime_session_id, command.payload["runtime_session_id"])
        self.assertEqual(old_host.generation, command.payload["runtime_generation"])
        self.assertEqual("host-generation-2", replacement.runtime_session_id)
        self.assertEqual(2, replacement.generation)

        page = sibling.poll_commands(
            claims,
            runtime_session_id=replacement.runtime_session_id,
            generation=replacement.generation,
        )
        self.assertEqual((), page.commands)
        self.assertEqual(
            CommandStatus.QUEUED,
            self.execution.command_queue.get(
                owner_id="alice",
                command_id=command.id,
                now=self.clock(),
            ).status,
        )

    def test_command_enqueued_after_takeover_uses_latest_host_fence(self) -> None:
        _enrollment, _grant, claims, _old_host = self.enroll(
            runtime_session_id="host-before-takeover",
            generation=1,
        )
        replacement = self.sibling_service().heartbeat(
            claims,
            instance_id="instance-device-a",
            boot_id="boot-after-takeover",
            runtime_session_id="host-after-takeover",
            generation=2,
        )

        command = self.service.enqueue_device_command(
            owner_id="alice",
            device_id="device-a",
            command_type="runtime.probe",
            command_id="post-takeover-command",
        )

        self.assertEqual(replacement.runtime_session_id, command.payload["runtime_session_id"])
        self.assertEqual(replacement.generation, command.payload["runtime_generation"])

    def test_rotation_winning_poll_race_delivers_no_commands(self) -> None:
        _enrollment, grant, claims, host = self.enroll()
        commands = [
            self.service.enqueue_device_command(
                owner_id="alice",
                device_id="device-a",
                command_type="runtime.probe",
                command_id=f"poll-race-{index}",
            )
            for index in range(2)
        ]
        sibling = self.sibling_service()
        preflight_complete = threading.Event()
        resume = threading.Event()
        original = self.store.require_host_fence

        def pause_after_preflight(*args, **kwargs):
            result = original(*args, **kwargs)
            preflight_complete.set()
            if not resume.wait(5):
                raise RuntimeError("poll race test did not resume")
            return result

        with patch.object(
            self.store, "require_host_fence", side_effect=pause_after_preflight
        ), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.service.poll_commands,
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
            )
            try:
                self.assertTrue(preflight_complete.wait(2))
                sibling.rotate_credential(
                    grant.token, request_id="poll-race-rotation"
                )
            finally:
                resume.set()
            with self.assertRaises(DeviceRuntimeAuthenticationError):
                future.result(timeout=5)

        self.assertEqual(
            [CommandStatus.QUEUED, CommandStatus.QUEUED],
            [
                self.execution.command_queue.get(
                    owner_id="alice", command_id=command.id, now=self.clock()
                ).status
                for command in commands
            ],
        )

    def test_rotation_winning_ack_race_does_not_persist_ack(self) -> None:
        _enrollment, grant, claims, host = self.enroll()
        command = self.service.enqueue_device_command(
            owner_id="alice",
            device_id="device-a",
            command_type="runtime.probe",
            command_id="ack-race-command",
        )
        self.service.poll_commands(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
        )
        sibling = self.sibling_service()
        preflight_complete = threading.Event()
        resume = threading.Event()
        original = self.store.require_host_fence

        def pause_after_preflight(*args, **kwargs):
            result = original(*args, **kwargs)
            preflight_complete.set()
            if not resume.wait(5):
                raise RuntimeError("ACK race test did not resume")
            return result

        with patch.object(
            self.store, "require_host_fence", side_effect=pause_after_preflight
        ), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.service.ack_command,
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                command_id=command.id,
                status="completed",
                ack_id="ack-race",
                payload={"ok": True},
            )
            try:
                self.assertTrue(preflight_complete.wait(2))
                sibling.rotate_credential(
                    grant.token, request_id="ack-race-rotation"
                )
            finally:
                resume.set()
            with self.assertRaises(DeviceRuntimeAuthenticationError):
                future.result(timeout=5)

        current = self.execution.command_queue.get(
            owner_id="alice", command_id=command.id, now=self.clock()
        )
        self.assertIsNotNone(current)
        self.assertEqual(CommandStatus.DELIVERED, current.status)
        with sqlite3.connect(self.database) as connection:
            ack_count = connection.execute(
                "SELECT COUNT(*) FROM execution_command_acks WHERE ack_id = ?",
                ("ack-race",),
            ).fetchone()[0]
        self.assertEqual(0, ack_count)

    def test_rotation_winning_duplicate_ack_race_does_not_leak_command(self) -> None:
        _enrollment, grant, claims, host = self.enroll()
        command = self.service.enqueue_device_command(
            owner_id="alice",
            device_id="device-a",
            command_type="runtime.probe",
            command_id="duplicate-ack-command",
        )
        self.service.poll_commands(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
        )
        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=command.id,
            status="completed",
            ack_id="duplicate-ack",
            payload={"ok": True},
        )
        sibling = self.sibling_service()
        preflight_complete = threading.Event()
        resume = threading.Event()
        original = self.store.require_host_fence

        def pause_after_preflight(*args, **kwargs):
            result = original(*args, **kwargs)
            preflight_complete.set()
            if not resume.wait(5):
                raise RuntimeError("duplicate ACK race test did not resume")
            return result

        with patch.object(
            self.store, "require_host_fence", side_effect=pause_after_preflight
        ), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.service.ack_command,
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                command_id=command.id,
                status="completed",
                ack_id="duplicate-ack",
                payload={"ok": True},
            )
            try:
                self.assertTrue(preflight_complete.wait(2))
                sibling.rotate_credential(
                    grant.token, request_id="duplicate-ack-rotation"
                )
            finally:
                resume.set()
            with self.assertRaises(DeviceRuntimeAuthenticationError):
                future.result(timeout=5)

        with sqlite3.connect(self.database) as connection:
            ack_count = connection.execute(
                "SELECT COUNT(*) FROM execution_command_acks WHERE ack_id = ?",
                ("duplicate-ack",),
            ).fetchone()[0]
        self.assertEqual(1, ack_count)

    def test_revocation_winning_event_races_do_not_reveal_or_append(self) -> None:
        _enrollment, grant, claims, host = self.enroll()
        self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="event-race-session",
        )
        started = {
            "event_id": "event-race-started",
            "producer_seq": 1,
            "type": "session.started",
            "payload": {},
        }
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="event-race-session",
            events=[started],
        )
        sibling = self.sibling_service()

        def race_event(event_claims, events, revoke):
            preflight_complete = threading.Event()
            resume = threading.Event()
            original = self.service._require_claims

            def pause_after_preflight(*args, **kwargs):
                result = original(*args, **kwargs)
                preflight_complete.set()
                if not resume.wait(5):
                    raise RuntimeError("event race test did not resume")
                return result

            with patch.object(
                self.service,
                "_require_claims",
                side_effect=pause_after_preflight,
            ), concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.service.ingest_session_events,
                    event_claims,
                    runtime_session_id=host.runtime_session_id,
                    generation=host.generation,
                    session_id="event-race-session",
                    events=events,
                )
                try:
                    self.assertTrue(preflight_complete.wait(2))
                    result = revoke()
                finally:
                    resume.set()
                with self.assertRaises(DeviceRuntimeAuthenticationError):
                    future.result(timeout=5)
                return result

        replacement = race_event(
            claims,
            [started],
            lambda: sibling.rotate_credential(
                grant.token, request_id="event-race-rotation"
            ),
        )
        replacement_claims = self.service.authenticate(replacement.token)
        race_event(
            replacement_claims,
            [
                {
                    "event_id": "event-exited-after-revoke",
                    "producer_seq": 2,
                    "type": "session.exited",
                    "payload": {"returncode": 0},
                }
            ],
            lambda: sibling.revoke_device(
                owner_id="alice",
                device_id="device-a",
                credential_id=replacement_claims.credential_id,
            ),
        )
        events = self.service.session_events(
            owner_id="alice", session_id="event-race-session"
        )
        self.assertEqual(["event-race-started"], [event.event_id for event in events])
        self.assertEqual(
            "lost",
            self.service.get_session(
                owner_id="alice", session_id="event-race-session"
            ).lifecycle,
        )

    def test_poll_transaction_can_linearize_before_revoke(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        command = self.service.enqueue_device_command(
            owner_id="alice",
            device_id="device-a",
            command_type="runtime.probe",
            command_id="poll-before-revoke",
        )
        sibling = self.sibling_service()
        guard_complete = threading.Event()
        resume = threading.Event()
        revoke_started = threading.Event()
        original = self.store.require_authenticated_host_on

        def pause_inside_transaction(*args, **kwargs):
            result = original(*args, **kwargs)
            guard_complete.set()
            if not resume.wait(5):
                raise RuntimeError("poll transaction race test did not resume")
            return result

        def revoke():
            revoke_started.set()
            return sibling.revoke_device(
                owner_id="alice",
                device_id="device-a",
                credential_id=claims.credential_id,
            )

        with patch.object(
            self.store,
            "require_authenticated_host_on",
            side_effect=pause_inside_transaction,
        ), concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            poll_future = executor.submit(
                self.service.poll_commands,
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
            )
            self.assertTrue(guard_complete.wait(2))
            revoke_future = executor.submit(revoke)
            try:
                self.assertTrue(revoke_started.wait(2))
            finally:
                resume.set()
            page = poll_future.result(timeout=5)
            self.assertEqual(1, revoke_future.result(timeout=5))

        self.assertEqual([command.id], [item.id for item in page.commands])
        self.assertEqual(CommandStatus.DELIVERED, page.commands[0].status)
        self.assertEqual(
            "revoked",
            self.service.runtime_status(
                owner_id="alice", device_id="device-a"
            )["state"],
        )

    def test_new_host_generation_loses_old_sessions_and_skips_stale_commands(self) -> None:
        _enrollment, _grant, claims, host = self.enroll(
            runtime_session_id="host-old", generation=1
        )
        session = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="session-old",
        )
        self.assertEqual("starting", session.lifecycle)
        replacement = self.service.heartbeat(
            claims,
            instance_id="instance-device-a",
            boot_id="boot-device-a-2",
            runtime_session_id="host-new",
            generation=2,
            capabilities={"providers": [], "features": []},
        )
        self.assertEqual("lost", self.service.get_session(
            owner_id="alice", session_id="session-old"
        ).lifecycle)
        with self.assertRaises(DeviceRuntimeFenceError):
            self.service.poll_commands(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
            )
        page = self.service.poll_commands(
            claims,
            runtime_session_id=replacement.runtime_session_id,
            generation=replacement.generation,
        )
        self.assertEqual((), page.commands)
        self.assertGreater(page.next_sequence, 0)
        with self.assertRaises(DeviceRuntimeFenceError):
            self.service.ingest_session_events(
                claims,
                runtime_session_id=replacement.runtime_session_id,
                generation=replacement.generation,
                session_id="session-old",
                events=[
                    {
                        "event_id": "late",
                        "producer_seq": 1,
                        "type": "session.started",
                        "payload": {},
                    }
                ],
            )

    def test_controlled_session_commands_events_and_projection(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        session = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            options={
                "model": "gpt-test",
                "permission_mode": "on-request",
                "resume_cursor": "opaque-cursor",
            },
            session_id="session-1",
        )
        replay = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            options={
                "model": "gpt-test",
                "permission_mode": "on-request",
                "resume_cursor": "opaque-cursor",
            },
            session_id="session-1",
        )
        self.assertEqual(session.session_id, replay.session_id)
        page = self.service.poll_commands(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
        )
        self.assertEqual(["session.start"], [item.type for item in page.commands])
        self.assertEqual(
            "opaque-cursor", page.commands[0].payload["options"]["resume_cursor"]
        )

        started = {
            "event_id": "event-started",
            "producer_seq": 1,
            "type": "session.started",
            "payload": {"provider_session_id": "opaque-provider-id"},
        }
        accepted = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[started],
        )
        duplicate = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[started],
        )
        self.assertEqual("accepted", accepted[0].status)
        self.assertEqual("duplicate", duplicate[0].status)
        ready = self.service.get_session(owner_id="alice", session_id="session-1")
        self.assertEqual("ready", ready.lifecycle)
        self.assertEqual("opaque-provider-id", ready.provider_session_id)

        turn = self.service.send_turn(
            owner_id="alice",
            session_id="session-1",
            input="implement it",
            turn_id="turn-1",
        )
        self.assertEqual("session.turn", turn.type)
        lifecycle = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[
                {
                    "event_id": "turn-started",
                    "producer_seq": 2,
                    "type": "turn.started",
                    "payload": {"turn_id": "turn-1"},
                },
                {
                    "event_id": "request-opened",
                    "producer_seq": 3,
                    "type": "interaction.opened",
                    "payload": {"interaction_id": "approval-1"},
                },
            ],
        )
        self.assertEqual(["accepted", "accepted"], [item.status for item in lifecycle])
        waiting = self.service.get_session(owner_id="alice", session_id="session-1")
        self.assertEqual("waiting", waiting.lifecycle)
        response = self.service.respond_to_request(
            owner_id="alice",
            session_id="session-1",
            request_id="approval-1",
            response={"decision": "accept"},
        )
        self.assertEqual("session.respond", response.type)
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[
                {
                    "event_id": "request-closed",
                    "producer_seq": 4,
                    # Rolling-upgrade compatibility for pre-canonical adapters.
                    "type": "request.resolved",
                    "payload": {"interaction_id": "approval-1"},
                },
                {
                    "event_id": "turn-completed",
                    "producer_seq": 5,
                    "type": "turn.completed",
                    "payload": {"turn_id": "turn-1"},
                },
            ],
        )
        finished = self.service.get_session(owner_id="alice", session_id="session-1")
        self.assertEqual("ready", finished.lifecycle)
        first_stop = self.service.stop_session(
            owner_id="alice", session_id="session-1"
        )
        stopping = self.service.get_session(owner_id="alice", session_id="session-1")
        repeated_stop = self.service.stop_session(
            owner_id="alice", session_id="session-1"
        )
        self.assertEqual(first_stop, repeated_stop)
        self.assertEqual(
            stopping.revision,
            self.service.get_session(owner_id="alice", session_id="session-1").revision,
        )
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[
                {
                    "event_id": "session-stopped",
                    "producer_seq": 6,
                    "type": "session.stopped",
                    "payload": {},
                }
            ],
        )
        self.assertEqual(
            first_stop,
            self.service.stop_session(owner_id="alice", session_id="session-1"),
        )
        self.assertEqual(
            "stopped",
            self.service.get_session(owner_id="alice", session_id="session-1").lifecycle,
        )
        exited = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[
                {
                    "event_id": "session-exited-after-stop",
                    "producer_seq": 7,
                    "type": "session.exited",
                    "payload": {"returncode": 0},
                }
            ],
        )
        self.assertEqual("accepted", exited[0].status)
        self.assertEqual(
            "stopped",
            self.service.get_session(owner_id="alice", session_id="session-1").lifecycle,
        )
        self.assertEqual(
            7,
            len(
                self.service.session_events(
                    owner_id="alice", session_id="session-1"
                )
            ),
        )

    def test_terminal_and_session_exited_are_atomic_in_the_same_event_batch(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="failed-session",
        )
        accepted = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="failed-session",
            events=[
                {
                    "event_id": "session-failed",
                    "producer_seq": 1,
                    "type": "session.failed",
                    "payload": {"error": "provider EOF"},
                },
                {
                    "event_id": "session-exited",
                    "producer_seq": 2,
                    "type": "session.exited",
                    "payload": {"returncode": 1},
                },
            ],
        )
        self.assertEqual(["accepted", "accepted"], [item.status for item in accepted])
        failed = self.service.get_session(
            owner_id="alice", session_id="failed-session"
        )
        self.assertEqual("failed", failed.lifecycle)
        self.assertEqual("provider EOF", failed.last_error)

    def test_stopping_absorbs_delayed_start_before_session_stopped(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        session = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="stop-while-starting",
        )
        self.service.stop_session(
            owner_id="alice", session_id=session.session_id
        )

        [started] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=session.session_id,
            events=[
                {
                    "event_id": "delayed-session-started",
                    "producer_seq": 1,
                    "type": "session.started",
                    "payload": {"provider_session_id": "provider-delayed"},
                }
            ],
        )
        stopping = self.service.get_session(
            owner_id="alice", session_id=session.session_id
        )
        self.assertEqual("accepted", started.status)
        self.assertEqual("stopping", stopping.lifecycle)
        self.assertEqual("provider-delayed", stopping.provider_session_id)

        [stopped] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=session.session_id,
            events=[
                {
                    "event_id": "stopped-after-delayed-start",
                    "producer_seq": 2,
                    "type": "session.stopped",
                    "payload": {},
                }
            ],
        )
        self.assertEqual("accepted", stopped.status)
        self.assertEqual(
            "stopped",
            self.service.get_session(
                owner_id="alice", session_id=session.session_id
            ).lifecycle,
        )

    def test_stopping_absorbs_active_turn_and_interaction_lifecycle(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()

        def stopping_turn(session_id: str, *, failed: bool) -> None:
            ready = self.make_ready_session(claims, host, session_id)
            command = self.service.send_turn(
                owner_id="alice",
                session_id=session_id,
                input="finish before shutdown",
                turn_id=f"client-{session_id}",
            )
            provider_turn_id = f"provider-{session_id}"
            self.service.ack_command(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                command_id=command.id,
                status="completed",
                ack_id=f"ack-{session_id}",
                payload={"turn_id": provider_turn_id},
            )
            self.service.stop_session(owner_id="alice", session_id=session_id)

            [turn_started] = self.service.ingest_session_events(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                session_id=session_id,
                events=[
                    {
                        "event_id": f"{session_id}-turn-started",
                        "producer_seq": 2,
                        "type": "turn.started",
                        "payload": {"turn_id": provider_turn_id},
                    }
                ],
            )
            self.assertEqual("accepted", turn_started.status)
            self.assertEqual(
                "stopping",
                self.service.get_session(
                    owner_id="alice", session_id=session_id
                ).lifecycle,
            )

            interaction_id = f"approval-{session_id}"
            [opened] = self.service.ingest_session_events(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                session_id=session_id,
                events=[
                    {
                        "event_id": f"{session_id}-interaction-opened",
                        "producer_seq": 3,
                        "type": "interaction.opened",
                        "payload": {
                            "interaction_id": interaction_id,
                            "turn_id": provider_turn_id,
                        },
                    }
                ],
            )
            waiting_to_stop = self.service.get_session(
                owner_id="alice", session_id=session_id
            )
            self.assertEqual("accepted", opened.status)
            self.assertEqual("stopping", waiting_to_stop.lifecycle)
            self.assertEqual(interaction_id, waiting_to_stop.active_request_id)

            next_sequence = 4
            if not failed:
                [resolved] = self.service.ingest_session_events(
                    claims,
                    runtime_session_id=host.runtime_session_id,
                    generation=host.generation,
                    session_id=session_id,
                    events=[
                        {
                            "event_id": f"{session_id}-interaction-resolved",
                            "producer_seq": next_sequence,
                            "type": "interaction.resolved",
                            "payload": {
                                "interaction_id": interaction_id,
                                "turn_id": provider_turn_id,
                            },
                        }
                    ],
                )
                self.assertEqual("accepted", resolved.status)
                self.assertEqual(
                    "",
                    self.service.get_session(
                        owner_id="alice", session_id=session_id
                    ).active_request_id,
                )
                next_sequence += 1

            terminal_type = "turn.failed" if failed else "turn.completed"
            terminal_payload = {"turn_id": provider_turn_id}
            if failed:
                terminal_payload["error"] = "provider stopped during shutdown"
            [turn_terminal] = self.service.ingest_session_events(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                session_id=session_id,
                events=[
                    {
                        "event_id": f"{session_id}-{terminal_type}",
                        "producer_seq": next_sequence,
                        "type": terminal_type,
                        "payload": terminal_payload,
                    }
                ],
            )
            after_turn = self.service.get_session(
                owner_id="alice", session_id=session_id
            )
            self.assertEqual("accepted", turn_terminal.status)
            self.assertEqual("stopping", after_turn.lifecycle)
            self.assertEqual("", after_turn.active_request_id)

            [stopped] = self.service.ingest_session_events(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                session_id=session_id,
                events=[
                    {
                        "event_id": f"{session_id}-session-stopped",
                        "producer_seq": next_sequence + 1,
                        "type": "session.stopped",
                        "payload": {},
                    }
                ],
            )
            self.assertEqual("accepted", stopped.status)
            stopped_session = self.service.get_session(
                owner_id="alice", session_id=session_id
            )
            self.assertEqual("stopped", stopped_session.lifecycle)

            [late_turn] = self.service.ingest_session_events(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                session_id=session_id,
                events=[
                    {
                        "event_id": f"{session_id}-late-turn",
                        "producer_seq": next_sequence + 2,
                        "type": "turn.completed",
                        "payload": {"turn_id": provider_turn_id},
                    }
                ],
            )
            self.assertEqual("rejected", late_turn.status)
            self.assertTrue(late_turn.permanent)
            self.assertEqual("session_terminal", late_turn.error_code)
            self.assertEqual(
                "stopped",
                self.service.get_session(
                    owner_id="alice", session_id=session_id
                ).lifecycle,
            )

        stopping_turn("stopping-completed-turn", failed=False)
        stopping_turn("stopping-failed-turn", failed=True)

    def test_expired_and_rejected_stop_commands_fail_closed(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()

        expired = self.make_ready_session(claims, host, "expired-stop")
        self.service.stop_session(
            owner_id="alice", session_id=expired.session_id, ttl=1
        )
        self.clock.value += 1
        expired_result = self.service.get_session(
            owner_id="alice", session_id=expired.session_id
        )
        self.assertEqual("failed", expired_result.lifecycle)
        self.assertEqual("session stop command expired", expired_result.last_error)

        rejected = self.make_ready_session(claims, host, "rejected-stop")
        stop_command = self.service.stop_session(
            owner_id="alice", session_id=rejected.session_id
        )
        unrelated_stop = self.service.enqueue_device_command(
            owner_id="alice",
            device_id="device-a",
            command_type="session.stop",
            command_id="unrelated-rejected-stop",
            payload={"session_id": rejected.session_id},
        )
        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=unrelated_stop.id,
            status="rejected",
            ack_id="unrelated-rejected-stop-ack",
            payload={"error": "not the real stop command"},
        )
        still_stopping = self.store.session(
            owner_id="alice", session_id=rejected.session_id
        )
        assert still_stopping is not None
        self.assertEqual("stopping", still_stopping.lifecycle)
        acknowledged = self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=stop_command.id,
            status="rejected",
            ack_id="rejected-stop-ack",
            payload={"error": "provider stop unavailable"},
        )
        self.assertEqual(CommandStatus.REJECTED, acknowledged.status)
        rejected_result = self.service.get_session(
            owner_id="alice", session_id=rejected.session_id
        )
        self.assertEqual("failed", rejected_result.lifecycle)
        self.assertEqual("provider stop unavailable", rejected_result.last_error)

    def test_stop_command_and_stopping_projection_commit_atomically(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        ready = self.make_ready_session(claims, host, "atomic-stop")
        sibling = self.sibling_service()
        enqueue_entered = threading.Event()
        resume_enqueue = threading.Event()
        event_started = threading.Event()
        original_enqueue = self.execution.command_queue.enqueue

        def pause_stop_enqueue(*args, **kwargs):
            connection = kwargs.get("_connection")
            self.assertIsNotNone(connection)
            self.assertTrue(connection.in_transaction)
            projected = connection.execute(
                "SELECT lifecycle FROM device_runtime_sessions WHERE session_id = ?",
                (ready.session_id,),
            ).fetchone()
            self.assertEqual("stopping", projected[0])
            enqueue_entered.set()
            if not resume_enqueue.wait(5):
                raise RuntimeError("atomic stop race did not resume")
            return original_enqueue(*args, **kwargs)

        def append_stopped_event():
            event_started.set()
            return sibling.ingest_session_events(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                session_id=ready.session_id,
                events=[
                    {
                        "event_id": "atomic-stop-event",
                        "producer_seq": 2,
                        "type": "session.stopped",
                        "payload": {},
                    }
                ],
            )

        with patch.object(
            self.execution.command_queue,
            "enqueue",
            side_effect=pause_stop_enqueue,
        ), concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            stop_future = executor.submit(
                self.service.stop_session,
                owner_id="alice",
                session_id=ready.session_id,
            )
            self.assertTrue(enqueue_entered.wait(2))
            event_future = executor.submit(append_stopped_event)
            self.assertTrue(event_started.wait(2))
            try:
                with self.assertRaises(concurrent.futures.TimeoutError):
                    event_future.result(timeout=0.2)
            finally:
                resume_enqueue.set()
            stop_command = stop_future.result(timeout=5)
            [stopped_event] = event_future.result(timeout=5)

        self.assertEqual("session.stop", stop_command.type)
        self.assertEqual("accepted", stopped_event.status)
        self.assertEqual(
            "stopped",
            self.service.get_session(
                owner_id="alice", session_id=ready.session_id
            ).lifecycle,
        )

    def test_stop_command_enqueue_failure_rolls_back_stopping_projection(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        ready = self.make_ready_session(claims, host, "rollback-stop")

        with patch.object(
            self.execution.command_queue,
            "enqueue",
            side_effect=OSError("stop command journal unavailable"),
        ):
            with self.assertRaises(OSError):
                self.service.stop_session(
                    owner_id="alice", session_id=ready.session_id
                )

        unchanged = self.store.session(
            owner_id="alice", session_id=ready.session_id
        )
        assert unchanged is not None
        self.assertEqual("ready", unchanged.lifecycle)
        self.assertEqual(ready.revision, unchanged.revision)
        with sqlite3.connect(self.database) as connection:
            stop_commands = connection.execute(
                """
                SELECT COUNT(*) FROM execution_commands
                WHERE owner_id = ? AND command_type = 'session.stop'
                  AND json_extract(payload_json, '$.session_id') = ?
                """,
                ("alice", ready.session_id),
            ).fetchone()[0]
        self.assertEqual(0, stop_commands)

    def test_multi_session_event_batch_rolls_back_before_late_device_fence(self) -> None:
        _enrollment, _grant, claims, host = self.enroll("device-a")
        first = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="batch-first-session",
        )
        _other_enrollment, _other_grant, _other_claims, _other_host = self.enroll(
            "device-b"
        )
        second = self.service.create_session(
            owner_id="alice",
            device_id="device-b",
            provider="codex",
            workspace="/workspace",
            session_id="batch-wrong-device-session",
        )

        with self.assertRaises(DeviceRuntimeFenceError):
            self.service.ingest_event_batch(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                groups={
                    first.session_id: [
                        {
                            "event_id": "batch-first-started",
                            "producer_seq": 1,
                            "type": "session.started",
                            "payload": {"provider_session_id": "must-roll-back"},
                        }
                    ],
                    second.session_id: [
                        {
                            "event_id": "batch-wrong-device-started",
                            "producer_seq": 2,
                            "type": "session.started",
                            "payload": {},
                        }
                    ],
                },
            )

        unchanged = self.store.session(
            owner_id="alice", session_id=first.session_id
        )
        assert unchanged is not None
        self.assertEqual("starting", unchanged.lifecycle)
        self.assertEqual(first.revision, unchanged.revision)
        self.assertEqual("", unchanged.provider_session_id)
        self.assertEqual(0, unchanged.last_event_sequence)
        self.assertEqual(
            [],
            self.store.session_events(
                owner_id="alice", session_id=first.session_id
            ),
        )
        self.assertEqual(
            [],
            self.store.session_events(
                owner_id="alice", session_id=second.session_id
            ),
        )

    def test_quota_rejection_replay_stays_permanent_without_accounting(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        ready = self.make_ready_session(claims, host, "quota-response-loss")
        turn = self.service.send_turn(
            owner_id="alice",
            session_id="quota-response-loss",
            input="exercise quota fail-close",
            turn_id="quota-turn",
        )
        reserved = self.store.session(owner_id="alice", session_id=ready.session_id)
        assert reserved is not None
        self.assertEqual("running", reserved.lifecycle)
        with sqlite3.connect(self.database) as connection:
            reservation = connection.execute(
                """
                SELECT active_turn_command_id, active_turn_revision
                FROM device_runtime_sessions WHERE session_id = ?
                """,
                (ready.session_id,),
            ).fetchone()
        self.assertEqual((turn.id, reserved.revision), reservation)
        event = {
            "event_id": "quota-response-loss-event",
            "producer_seq": 2,
            "type": "turn.started",
            "payload": {"turn_id": "quota-turn"},
        }

        def accounting() -> tuple[int, int]:
            with sqlite3.connect(self.database) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(
                        length(CAST(payload_json AS BLOB))
                        + length(CAST(event_type AS BLOB)) + 256
                    ), 0)
                    FROM device_runtime_session_events
                    WHERE owner_id = ? AND session_id = ?
                    """,
                    ("alice", ready.session_id),
                ).fetchone()
            return int(row[0]), int(row[1])

        before = accounting()
        self.assertEqual(1, before[0])

        with patch("app.execution.device_runtime.MAX_SESSION_EVENTS", 1):
            [first] = self.service.ingest_event_batch(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                groups={ready.session_id: [event]},
            )
            # Model a committed server response that was lost before the Host
            # could settle its durable spool: retry the exact envelope.
            [replayed] = self.service.ingest_event_batch(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                groups={ready.session_id: [event]},
            )

        self.assertEqual("rejected", first.status)
        self.assertTrue(first.permanent)
        self.assertEqual("retention_quota", first.error_code)
        self.assertIsNone(first.sequence)
        self.assertEqual("rejected", replayed.status)
        self.assertTrue(replayed.permanent)
        self.assertEqual("session_terminal", replayed.error_code)
        self.assertIsNone(replayed.sequence)
        failed = self.store.session(owner_id="alice", session_id=ready.session_id)
        assert failed is not None
        self.assertEqual("failed", failed.lifecycle)
        self.assertEqual("server_retention_quota", failed.last_error)
        self.assertEqual(reserved.revision + 1, failed.revision)
        self.assertEqual(before, accounting())
        with sqlite3.connect(self.database) as connection:
            reservation = connection.execute(
                """
                SELECT active_turn_command_id, active_turn_revision
                FROM device_runtime_sessions WHERE session_id = ?
                """,
                (ready.session_id,),
            ).fetchone()
        self.assertEqual(("", 0), reservation)

    def test_utf8_event_quota_uses_sqlite_blob_byte_boundary(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        accepted_session = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="utf8-quota-exact",
        )
        rejected_session = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="utf8-quota-over",
        )
        event_type = "运行时.警告"
        payload = {"消息": "你好，😀"}
        payload_json = json.dumps(
            payload, separators=(",", ":"), sort_keys=True, allow_nan=False
        )
        storage_bytes = (
            len(payload_json.encode("utf-8"))
            + len(event_type.encode("utf-8"))
            + 256
        )
        self.assertGreater(len(event_type.encode("utf-8")), len(event_type))

        with patch(
            "app.execution.device_runtime.MAX_SESSION_EVENT_BYTES", storage_bytes
        ):
            [accepted] = self.service.ingest_event_batch(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                groups={
                    accepted_session.session_id: [
                        {
                            "event_id": "utf8-exact-event",
                            "producer_seq": 1,
                            "type": event_type,
                            "payload": payload,
                        }
                    ]
                },
            )
        self.assertEqual("accepted", accepted.status)
        with sqlite3.connect(self.database) as connection:
            recorded = connection.execute(
                """
                SELECT payload_json,
                       length(CAST(payload_json AS BLOB))
                       + length(CAST(event_type AS BLOB)) + 256
                FROM device_runtime_session_events
                WHERE owner_id = ? AND event_id = ?
                """,
                ("alice", "utf8-exact-event"),
            ).fetchone()
        self.assertEqual(payload_json, recorded[0])
        self.assertEqual(storage_bytes, recorded[1])

        with patch(
            "app.execution.device_runtime.MAX_SESSION_EVENT_BYTES",
            storage_bytes - 1,
        ):
            [rejected] = self.service.ingest_event_batch(
                claims,
                runtime_session_id=host.runtime_session_id,
                generation=host.generation,
                groups={
                    rejected_session.session_id: [
                        {
                            "event_id": "utf8-over-event",
                            "producer_seq": 2,
                            "type": event_type,
                            "payload": payload,
                        }
                    ]
                },
            )
        self.assertEqual("rejected", rejected.status)
        self.assertTrue(rejected.permanent)
        self.assertEqual("retention_quota", rejected.error_code)
        self.assertEqual(
            [],
            self.store.session_events(
                owner_id="alice", session_id=rejected_session.session_id
            ),
        )

    def test_observation_before_session_started_preserves_start_revision(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        starting = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="observation-before-start",
        )
        [warning] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=starting.session_id,
            events=[
                {
                    "event_id": "pre-start-warning",
                    "producer_seq": 1,
                    "type": "runtime.warning",
                    "payload": {"code": "provider_starting"},
                }
            ],
        )
        observed = self.service.get_session(
            owner_id="alice", session_id=starting.session_id
        )
        self.assertEqual("accepted", warning.status)
        self.assertEqual("starting", observed.lifecycle)
        self.assertEqual(starting.revision, observed.revision)
        self.assertEqual(warning.sequence, observed.last_event_sequence)

        [started] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=starting.session_id,
            events=[
                {
                    "event_id": "session-started-after-warning",
                    "producer_seq": 2,
                    "type": "session.started",
                    "payload": {"provider_session_id": "provider-session"},
                }
            ],
        )
        ready = self.service.get_session(
            owner_id="alice", session_id=starting.session_id
        )
        self.assertEqual("accepted", started.status)
        self.assertEqual("ready", ready.lifecycle)
        self.assertEqual(starting.revision + 1, ready.revision)

    def test_observation_preserves_turn_reservation_but_provider_id_needs_ack(
        self,
    ) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        ready = self.make_ready_session(claims, host, "provider-turn-before-ack")
        command = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="run the provider turn",
            turn_id="client-turn-id",
        )
        reserved = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        [message] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=ready.session_id,
            events=[
                {
                    "event_id": "message-before-turn-started",
                    "producer_seq": 2,
                    "type": "message.delta",
                    "payload": {"kind": "public_progress"},
                }
            ],
        )
        after_message = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("accepted", message.status)
        self.assertEqual(reserved.revision, after_message.revision)
        with sqlite3.connect(self.database) as connection:
            reservation = connection.execute(
                """
                SELECT active_turn_command_id, active_turn_revision
                FROM device_runtime_sessions WHERE session_id = ?
                """,
                (ready.session_id,),
            ).fetchone()
        self.assertEqual(command.id, reservation[0])
        self.assertEqual(reserved.revision, reservation[1])

        [provider_id_before_ack] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=ready.session_id,
            events=[
                {
                    "event_id": "provider-turn-started-before-ack",
                    "producer_seq": 3,
                    "type": "turn.started",
                    "payload": {"turn_id": "provider-turn-id"},
                }
            ],
        )
        self.assertEqual("rejected", provider_id_before_ack.status)
        self.assertTrue(provider_id_before_ack.permanent)
        self.assertIsNone(provider_id_before_ack.sequence)
        self.assertEqual("invalid_transition", provider_id_before_ack.error_code)
        failed = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("failed", failed.lifecycle)
        self.assertEqual("server_invalid_transition", failed.last_error)

    def test_event_idempotency_conflicts_and_owner_device_scope(self) -> None:
        _ea, _ga, claims_a, host_a = self.enroll("device-a")
        _eb, _gb, claims_b, host_b = self.enroll("device-b")
        self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="session-a",
        )
        event = {
            "event_id": "same-id",
            "producer_seq": 1,
            "type": "session.started",
            "payload": {},
        }
        self.service.ingest_session_events(
            claims_a,
            runtime_session_id=host_a.runtime_session_id,
            generation=host_a.generation,
            session_id="session-a",
            events=[event],
        )
        [conflict] = self.service.ingest_session_events(
            claims_a,
            runtime_session_id=host_a.runtime_session_id,
            generation=host_a.generation,
            session_id="session-a",
            events=[{**event, "payload": {"changed": True}}],
        )
        self.assertEqual("rejected", conflict.status)
        self.assertTrue(conflict.permanent)
        self.assertIsNone(conflict.sequence)
        self.assertEqual("identity_conflict", conflict.error_code)
        self.assertEqual("server_event_identity_conflict", conflict.reason)
        failed = self.service.get_session(owner_id="alice", session_id="session-a")
        self.assertEqual("failed", failed.lifecycle)
        self.assertEqual("server_event_identity_conflict", failed.last_error)
        with self.assertRaises(DeviceRuntimeFenceError):
            self.service.ingest_session_events(
                claims_b,
                runtime_session_id=host_b.runtime_session_id,
                generation=host_b.generation,
                session_id="session-a",
                events=[
                    {
                        "event_id": "cross-device",
                        "producer_seq": 1,
                        "type": "message.delta",
                        "payload": {},
                    }
                ],
            )

    def test_reused_turn_and_ack_ids_are_content_addressed(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="session-1",
        )
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[
                {
                    "event_id": "started",
                    "producer_seq": 1,
                    "type": "session.started",
                    "payload": {},
                }
            ],
        )
        first = self.service.send_turn(
            owner_id="alice",
            session_id="session-1",
            input="one",
            turn_id="stable-turn",
        )
        replay = self.service.send_turn(
            owner_id="alice",
            session_id="session-1",
            input="one",
            turn_id="stable-turn",
        )
        self.assertEqual(first.id, replay.id)
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[
                {
                    "event_id": "turn-running",
                    "producer_seq": 2,
                    "type": "turn.started",
                    "payload": {"turn_id": "stable-turn"},
                }
            ],
        )
        self.assertEqual(
            first,
            self.service.send_turn(
                owner_id="alice",
                session_id="session-1",
                input="one",
                turn_id="stable-turn",
            ),
        )
        with self.assertRaises(CommandConflict):
            self.service.send_turn(
                owner_id="alice",
                session_id="session-1",
                input="different",
                turn_id="stable-turn",
            )
        with self.assertRaises(DeviceRuntimeConflict):
            self.service.send_turn(
                owner_id="alice",
                session_id="session-1",
                input="second concurrent turn",
                turn_id="different-turn",
            )

    def test_missing_start_command_is_reconciled_to_failed(self) -> None:
        self.enroll()
        session = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="missing-start-command",
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "DELETE FROM execution_commands WHERE command_id = ?",
                (session.start_command_id,),
            )

        reconciled = self.service.get_session(
            owner_id="alice", session_id=session.session_id
        )
        self.assertEqual("failed", reconciled.lifecycle)
        self.assertEqual("session start command is missing", reconciled.last_error)

    def test_missing_turn_command_releases_the_exact_reservation(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        ready = self.make_ready_session(claims, host, "missing-turn-command")
        turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="recover this reservation",
            turn_id="missing-turn",
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "DELETE FROM execution_commands WHERE command_id = ?", (turn.id,)
            )

        reconciled = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("ready", reconciled.lifecycle)
        self.assertEqual("turn command is missing", reconciled.last_error)
        with sqlite3.connect(self.database) as connection:
            marker = connection.execute(
                """
                SELECT active_turn_command_id, active_turn_revision
                FROM device_runtime_sessions WHERE session_id = ?
                """,
                (ready.session_id,),
            ).fetchone()
        self.assertEqual(("", 0), marker)

    def test_mismatched_runtime_fence_cannot_leave_a_session_stuck(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        starting = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="wrong-start-fence",
        )
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT payload_json FROM execution_commands WHERE command_id = ?",
                (starting.start_command_id,),
            ).fetchone()
            payload = json.loads(row[0])
            # bool compares equal to integer 1 in Python; fence checks must
            # still reject it as a malformed generation.
            payload["runtime_generation"] = True
            connection.execute(
                "UPDATE execution_commands SET payload_json = ? WHERE command_id = ?",
                (
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    starting.start_command_id,
                ),
            )

        page = self.service.poll_commands(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
        )
        self.assertEqual((), page.commands)
        self.assertEqual(
            "failed",
            self.store.session(
                owner_id="alice", session_id=starting.session_id
            ).lifecycle,
        )

        ready = self.make_ready_session(claims, host, "wrong-turn-fence")
        turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="must not stay reserved",
            turn_id="wrong-fence-turn",
        )
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT payload_json FROM execution_commands WHERE command_id = ?",
                (turn.id,),
            ).fetchone()
            payload = json.loads(row[0])
            payload["runtime_session_id"] = "stale-runtime-session"
            connection.execute(
                "UPDATE execution_commands SET payload_json = ? WHERE command_id = ?",
                (
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    turn.id,
                ),
            )

        released = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("ready", released.lifecycle)
        self.assertEqual("turn command is missing", released.last_error)

    def test_completed_commands_without_lifecycle_events_fail_at_deadline(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        starting = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="completed-start-without-event",
            ttl=1,
        )
        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=starting.start_command_id,
            status="completed",
            ack_id="completed-start-without-event",
        )
        self.clock.value += 1
        failed_start = self.service.get_session(
            owner_id="alice", session_id=starting.session_id
        )
        self.assertEqual("failed", failed_start.lifecycle)
        self.assertEqual(
            "session start completed without a lifecycle event",
            failed_start.last_error,
        )

        ready = self.make_ready_session(claims, host, "completed-turn-without-event")
        turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="side effect happened but event was lost",
            turn_id="completed-without-event",
            ttl=1,
        )
        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=turn.id,
            status="completed",
            ack_id="completed-turn-without-event",
            payload={"turn_id": "provider-turn-without-event"},
        )
        self.clock.value += 1
        failed_turn = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("failed", failed_turn.lifecycle)
        self.assertEqual(
            "turn command completed without a lifecycle event",
            failed_turn.last_error,
        )

    def test_rejected_start_ack_is_bound_to_its_command_and_revision(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        starting = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="start-rejection-cas",
        )
        unrelated = self.service.enqueue_device_command(
            owner_id="alice",
            device_id="device-a",
            command_type="session.start",
            command_id="unrelated-start-command",
            payload={
                "session_id": starting.session_id,
                "session_revision": starting.revision,
            },
        )
        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=unrelated.id,
            status="rejected",
            ack_id="unrelated-start-rejection",
            payload={"error": "not the real start command"},
        )
        unchanged = self.store.session(
            owner_id="alice", session_id=starting.session_id
        )
        self.assertEqual("starting", unchanged.lifecycle)
        self.assertEqual(starting.revision, unchanged.revision)

        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=starting.start_command_id,
            status="rejected",
            ack_id="real-start-rejection",
            payload={"error": "provider unavailable"},
        )
        failed = self.service.get_session(
            owner_id="alice", session_id=starting.session_id
        )
        self.assertEqual("failed", failed.lifecycle)
        self.assertEqual("provider unavailable", failed.last_error)

    def test_expired_and_stale_turn_events_cannot_take_over_a_reservation(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        starting = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="event-after-start-expiry",
            ttl=1,
        )
        self.clock.value += 1
        [late_start] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=starting.session_id,
            events=[
                {
                    "event_id": "late-session-started",
                    "producer_seq": 1,
                    "type": "session.started",
                    "payload": {},
                }
            ],
        )
        self.assertEqual("rejected", late_start.status)
        self.assertTrue(late_start.permanent)
        self.assertEqual("session_terminal", late_start.error_code)
        self.assertEqual(
            "failed",
            self.store.session(
                owner_id="alice", session_id=starting.session_id
            ).lifecycle,
        )

        ready = self.make_ready_session(claims, host, "expired-event-reservation")
        expired_turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="this dispatch expires",
            turn_id="expired-turn",
            ttl=1,
        )
        self.clock.value += 1
        [expired_started] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=ready.session_id,
            events=[
                {
                    "event_id": "expired-turn-started",
                    "producer_seq": 2,
                    "type": "turn.started",
                    "payload": {"turn_id": "expired-turn"},
                }
            ],
        )
        self.assertEqual("rejected", expired_started.status)
        self.assertTrue(expired_started.permanent)
        self.assertEqual("invalid_transition", expired_started.error_code)
        self.assertEqual(
            "failed",
            self.store.session(
                owner_id="alice", session_id=ready.session_id
            ).lifecycle,
        )
        self.assertEqual(
            CommandStatus.EXPIRED,
            self.execution.command_queue.get(
                owner_id="alice", command_id=expired_turn.id, now=self.clock()
            ).status,
        )

        ready = self.make_ready_session(claims, host, "stale-start-reservation")
        stale_turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="the stale turn",
            turn_id="stale-turn",
            ttl=1,
        )
        self.clock.value += 1
        self.assertEqual(
            CommandStatus.EXPIRED,
            self.execution.command_queue.get(
                owner_id="alice", command_id=stale_turn.id, now=self.clock()
            ).status,
        )
        active_turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="the current turn",
            turn_id="current-turn",
        )
        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=active_turn.id,
            status="completed",
            ack_id="current-turn-ack",
            payload={"turn_id": "current-turn"},
        )
        [stale_started] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=ready.session_id,
            events=[
                {
                    "event_id": "stale-turn-started",
                    "producer_seq": 2,
                    "type": "turn.started",
                    "payload": {"turn_id": "stale-turn"},
                }
            ],
        )
        self.assertEqual("rejected", stale_started.status)
        self.assertTrue(stale_started.permanent)
        self.assertEqual("invalid_transition", stale_started.error_code)
        with sqlite3.connect(self.database) as connection:
            marker = connection.execute(
                """
                SELECT lifecycle, active_turn_command_id, active_turn_revision,
                       revision
                FROM device_runtime_sessions WHERE session_id = ?
                """,
                (ready.session_id,),
            ).fetchone()
        self.assertEqual("failed", marker[0])
        self.assertEqual("", marker[1])
        self.assertEqual(0, marker[2])
        self.assertIsNotNone(active_turn)

        ready = self.make_ready_session(claims, host, "stale-complete-reservation")
        current_turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="the current turn",
            turn_id="current-turn",
        )
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=ready.session_id,
            events=[
                {
                    "event_id": "current-turn-started",
                    "producer_seq": 2,
                    "type": "turn.started",
                    "payload": {"turn_id": "current-turn"},
                }
            ],
        )
        [stale_completed] = self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=ready.session_id,
            events=[
                {
                    "event_id": "stale-turn-completed",
                    "producer_seq": 3,
                    "type": "turn.completed",
                    "payload": {"turn_id": "stale-turn"},
                }
            ],
        )
        self.assertEqual("rejected", stale_completed.status)
        self.assertTrue(stale_completed.permanent)
        self.assertEqual("invalid_transition", stale_completed.error_code)
        self.assertEqual(
            "failed",
            self.store.session(
                owner_id="alice", session_id=ready.session_id
            ).lifecycle,
        )
        self.assertIsNotNone(current_turn)

    def test_expired_start_and_turn_commands_reconcile_session_state(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        starting = self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="expiring-start-command",
            ttl=1,
        )
        self.clock.value += 1
        failed = self.service.get_session(
            owner_id="alice", session_id=starting.session_id
        )
        self.assertEqual("failed", failed.lifecycle)
        self.assertEqual("session start command expired", failed.last_error)
        self.assertEqual(
            CommandStatus.EXPIRED,
            self.execution.command_queue.get(
                owner_id="alice",
                command_id=starting.start_command_id,
                now=self.clock(),
            ).status,
        )

        ready = self.make_ready_session(claims, host, "expiring-turn-command")
        turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="expire before dispatch",
            turn_id="expiring-turn",
            ttl=1,
        )
        self.clock.value += 1
        released = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("ready", released.lifecycle)
        self.assertEqual("turn command expired", released.last_error)
        self.assertEqual(
            CommandStatus.EXPIRED,
            self.execution.command_queue.get(
                owner_id="alice", command_id=turn.id, now=self.clock()
            ).status,
        )

    def test_started_turn_is_not_rolled_back_when_its_command_expires(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        ready = self.make_ready_session(claims, host, "started-before-expiry")
        turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="already running",
            turn_id="started-turn",
            ttl=1,
        )
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=ready.session_id,
            events=[
                {
                    "event_id": "started-before-expiry-turn-started",
                    "producer_seq": 2,
                    "type": "turn.started",
                    "payload": {"turn_id": "started-turn"},
                }
            ],
        )
        self.clock.value += 1

        running = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("running", running.lifecycle)
        self.assertEqual(
            CommandStatus.EXPIRED,
            self.execution.command_queue.get(
                owner_id="alice", command_id=turn.id, now=self.clock()
            ).status,
        )

    def test_session_and_turn_command_enqueue_failures_roll_back_atomically(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        with patch.object(
            self.execution.command_queue,
            "enqueue",
            side_effect=RuntimeError("simulated enqueue failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated enqueue failure"):
                self.service.create_session(
                    owner_id="alice",
                    device_id="device-a",
                    provider="codex",
                    workspace="/workspace",
                    session_id="rolled-back-session",
                )
        self.assertIsNone(
            self.store.session(owner_id="alice", session_id="rolled-back-session")
        )

        ready = self.make_ready_session(claims, host, "rolled-back-turn")
        with sqlite3.connect(self.database) as connection:
            command_count_before = connection.execute(
                "SELECT COUNT(*) FROM execution_commands"
            ).fetchone()[0]
        with patch.object(
            self.execution.command_queue,
            "enqueue",
            side_effect=RuntimeError("simulated turn enqueue failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "turn enqueue failure"):
                self.service.send_turn(
                    owner_id="alice",
                    session_id=ready.session_id,
                    input="must roll back",
                    turn_id="rolled-back-turn",
                )
        unchanged = self.store.session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertIsNotNone(unchanged)
        self.assertEqual("ready", unchanged.lifecycle)
        self.assertEqual(ready.revision, unchanged.revision)
        with sqlite3.connect(self.database) as connection:
            command_count_after = connection.execute(
                "SELECT COUNT(*) FROM execution_commands"
            ).fetchone()[0]
            marker = connection.execute(
                """
                SELECT active_turn_command_id, active_turn_revision
                FROM device_runtime_sessions WHERE session_id = ?
                """,
                (ready.session_id,),
            ).fetchone()
        self.assertEqual(command_count_before, command_count_after)
        self.assertEqual(("", 0), marker)

    def test_rejected_turn_ack_is_cas_bound_to_the_active_reservation(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        ready = self.make_ready_session(claims, host, "turn-rejection-cas")
        old_turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="first turn",
            turn_id="old-turn",
        )
        self.service.poll_commands(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
        )
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id=ready.session_id,
            events=[
                {
                    "event_id": "rejection-cas-old-started",
                    "producer_seq": 2,
                    "type": "turn.started",
                    "payload": {"turn_id": "old-turn"},
                },
                {
                    "event_id": "rejection-cas-old-completed",
                    "producer_seq": 3,
                    "type": "turn.completed",
                    "payload": {"turn_id": "old-turn"},
                },
            ],
        )
        active_turn = self.service.send_turn(
            owner_id="alice",
            session_id=ready.session_id,
            input="second turn",
            turn_id="active-turn",
        )
        before_stale_ack = self.store.session(
            owner_id="alice", session_id=ready.session_id
        )

        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=old_turn.id,
            status="rejected",
            ack_id="stale-turn-rejection",
            payload={"error": "late rejection"},
        )
        after_stale_ack = self.store.session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("running", after_stale_ack.lifecycle)
        self.assertEqual(before_stale_ack.revision, after_stale_ack.revision)
        with sqlite3.connect(self.database) as connection:
            marker = connection.execute(
                """
                SELECT active_turn_command_id, active_turn_revision
                FROM device_runtime_sessions WHERE session_id = ?
                """,
                (ready.session_id,),
            ).fetchone()
        self.assertEqual((active_turn.id, after_stale_ack.revision), marker)

        self.service.ack_command(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            command_id=active_turn.id,
            status="rejected",
            ack_id="active-turn-rejection",
            payload={"error": "provider refused the turn"},
        )
        released = self.service.get_session(
            owner_id="alice", session_id=ready.session_id
        )
        self.assertEqual("ready", released.lifecycle)
        self.assertEqual("provider refused the turn", released.last_error)
        with sqlite3.connect(self.database) as connection:
            marker = connection.execute(
                """
                SELECT active_turn_command_id, active_turn_revision
                FROM device_runtime_sessions WHERE session_id = ?
                """,
                (ready.session_id,),
            ).fetchone()
        self.assertEqual(("", 0), marker)

    def test_turn_reservation_and_active_session_limit_are_atomic(self) -> None:
        _enrollment, _grant, claims, host = self.enroll()
        self.service.max_active_sessions = 1
        self.service.create_session(
            owner_id="alice",
            device_id="device-a",
            provider="codex",
            workspace="/workspace",
            session_id="session-1",
        )
        with self.assertRaises(DeviceRuntimeConflict):
            self.service.create_session(
                owner_id="alice",
                device_id="device-a",
                provider="codex",
                workspace="/workspace",
                session_id="session-over-limit",
            )
        self.service.ingest_session_events(
            claims,
            runtime_session_id=host.runtime_session_id,
            generation=host.generation,
            session_id="session-1",
            events=[
                {
                    "event_id": "session-ready",
                    "producer_seq": 1,
                    "type": "session.started",
                    "payload": {},
                }
            ],
        )

        def send(index: int):
            try:
                return self.service.send_turn(
                    owner_id="alice",
                    session_id="session-1",
                    input=f"turn {index}",
                    turn_id=f"turn-{index}",
                )
            except DeviceRuntimeConflict as error:
                return error

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send, range(2)))
        commands = [item for item in results if not isinstance(item, Exception)]
        conflicts = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(1, len(commands))
        self.assertEqual(1, len(conflicts))
        self.assertEqual(
            "running",
            self.service.get_session(
                owner_id="alice", session_id="session-1"
            ).lifecycle,
        )


if __name__ == "__main__":
    unittest.main()
