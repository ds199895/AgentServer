from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
from pathlib import Path

import httpx

from app.execution.bridge import AgentBridge, ReloadingTokenFile, _validated_base_url
from app.execution.bridge_commands import BridgeCommandJournalError
from app.execution.reporter import ReporterContext, ReporterSpool, RuntimeReporter


@unittest.skipIf(os.name == "nt", "Unix socket contract")
class AgentBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.root = root
        context = ReporterContext(
            owner_id="alice",
            device_id="device-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            run_id="run-1",
            assignment_id="assignment-1",
            task_id="task-1",
            agent_instance_id="agent-1",
        )
        self.reporter = RuntimeReporter(
            context,
            ReporterSpool(root / "spool.db"),
            producer_id="bridge:device-1",
        )
        self.socket_path = root / "bridge.sock"
        self.bridge = AgentBridge(
            self.reporter,
            address=str(self.socket_path),
            base_url="http://127.0.0.1:9",
            reporter_token="test-token",
            launch_root_pid=os.getpid(),
            context_provider=self.command_context,
            heartbeat_interval=3600,
            command_interval=3600,
        )
        await self.bridge.start()

    async def asyncTearDown(self) -> None:
        await self.bridge.close()
        self.directory.cleanup()

    async def request(self, payload: object) -> dict[str, object]:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    @staticmethod
    def token(*, issued_at: int, expires_at: int, token_id: str) -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {"iat": issued_at, "exp": expires_at, "jti": token_id},
                separators=(",", ":"),
            ).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        return f"{payload}.test-signature"

    @staticmethod
    def command(
        command_id: str = "command-1",
        *,
        sequence: int = 7,
        payload: dict[str, object] | None = None,
        expires_at: float | None = None,
    ) -> dict[str, object]:
        return {
            "sequence": sequence,
            "command_id": command_id,
            "owner_id": "alice",
            "target_kind": "agent_instance",
            "target_id": "agent-1",
            "type": "cancel",
            "payload": payload
            or {
                "reason": "user",
                "run_id": "run-1",
                "assignment_id": "assignment-1",
                "terminal_id": "terminal-1",
                "launch_id": "launch-1",
                "terminal_lease_id": "lease-1",
                "terminal_lease_revision": 1,
            },
            "status": "delivered",
            "expected_revision": 2,
            "created_at": 100.0,
            "expires_at": expires_at,
            "delivered_at": 101.0,
            "acked_at": None,
            "ack_payload": {},
        }

    @staticmethod
    def command_context(
        *,
        lease_id: str = "lease-1",
        lease_revision: int = 1,
        server_time: float | None = None,
        lease_expires_at: float | None = None,
    ) -> dict[str, object]:
        timestamp = time.time() if server_time is None else float(server_time)
        return {
            "managed": True,
            "terminal_id": "terminal-1",
            "launch_id": "launch-1",
            "active_run_id": "run-1",
            "assignment": {"assignment_id": "assignment-1"},
            "context_revision": 2,
            "server_time": timestamp,
            "terminal_lease": {
                "id": lease_id,
                "revision": lease_revision,
                "expires_at": (
                    timestamp + 3600
                    if lease_expires_at is None
                    else float(lease_expires_at)
                ),
            },
        }

    async def replace_bridge(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        command_handler=None,
        command_handler_idempotent: bool = False,
        reporter_token_provider=None,
        command_token_provider=None,
        launch_root_pid: int | None = None,
        context_provider=None,
        start: bool = True,
    ) -> None:
        await self.bridge.close()
        self.bridge = AgentBridge(
            self.reporter,
            address=str(self.socket_path),
            base_url="https://agentserver.example",
            reporter_token="report-token",
            command_token="command-token",
            reporter_token_provider=reporter_token_provider,
            command_token_provider=command_token_provider,
            launch_root_pid=launch_root_pid or os.getpid(),
            context_provider=context_provider
            or self.command_context,
            command_handler=command_handler,
            command_handler_idempotent=command_handler_idempotent,
            http_transport=transport,
            heartbeat_interval=3600,
            command_interval=3600,
        )
        if start:
            await self.bridge.start()

    async def wait_until(self, predicate, *, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("condition was not reached before timeout")
            await asyncio.sleep(0.01)

    async def test_context_and_event_share_one_local_protocol(self) -> None:
        context = await self.request({"action": "context"})
        self.assertTrue(context["ok"])
        self.assertEqual("terminal-1", context["context"]["terminal_id"])

        result = await self.request(
            {
                "action": "event",
                "event_type": "run.activity.changed",
                "payload": {"activity": "coding"},
            }
        )
        self.assertTrue(result["ok"])
        pending = self.reporter.spool.pending()
        self.assertTrue(any(item["payload"].get("activity") == "coding" for item in pending))
        self.assertEqual(0o600, self.socket_path.stat().st_mode & 0o777)

    async def test_second_bridge_cannot_replace_or_unlink_live_socket(self) -> None:
        contender = AgentBridge(
            self.reporter,
            address=str(self.socket_path),
            base_url="http://127.0.0.1:9",
            reporter_token="test-token",
            launch_root_pid=os.getpid(),
            context_provider=lambda: {
                "managed": True,
                "terminal_id": "terminal-1",
                "launch_id": "launch-1",
                "active_run_id": "run-1",
                "assignment": {"assignment_id": "assignment-1"},
                "context_revision": 2,
            },
            heartbeat_interval=3600,
            command_interval=3600,
        )
        with self.assertRaisesRegex(RuntimeError, "already in use"):
            await contender.start()
        await contender.close()

        self.assertTrue(self.socket_path.is_socket())
        response = await self.request({"action": "context"})
        self.assertTrue(response["ok"])

    async def test_malformed_or_control_like_input_cannot_become_a_command(self) -> None:
        malformed = await self.request(["cancel", "run-1"])
        self.assertFalse(malformed["ok"])
        unsupported = await self.request({"action": "cancel", "run_id": "run-1"})
        self.assertFalse(unsupported["ok"])
        ordinary_output = await self.request(
            {
                "action": "event",
                "event_type": "run.activity.changed",
                "payload": {"summary": "cancel command-1", "activity": "coding"},
            }
        )
        self.assertTrue(ordinary_output["ok"])
        self.assertEqual([], self.bridge.command_journal.pending())

    async def test_same_uid_process_outside_bound_launch_is_rejected(self) -> None:
        launch_root = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            close_fds=True,
        )
        try:
            await self.replace_bridge(launch_root_pid=launch_root.pid)
            response = await self.request({"action": "context"})
            self.assertFalse(response["ok"])
            self.assertIn("not authorized", response["error"])
        finally:
            launch_root.terminate()
            launch_root.wait(timeout=5)

    async def test_heartbeat_uses_dedicated_endpoint_instead_of_poisoning_event_wal(self) -> None:
        self.bridge.send_heartbeat = AsyncMock(return_value={"lease": {"status": "active"}})
        result = await self.request({"action": "heartbeat"})
        self.assertTrue(result["ok"])
        self.assertEqual("active", result["heartbeat"]["lease"]["status"])
        self.assertGreaterEqual(self.bridge.send_heartbeat.await_count, 1)
        self.assertEqual([], self.reporter.spool.pending())

    async def test_command_cursor_and_pending_commands_survive_bridge_restart(self) -> None:
        self.bridge.command_journal.record_server_commands([self.command()])
        self.assertEqual(7, self.bridge.command_journal.cursor)

        await self.replace_bridge()
        pending = await self.request({"action": "commands"})

        self.assertTrue(pending["ok"])
        self.assertEqual(7, pending["cursor"])
        self.assertEqual("command-1", pending["commands"][0]["command_id"])
        self.assertEqual("pending", pending["commands"][0]["status"])
        self.assertEqual([], self.bridge.command_journal.pending_acks())

    async def test_lost_ack_response_reuses_stable_ack_id(self) -> None:
        seen: list[dict[str, object]] = []

        def server(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ack"):
                seen.append(json.loads(request.content))
                if len(seen) == 1:
                    raise httpx.ReadError("response was lost", request=request)
                return httpx.Response(
                    200,
                    request=request,
                    json={"command": {"status": "accepted"}},
                )
            if request.url.path.endswith("/commands"):
                return httpx.Response(200, request=request, json={"commands": []})
            return httpx.Response(200, request=request, json={"lease": {}})

        await self.replace_bridge(transport=httpx.MockTransport(server))
        # Let the initial background poll reach its long wait before creating
        # the ACK, so this test controls the lost-response retry deterministically.
        await asyncio.sleep(0.05)
        self.bridge.command_journal.record_server_commands([self.command()])

        request = {
            "action": "command_ack",
            "command_id": "command-1",
            "status": "accepted",
            "payload": {"will_cancel": True},
        }
        lost = await self.request(request)
        self.assertFalse(lost["ok"])
        [pending_ack] = self.bridge.command_journal.pending_acks()

        await self.replace_bridge(transport=httpx.MockTransport(server))
        await asyncio.sleep(0.05)
        retried = await self.request(request)
        self.assertTrue(retried["ok"])
        self.assertEqual(pending_ack.ack_id, retried["ack"]["ack_id"])
        self.assertEqual(seen[0]["ack_id"], seen[1]["ack_id"])
        self.assertEqual([], self.bridge.command_journal.pending_acks())

    async def test_duplicate_delivery_is_idempotent_and_fingerprint_is_enforced(self) -> None:
        command = self.command()
        command_handler = AsyncMock(return_value="completed")
        responses = [[command], [command], [{**command, "payload": {"reason": "other"}}]]

        def server(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                "Bearer command-token", request.headers.get("authorization")
            )
            if request.method == "GET":
                return httpx.Response(
                    200,
                    request=request,
                    json={"commands": responses.pop(0)},
                )
            return httpx.Response(
                200,
                request=request,
                json={"command": {"status": "completed"}},
            )

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            command_handler=command_handler,
            start=False,
        )
        await self.bridge.poll_commands()
        await self.bridge.poll_commands()
        self.assertEqual(1, command_handler.await_count)
        self.assertEqual(7, self.bridge.command_journal.cursor)

        with self.assertRaises(BridgeCommandJournalError):
            await self.bridge.poll_commands()
        self.assertEqual(7, self.bridge.command_journal.cursor)

    async def test_handler_is_retried_at_least_once_after_failure(self) -> None:
        command = self.command()
        handler = AsyncMock(side_effect=[RuntimeError("crashed"), "completed"])
        first = True

        def server(request: httpx.Request) -> httpx.Response:
            nonlocal first
            if request.method == "GET":
                commands = [command] if first else []
                first = False
                return httpx.Response(200, request=request, json={"commands": commands})
            return httpx.Response(
                200,
                request=request,
                json={"command": {"status": "completed"}},
            )

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            command_handler=handler,
            command_handler_idempotent=True,
            start=False,
        )
        with self.assertRaises(RuntimeError):
            await self.bridge.poll_commands()
        await self.bridge.poll_commands()

        self.assertEqual(2, handler.await_count)
        self.assertEqual([], self.bridge.command_journal.pending())
        self.assertEqual([], self.bridge.command_journal.pending_acks())

    async def test_non_idempotent_handler_failure_is_quarantined_not_retried(self) -> None:
        command = self.command()
        handler = AsyncMock(side_effect=RuntimeError("outcome is unknown"))
        first = True

        def server(request: httpx.Request) -> httpx.Response:
            nonlocal first
            if request.method == "GET":
                commands = [command] if first else []
                first = False
                return httpx.Response(200, request=request, json={"commands": commands})
            self.fail("an uncertain command must not be acknowledged")

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            command_handler=handler,
            start=False,
        )
        with self.assertRaises(RuntimeError):
            await self.bridge.poll_commands()
        await self.bridge.poll_commands()

        handler.assert_awaited_once()
        [uncertain] = self.bridge.command_journal.pending()
        self.assertEqual("uncertain", uncertain["status"])
        self.assertIn("RuntimeError", uncertain["uncertain_reason"])

    async def test_expired_command_advances_cursor_but_is_never_executed(self) -> None:
        expired = self.command(expires_at=200.0)
        handler = AsyncMock(return_value="completed")

        def server(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, request=request, json={"commands": [expired]})
            self.fail("an expired command must not be acknowledged")

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            command_handler=handler,
            context_provider=lambda: self.command_context(
                server_time=300.0,
                lease_expires_at=1000.0,
            ),
            start=False,
        )
        # A device clock behind the server must not resurrect the command.
        self.bridge.command_journal.clock = lambda: 0.0
        commands = await self.bridge.poll_commands()

        self.assertEqual([], commands)
        self.assertEqual(7, self.bridge.command_journal.cursor)
        handler.assert_not_awaited()

    async def test_server_time_keeps_valid_command_when_device_clock_is_ahead(self) -> None:
        command = self.command(expires_at=200.0)
        handler = AsyncMock(return_value="completed")
        delivered = False

        def server(request: httpx.Request) -> httpx.Response:
            nonlocal delivered
            if request.method == "GET":
                commands = [] if delivered else [command]
                delivered = True
                return httpx.Response(200, request=request, json={"commands": commands})
            return httpx.Response(
                200,
                request=request,
                json={"command": {"status": "completed"}},
            )

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            command_handler=handler,
            context_provider=lambda: self.command_context(
                lease_revision=3,
                server_time=100.0,
                lease_expires_at=1000.0,
            ),
            start=False,
        )
        # Every journal operation on the Bridge path receives calibrated
        # server time instead of this deliberately wrong wall clock.
        self.bridge.command_journal.clock = lambda: 10_000.0

        self.assertEqual([], await self.bridge.poll_commands())
        handler.assert_awaited_once()

    async def test_lease_change_after_get_blocks_handler_before_side_effect(self) -> None:
        command = self.command()
        handler = AsyncMock(return_value="completed")
        contexts = [
            self.command_context(lease_id="lease-1"),
            self.command_context(lease_id="lease-2"),
        ]

        def context_provider() -> dict[str, object]:
            return contexts.pop(0) if contexts else self.command_context(
                lease_id="lease-2"
            )

        def server(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200, request=request, json={"commands": [command]}
                )
            self.fail("a command whose lease changed must not be acknowledged")

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            command_handler=handler,
            context_provider=context_provider,
            start=False,
        )

        with self.assertRaisesRegex(ValueError, "lease fence"):
            await self.bridge.poll_commands()
        handler.assert_not_awaited()
        [pending] = self.bridge.command_journal.pending()
        self.assertEqual("pending", pending["status"])

    async def test_reassigned_lease_blocks_local_exposure_recovery_and_ack(self) -> None:
        self.bridge.command_journal.record_server_commands([self.command()])
        executing = self.bridge.command_journal.begin_handler("command-1")
        assert executing is not None
        self.bridge.command_journal.mark_uncertain(
            "command-1", "simulated restart after side effect"
        )
        await self.replace_bridge(
            context_provider=lambda: self.command_context(lease_id="lease-2")
        )

        exposed = await self.request({"action": "commands"})
        self.assertFalse(exposed["ok"])
        self.assertIn("lease fence", exposed["error"])
        recovered = await self.request(
            {
                "action": "command_recover",
                "command_id": "command-1",
                "strategy": "retry_idempotent",
            }
        )
        self.assertFalse(recovered["ok"])
        self.assertIn("lease fence", recovered["error"])
        acknowledged = await self.request(
            {
                "action": "command_ack",
                "command_id": "command-1",
                "status": "completed",
                "payload": {},
            }
        )
        self.assertFalse(acknowledged["ok"])
        self.assertFalse(acknowledged["server_acknowledged"])
        self.assertEqual("uncertain", acknowledged["command"]["status"])

    async def test_downloaded_command_is_not_exposed_or_executed_after_run_loses_fence(self) -> None:
        self.bridge.command_journal.record_server_commands([self.command()])
        handler = AsyncMock(return_value="completed")

        def server(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, request=request, json={"commands": []})
            self.fail("a stale downloaded command must not be acknowledged")

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            command_handler=handler,
            context_provider=lambda: {
                "managed": True,
                "terminal_id": "terminal-1",
                "launch_id": "launch-1",
                "active_run_id": None,
                "assignment": None,
            },
        )
        exposed = await self.request({"action": "commands"})
        self.assertFalse(exposed["ok"])
        self.assertIn("no longer the active assignment", exposed["error"])
        stale_ack = await self.request(
            {
                "action": "command_ack",
                "command_id": "command-1",
                "status": "completed",
                "payload": {},
            }
        )
        self.assertFalse(stale_ack["ok"])
        self.assertIn("no longer the active assignment", stale_ack["error"])
        with self.assertRaisesRegex(
            ValueError, "no longer the active assignment"
        ):
            await self.bridge.poll_commands()
        handler.assert_not_awaited()
        [pending] = self.bridge.command_journal.pending()
        self.assertEqual("pending", pending["status"])

    async def test_executing_command_reopens_uncertain_until_explicit_recovery(self) -> None:
        self.bridge.command_journal.record_server_commands([self.command()])
        executing = self.bridge.command_journal.begin_handler("command-1")
        self.assertEqual("executing", executing["status"])
        side_effects = ["performed-before-crash"]
        handler = AsyncMock(return_value="completed")

        def server(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, request=request, json={"commands": []})
            return httpx.Response(
                200,
                request=request,
                json={"command": {"status": "completed"}},
            )

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            command_handler=handler,
        )
        commands = await self.request({"action": "commands"})
        self.assertEqual("uncertain", commands["commands"][0]["status"])
        self.assertIn("restarted", commands["commands"][0]["uncertain_reason"])
        health = await self.request({"action": "health"})
        self.assertEqual("degraded", health["health"]["status"])
        self.assertEqual(
            "uncertain", health["health"]["commands"]["uncertain"][0]["status"]
        )
        handler.assert_not_awaited()
        self.assertEqual(["performed-before-crash"], side_effects)

        recovered = await self.request(
            {
                "action": "command_recover",
                "command_id": "command-1",
                "strategy": "retry_idempotent",
            }
        )
        self.assertTrue(recovered["ok"])
        self.assertEqual("pending", recovered["command"]["status"])
        await self.bridge.poll_commands()
        handler.assert_awaited_once()
        self.assertEqual([], self.bridge.command_journal.pending())

    async def test_expired_pending_ack_reports_durable_abandoned_state(self) -> None:
        expires_at = time.time() + 60
        self.bridge.command_journal.record_server_commands(
            [self.command(expires_at=expires_at)]
        )
        prepared = self.bridge.command_journal.prepare_ack(
            command_id="command-1",
            status="accepted",
            payload={"will_cancel": True},
        )
        self.bridge.command_journal.pending_acks(now=expires_at + 1)

        result = await self.request(
            {
                "action": "command_ack",
                "command_id": "command-1",
                "status": "accepted",
                "payload": {"will_cancel": True},
            }
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["server_acknowledged"])
        self.assertEqual("expired", result["command"]["status"])
        self.assertEqual(prepared.ack_id, result["ack"]["ack_id"])
        self.assertEqual("abandoned", result["ack"]["delivery_state"])
        self.assertFalse(result["ack"]["server_acknowledged"])
        self.assertIn("expired", result["error"])

    async def test_base_url_requires_https_except_for_exact_loopback_hosts(self) -> None:
        valid = (
            "https://agentserver.example",
            "https://agentserver.example/runtime",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
        )
        for value in valid:
            with self.subTest(value=value):
                self.assertEqual(value, _validated_base_url(value))

        invalid = (
            "http://agentserver.example",
            "http://127.0.0.2:8000",
            "https://user:secret@agentserver.example",
            "https://agentserver.example?token=secret",
            "https://agentserver.example?",
            "https://agentserver.example/#fragment",
            "https://agentserver.example#",
            "https://agent server.example",
            "ftp://agentserver.example",
            "agentserver.example",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _validated_base_url(value)

    async def test_bad_json_and_auth_failures_degrade_but_do_not_kill_tasks(self) -> None:
        mode = "bad-json"
        requests = {"forward": 0, "commands": 0}

        def server(request: httpx.Request) -> httpx.Response:
            component = "commands" if request.method == "GET" else "forward"
            requests[component] += 1
            if mode == "bad-json":
                return httpx.Response(200, request=request, text="{")
            if mode == "unauthorized":
                return httpx.Response(401, request=request, json={"detail": "expired"})
            if request.method == "GET":
                return httpx.Response(200, request=request, json={"commands": []})
            return httpx.Response(200, request=request, json={"lease": {}})

        await self.replace_bridge(transport=httpx.MockTransport(server), start=False)
        self.bridge.heartbeat_interval = 0.03
        self.bridge.command_interval = 0.03
        await self.bridge.start()
        await self.wait_until(
            lambda: requests["forward"] >= 1 and requests["commands"] >= 1
        )
        await asyncio.sleep(0.02)
        degraded = await self.request({"action": "health"})
        self.assertEqual("degraded", degraded["health"]["status"])
        self.assertIsNotNone(degraded["health"]["last_error"])
        self.assertEqual("running", degraded["health"]["tasks"]["forward"]["task"])
        self.assertEqual("running", degraded["health"]["tasks"]["commands"]["task"])

        mode = "unauthorized"
        baseline = dict(requests)
        await self.wait_until(
            lambda: requests["forward"] > baseline["forward"]
            and requests["commands"] > baseline["commands"]
        )
        await asyncio.sleep(0.02)
        unauthorized = await self.request({"action": "health"})
        self.assertTrue(unauthorized["health"]["auth_expired"])
        self.assertEqual("degraded", unauthorized["health"]["status"])

        mode = "healthy"
        baseline = dict(requests)
        await self.wait_until(
            lambda: requests["forward"] > baseline["forward"]
            and requests["commands"] > baseline["commands"]
        )
        await asyncio.sleep(0.02)
        recovered = await self.request({"action": "health"})
        self.assertEqual("healthy", recovered["health"]["status"])
        self.assertFalse(recovered["health"]["auth_expired"])
        self.assertIsNone(recovered["health"]["last_error"])
        self.assertIsNotNone(recovered["health"]["last_success"])

    async def test_token_file_rotation_recovers_after_401_and_failed_reload(self) -> None:
        token_path = self.root / "report.token"
        token_path.write_text("old-token\n", encoding="utf-8")
        token_path.chmod(0o600)
        provider = ReloadingTokenFile(token_path)
        seen: list[str] = []

        def server(request: httpx.Request) -> httpx.Response:
            authorization = request.headers.get("authorization", "")
            if request.method == "GET":
                return httpx.Response(200, request=request, json={"commands": []})
            seen.append(authorization)
            if authorization == "Bearer new-token":
                return httpx.Response(200, request=request, json={"lease": {}})
            return httpx.Response(401, request=request, json={"detail": "expired"})

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            reporter_token_provider=provider,
            start=False,
        )
        self.bridge.heartbeat_interval = 0.03
        await self.bridge.start()
        await self.wait_until(lambda: "Bearer old-token" in seen)
        await asyncio.sleep(0.02)
        expired = await self.request({"action": "health"})
        self.assertTrue(expired["health"]["auth_expired"])

        invalid = self.root / "report.invalid"
        invalid.write_text("new-token\n", encoding="utf-8")
        invalid.chmod(0o644)
        os.replace(invalid, token_path)
        old_count = seen.count("Bearer old-token")
        await self.wait_until(lambda: seen.count("Bearer old-token") > old_count)
        reload_failed = await self.request({"action": "health"})
        self.assertEqual(
            "degraded", reload_failed["health"]["credentials"]["reporter"]["status"]
        )
        self.assertIn(
            "0600",
            reload_failed["health"]["credentials"]["reporter"]["last_error"],
        )

        replacement = self.root / "report.next"
        replacement.write_text("new-token\n", encoding="utf-8")
        replacement.chmod(0o600)
        os.replace(replacement, token_path)
        await self.wait_until(lambda: "Bearer new-token" in seen)
        await asyncio.sleep(0.02)
        recovered = await self.request({"action": "health"})
        self.assertEqual("healthy", recovered["health"]["status"])
        self.assertFalse(recovered["health"]["auth_expired"])
        self.assertEqual(
            "healthy", recovered["health"]["credentials"]["reporter"]["status"]
        )
        self.assertNotIn("old-token", json.dumps(recovered))
        self.assertNotIn("new-token", json.dumps(recovered))

    async def test_due_token_is_refreshed_and_atomically_persisted_before_use(self) -> None:
        now = int(time.time())
        old_token = self.token(
            issued_at=now - 890, expires_at=now + 10, token_id="old"
        )
        new_token = self.token(
            issued_at=now, expires_at=now + 900, token_id="new"
        )
        token_path = self.root / "rotating-report.token"
        token_path.write_text(old_token + "\n", encoding="utf-8")
        token_path.chmod(0o600)
        provider = ReloadingTokenFile(token_path)
        requests: list[tuple[str, str]] = []

        def server(request: httpx.Request) -> httpx.Response:
            authorization = request.headers.get("authorization", "")
            requests.append((request.url.path, authorization))
            if request.url.path.endswith("/token:refresh"):
                self.assertEqual(f"Bearer {old_token}", authorization)
                return httpx.Response(
                    200,
                    request=request,
                    json={"token": new_token, "token_type": "Bearer"},
                )
            if request.url.path.endswith("/heartbeat"):
                self.assertEqual(f"Bearer {new_token}", authorization)
                return httpx.Response(200, request=request, json={"lease": {}})
            return httpx.Response(200, request=request, json={"commands": []})

        await self.replace_bridge(
            transport=httpx.MockTransport(server),
            reporter_token_provider=provider,
            start=False,
        )
        result = await self.bridge.send_heartbeat()

        self.assertEqual({}, result["lease"])
        self.assertEqual(new_token, provider())
        self.assertEqual(0o600, token_path.stat().st_mode & 0o777)
        self.assertEqual(
            [
                "/api/runtime/v1/token:refresh",
                "/api/runtime/v1/heartbeat",
            ],
            [path for path, _authorization in requests],
        )

    async def test_token_file_provider_requires_exact_private_mode(self) -> None:
        token_path = self.root / "loose.token"
        token_path.write_text("token\n", encoding="utf-8")
        token_path.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "0600"):
            ReloadingTokenFile(token_path)

        token_path.chmod(0o600)
        provider = ReloadingTokenFile(token_path)
        self.assertEqual("token", provider())


if __name__ == "__main__":
    unittest.main()
