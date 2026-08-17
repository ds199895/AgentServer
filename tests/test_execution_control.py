from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.execution import ExecutionStore
from app.execution.cli import build_parser, event_from_args
from app.execution.control import (
    ControlProtocolError,
    ExecutionControlBroker,
    LocalLaunchAuthorizer,
    PeerCredentials,
    ProcessIdentity,
)
from app.execution.service import ExecutionService


class LocalLaunchAuthorizerTests(unittest.TestCase):
    UID = 1000

    @staticmethod
    def process(
        pid: int,
        ppid: int,
        *,
        start: int,
        group: int,
        session: int,
        tty: int,
    ) -> ProcessIdentity:
        return ProcessIdentity(
            pid=pid,
            ppid=ppid,
            process_group_id=group,
            session_id=session,
            tty_device=tty,
            start_time_ticks=start,
            uid=LocalLaunchAuthorizerTests.UID,
        )

    def setUp(self) -> None:
        self.processes = {
            100: self.process(100, 1, start=10, group=100, session=100, tty=10),
            101: self.process(101, 100, start=11, group=101, session=100, tty=10),
            102: self.process(102, 101, start=12, group=101, session=100, tty=10),
            200: self.process(200, 1, start=20, group=200, session=200, tty=20),
            201: self.process(201, 200, start=21, group=201, session=200, tty=20),
        }
        self.authorizer = LocalLaunchAuthorizer(
            process_reader=self.processes.get,
            expected_uid=self.UID,
        )
        self.authorizer.bind_launch(
            owner_id="alice",
            terminal_id="terminal-a",
            launch_id="launch-a",
            root_pid=100,
        )
        self.authorizer.bind_launch(
            owner_id="alice",
            terminal_id="terminal-b",
            launch_id="launch-b",
            root_pid=200,
        )

    def authorize(self, pid: int, terminal_id: str, launch_id: str) -> bool:
        return self.authorizer.authorize(
            PeerCredentials(pid=pid, uid=self.UID, gid=self.UID),
            owner_id="alice",
            terminal_id=terminal_id,
            launch_id=launch_id,
        )

    def test_descendant_process_is_authorized_for_its_launch(self) -> None:
        self.assertTrue(self.authorize(102, "terminal-a", "launch-a"))

    def test_same_uid_sibling_terminal_cannot_report_for_another_launch(self) -> None:
        self.assertFalse(self.authorize(201, "terminal-a", "launch-a"))
        self.assertTrue(self.authorize(201, "terminal-b", "launch-b"))

    def test_wrong_launch_identity_is_rejected(self) -> None:
        self.assertFalse(self.authorize(102, "terminal-a", "launch-b"))
        self.assertFalse(self.authorize(102, "terminal-a", "missing"))

    def test_reused_root_pid_is_rejected_by_start_time(self) -> None:
        self.processes[100] = self.process(
            100, 1, start=999, group=100, session=100, tty=10
        )
        self.assertFalse(self.authorize(102, "terminal-a", "launch-a"))


class ExecutionControlBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.service = ExecutionService(ExecutionStore(root / "execution.db"))
        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-1",
            launch_id="launch-1",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-1")
        self.broker = ExecutionControlBroker(
            self.service, root / "control" / "agentserver.sock"
        )
        self.broker.bind_launch(
            owner_id="alice",
            terminal_id="terminal-1",
            launch_id="launch-1",
            root_pid=os.getpid(),
        )

    async def asyncTearDown(self) -> None:
        await self.broker.close()
        self.directory.cleanup()

    @staticmethod
    def scope(*, launch_id: str = "launch-1") -> dict[str, object]:
        return {
            "owner_id": "alice",
            "terminal_id": "terminal-1",
            "launch_id": launch_id,
        }

    async def test_unassigned_terminal_gets_context_and_attach_is_not_fabricated(self) -> None:
        context = self.broker.handle_request(
            {"action": "context", "scope": self.scope()}
        )["context"]
        self.assertTrue(context["managed"])
        self.assertIsNone(context["assignment"])
        self.assertIsNone(context["active_run_id"])
        ignored = self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "agent.registered",
                "payload": {"kind": "kimi"},
            }
        )
        self.assertEqual("no_active_assignment", ignored["ignored"])

    async def test_local_attach_and_phase_use_bound_run_only(self) -> None:
        task = self.service.create_task(owner_id="alice", title="Local work")
        assigned = self.service.assign_task(
            owner_id="alice",
            task_id=task["id"],
            terminal_id="terminal-1",
            agent_kind="kimi",
            expected_task_revision=task["revision"],
        )
        run_id = assigned["runs"][0]["id"]
        attached = self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "agent.registered",
                "payload": {"kind": "kimi"},
            }
        )
        self.assertEqual("accepted", attached["status"])
        phase = self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "run.activity.changed",
                "payload": {"activity": "coding"},
            }
        )
        self.assertEqual("accepted", phase["status"])
        run = self.service.get_run(owner_id="alice", run_id=run_id)
        self.assertEqual("coding", run["state"]["activity"])
        self.assertEqual("adapter", run["evidence"]["activity"]["source"])

    async def test_control_ingress_rebuilds_schema_and_ignores_caller_event_id(self) -> None:
        task = self.service.create_task(owner_id="alice", title="Sanitized local work")
        self.service.assign_task(
            owner_id="alice",
            task_id=task["id"],
            terminal_id="terminal-1",
            agent_kind="generic",
            expected_task_revision=task["revision"],
        )
        self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "agent.registered",
                "adapter": "generic",
                "payload": {"kind": "generic"},
            }
        )
        artifact_type, artifact_payload = event_from_args(
            build_parser().parse_args(
                (
                    "artifact",
                    "reports/result.png",
                    "--kind",
                    "image",
                    "--media-type",
                    "image/png",
                )
            )
        )
        self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": artifact_type,
                "adapter": "generic",
                "payload": {**artifact_payload, "summary": "SECRET-ARTIFACT-EXTRA"},
            }
        )

        with self.assertRaises(ControlProtocolError) as bad_adapter:
            self.broker.handle_request(
                {
                    "action": "event",
                    "scope": self.scope(),
                    "event_type": "run.activity.changed",
                    "adapter": "secret_adapter_token",
                    "payload": {"activity": "coding"},
                }
            )
        self.assertNotIn("secret_adapter_token", str(bad_adapter.exception))

        with self.assertRaises(ControlProtocolError) as bad_code:
            self.broker.handle_request(
                {
                    "action": "event",
                    "scope": self.scope(),
                    "event_type": "run.failed",
                    "adapter": "generic",
                    "payload": {"code": "secret_token_123"},
                }
            )
        self.assertNotIn("secret_token_123", str(bad_code.exception))

        self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "run.activity.changed",
                "event_id": "SECRET-CALLER-EVENT-ID",
                "adapter": "generic",
                "payload": {
                    "activity": "coding",
                    "summary": "SECRET-PAYLOAD",
                    "command": "SECRET-COMMAND",
                },
            }
        )
        self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "span.started",
                "adapter": "generic",
                "payload": {
                    "span_id": "SECRET-SPAN-ID",
                    "name": "SECRET_TOOL_NAME",
                    "kind": "SECRET_KIND",
                },
            }
        )
        self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "run.activity.changed",
                "adapter": "generic",
                "payload": {
                    "activity": "waiting",
                    "wait_reason": "approval",
                    "wait_target_run_id": "SECRET-IRRELEVANT-WAIT-TARGET",
                },
            }
        )
        with self.assertRaises(ControlProtocolError) as invalid_wait_target:
            self.broker.handle_request(
                {
                    "action": "event",
                    "scope": self.scope(),
                    "event_type": "run.activity.changed",
                    "adapter": "generic",
                    "payload": {
                        "activity": "waiting",
                        "wait_reason": "child_run",
                        "wait_target_run_id": "SECRET-UNDECLARED-CHILD",
                    },
                }
            )
        self.assertNotIn(
            "SECRET-UNDECLARED-CHILD", str(invalid_wait_target.exception)
        )

        encoded = json.dumps(
            [
                event.as_dict()
                for event in self.service.store.snapshot(owner_id="alice").events
            ],
            sort_keys=True,
        )
        for secret in (
            "SECRET-CALLER-EVENT-ID",
            "SECRET-PAYLOAD",
            "SECRET-COMMAND",
            "SECRET-SPAN-ID",
            "SECRET_TOOL_NAME",
            "SECRET_KIND",
            "SECRET-IRRELEVANT-WAIT-TARGET",
            "SECRET-UNDECLARED-CHILD",
            "SECRET-ARTIFACT-EXTRA",
            "secret_adapter_token",
            "secret_token_123",
        ):
            self.assertNotIn(secret, encoded)
        artifact_events = [
            event
            for event in self.service.store.snapshot(owner_id="alice").events
            if event.type == "artifact.published"
        ]
        self.assertEqual(1, len(artifact_events))
        self.assertEqual(
            {
                "path": "reports/result.png",
                "kind": "image",
                "media_type": "image/png",
            },
            artifact_events[0].payload,
        )

    async def test_provider_reference_key_survives_broker_restart(self) -> None:
        task = self.service.create_task(owner_id="alice", title="Cross-restart span")
        self.service.assign_task(
            owner_id="alice",
            task_id=task["id"],
            terminal_id="terminal-1",
            agent_kind="generic",
            expected_task_revision=task["revision"],
        )
        started = self.broker.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "span.started",
                "adapter": "generic",
                "payload": {"span_id": "provider-tool-call-1", "name": "shell"},
            }
        )
        self.assertEqual("accepted", started["status"])

        restarted = ExecutionControlBroker(self.service, self.broker.path)
        restarted.bind_launch(
            owner_id="alice",
            terminal_id="terminal-1",
            launch_id="launch-1",
            root_pid=os.getpid(),
        )
        ended = restarted.handle_request(
            {
                "action": "event",
                "scope": self.scope(),
                "event_type": "span.ended",
                "adapter": "generic",
                "payload": {
                    "span_id": "provider-tool-call-1",
                    "name": "shell",
                    "outcome": "succeeded",
                },
            }
        )
        self.assertEqual("accepted", ended["status"])

        span_events = [
            event
            for event in self.service.store.snapshot(owner_id="alice").events
            if event.type in {"span.started", "span.ended"}
        ]
        self.assertEqual(2, len(span_events))
        self.assertEqual(span_events[0].scope.span_id, span_events[1].scope.span_id)
        self.assertEqual(span_events[0].aggregate_id, span_events[1].aggregate_id)
        key_path = self.broker._reference_key_path
        self.assertEqual(0o600, stat.S_IMODE(key_path.stat().st_mode))
        self.assertEqual(32, len(key_path.read_bytes()))

    async def test_explicit_provider_reference_key_must_be_strong(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            ExecutionControlBroker(
                self.service,
                self.broker.path,
                reference_key=b"weak",
            )

    async def test_launch_identity_is_mandatory(self) -> None:
        with self.assertRaisesRegex(ValueError, "launch identity"):
            self.broker.handle_request(
                {"action": "context", "scope": self.scope(launch_id="wrong")}
            )

    @unittest.skipUnless(
        hasattr(socket, "SO_PEERCRED") and Path("/proc/self/stat").is_file(),
        "requires Linux peer PID credentials",
    )
    async def test_unix_socket_is_private_and_round_trips_context(self) -> None:
        await self.broker.start()
        self.assertEqual(0o700, self.broker.path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.broker.path.stat().st_mode & 0o777)
        self.assertTrue(stat.S_ISSOCK(self.broker.path.stat().st_mode))
        reader, writer = await asyncio.open_unix_connection(str(self.broker.path))
        writer.write(
            json.dumps({"action": "context", "scope": self.scope()}).encode()
            + b"\n"
        )
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        self.assertTrue(response["ok"])
        self.assertEqual("terminal-1", response["context"]["terminal_id"])

    @unittest.skipUnless(
        hasattr(socket, "SO_PEERCRED") and Path("/proc/self/stat").is_file(),
        "requires Linux peer PID credentials",
    )
    async def test_second_broker_cannot_replace_a_live_control_socket(self) -> None:
        await self.broker.start()
        original_inode = self.broker.path.stat().st_ino
        other = ExecutionControlBroker(self.service, self.broker.path)
        try:
            with self.assertRaisesRegex(RuntimeError, "already served"):
                await other.start()
            self.assertEqual(original_inode, self.broker.path.stat().st_ino)
            reader, writer = await asyncio.open_unix_connection(
                str(self.broker.path)
            )
            writer.write(
                json.dumps({"action": "context", "scope": self.scope()}).encode()
                + b"\n"
            )
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            self.assertTrue(response["ok"])
        finally:
            await other.close()

    @unittest.skipUnless(
        hasattr(socket, "SO_PEERCRED") and Path("/proc/self/stat").is_file(),
        "requires Linux peer PID credentials and flock",
    )
    async def test_startup_lock_fails_closed_before_probe_and_bind(self) -> None:
        self.broker._prepare_socket_directory()
        descriptor = self.broker._acquire_startup_lock()
        other = ExecutionControlBroker(self.service, self.broker.path)
        try:
            with self.assertRaisesRegex(RuntimeError, "startup lock is already held"):
                await other.start()
            self.assertFalse(self.broker.path.exists())
        finally:
            self.broker._release_startup_lock(descriptor)
            await other.close()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires Unix sockets")
    async def test_stale_cleanup_refuses_replaced_socket_inode(self) -> None:
        self.broker._prepare_socket_directory()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.broker.path))
        stale.close()
        stale_inode = self.broker.path.lstat().st_ino
        replacement_sockets: list[socket.socket] = []
        inode_blockers: list[Path] = []
        real_socket = socket.socket

        class SwappingProbe:
            def settimeout(self, _value: float) -> None:
                return None

            def connect(self, _path: str) -> None:
                self_outer.broker.path.unlink()
                # Consume the just-freed inode so this simulates a genuine path
                # replacement rather than an allocator reusing the same number.
                blocker = self_outer.broker.path.with_name("inode-blocker")
                blocker.touch()
                inode_blockers.append(blocker)
                replacement = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
                replacement.bind(str(self_outer.broker.path))
                replacement_sockets.append(replacement)
                raise ConnectionRefusedError(
                    errno.ECONNREFUSED, "simulated stale-socket race"
                )

            def close(self) -> None:
                return None

        self_outer = self
        try:
            with mock.patch(
                "app.execution.control.socket.socket", return_value=SwappingProbe()
            ):
                with self.assertRaisesRegex(RuntimeError, "changed during stale cleanup"):
                    self.broker._remove_stale_socket()
            self.assertTrue(self.broker.path.exists())
            self.assertNotEqual(stale_inode, self.broker.path.lstat().st_ino)
        finally:
            for replacement in replacement_sockets:
                replacement.close()
            if self.broker.path.exists():
                self.broker.path.unlink()
            for blocker in inode_blockers:
                blocker.unlink(missing_ok=True)

    @unittest.skipUnless(
        hasattr(socket, "SO_PEERCRED") and Path("/proc/self/stat").is_file(),
        "requires Linux peer PID credentials",
    )
    async def test_same_uid_sibling_process_cannot_impersonate_terminal(self) -> None:
        self.service.register_terminal(
            owner_id="alice",
            terminal_id="terminal-2",
            launch_id="launch-2",
        )
        self.service.terminal_ready(owner_id="alice", terminal_id="terminal-2")
        await self.broker.start()
        client = (
            "import json,socket,sys;"
            "sys.stdin.buffer.read(1);"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);"
            "s.connect(sys.argv[1]);"
            "s.sendall(sys.argv[2].encode()+b'\\n');"
            "data=b'';"
            "\nwhile b'\\n' not in data:\n"
            " chunk=s.recv(4096)\n"
            " if not chunk: break\n"
            " data+=chunk\n"
            "sys.stdout.buffer.write(data)"
        )

        async def waiting_client(scope: dict[str, object]):
            request = json.dumps({"action": "context", "scope": scope})
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                client,
                str(self.broker.path),
                request,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        legitimate = await waiting_client(self.scope())
        sibling = await waiting_client(self.scope())
        assert legitimate.pid is not None and sibling.pid is not None
        self.broker.release_launch(
            owner_id="alice", terminal_id="terminal-1", launch_id="launch-1"
        )
        self.broker.bind_launch(
            owner_id="alice",
            terminal_id="terminal-1",
            launch_id="launch-1",
            root_pid=legitimate.pid,
        )
        self.broker.bind_launch(
            owner_id="alice",
            terminal_id="terminal-2",
            launch_id="launch-2",
            root_pid=sibling.pid,
        )
        legitimate_stdout, legitimate_stderr = await legitimate.communicate(b"1")
        sibling_stdout, sibling_stderr = await sibling.communicate(b"1")
        self.assertEqual(b"", legitimate_stderr)
        self.assertEqual(b"", sibling_stderr)
        accepted = json.loads(legitimate_stdout)
        rejected = json.loads(sibling_stdout)
        self.assertTrue(accepted["ok"])
        self.assertFalse(rejected["ok"])
        self.assertIn("not authorized for the managed launch", rejected["error"])

    @unittest.skipUnless(
        hasattr(socket, "SO_PEERCRED") and hasattr(os, "fork"),
        "requires Linux peer PID credentials and fork",
    )
    async def test_descendant_process_can_use_its_bound_launch(self) -> None:
        await self.broker.start()
        child_client = r'''
import os
import socket
import sys

sys.stdin.buffer.read(1)
child = os.fork()
if child == 0:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(sys.argv[1])
    connection.sendall(sys.argv[2].encode() + b"\n")
    response = b""
    while b"\n" not in response:
        chunk = connection.recv(4096)
        if not chunk:
            break
        response += chunk
    os.write(1, response)
    os._exit(0)
_pid, status = os.waitpid(child, 0)
os._exit(os.waitstatus_to_exitcode(status))
'''
        request = json.dumps({"action": "context", "scope": self.scope()})
        root = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            child_client,
            str(self.broker.path),
            request,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert root.pid is not None
        self.broker.release_launch(
            owner_id="alice", terminal_id="terminal-1", launch_id="launch-1"
        )
        self.broker.bind_launch(
            owner_id="alice",
            terminal_id="terminal-1",
            launch_id="launch-1",
            root_pid=root.pid,
        )
        stdout, stderr = await root.communicate(b"1")
        self.assertEqual(b"", stderr)
        response = json.loads(stdout)
        self.assertTrue(response["ok"])
        self.assertEqual("terminal-1", response["context"]["terminal_id"])


if __name__ == "__main__":
    unittest.main()
