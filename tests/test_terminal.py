import asyncio
import base64
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.terminal import (
    STREAM_GAP,
    DetectedService,
    ListeningProcess,
    TerminalManager,
    TerminalSession,
    TerminalStore,
    parse_listener_scan,
    remote_shell_command,
)


class RemoteShellCommandTests(unittest.TestCase):
    def test_system_shell_does_not_add_a_remote_command(self) -> None:
        self.assertEqual([], remote_shell_command("system"))

    def test_windows_shell_commands_are_interactive(self) -> None:
        self.assertEqual(
            ["powershell.exe", "-NoLogo", "-NoExit"],
            remote_shell_command("powershell"),
        )
        self.assertEqual(["cmd.exe", "/Q"], remote_shell_command("cmd"))


class ListenerScanParserTests(unittest.TestCase):
    def test_parses_ss_lsof_netstat_and_normalized_records(self) -> None:
        ss = parse_listener_scan(
            "__AGENTSERVER_LISTENERS__:ss\n"
            'LISTEN 0 511 127.0.0.1:3000 0.0.0.0:* users:(("node",pid=123,fd=20))\n'
            'LISTEN 0 128 [::]:18080 [::]:* users:(("uvicorn",pid=456,fd=8))\n'
            'LISTEN 0 128 192.168.1.3:9999 0.0.0.0:* users:(("private",pid=9,fd=1))\n'
        )
        self.assertEqual(
            [ListeningProcess(3000, 123, "node"), ListeningProcess(18080, 456, "uvicorn")],
            ss,
        )

        lsof = parse_listener_scan(
            "__AGENTSERVER_LISTENERS__:lsof\n"
            "p777\ncpython\nn127.0.0.1:8000\n"
        )
        self.assertEqual([ListeningProcess(8000, 777, "python")], lsof)

        netstat = parse_listener_scan(
            "__AGENTSERVER_LISTENERS__:netstat\n"
            "tcp 0 0 0.0.0.0:5173 0.0.0.0:* LISTEN 88/node\n"
        )
        self.assertEqual([ListeningProcess(5173, 88, "node")], netstat)

        records = parse_listener_scan(
            "__AGENTSERVER_LISTENERS__:records\n"
            "__AGENTSERVER_LISTENER__|8080|321|dotnet\n"
        )
        self.assertEqual([ListeningProcess(8080, 321, "dotnet")], records)


class TerminalManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.manager = TerminalManager(
            command="printf 'ready-marker\\n'",
            cwd=self.directory.name,
            shell="/bin/sh",
            scrollback_bytes=64 * 1024,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.close()
        self.directory.cleanup()

    async def wait_for_output(self, session_id: str, marker: bytes, timeout: float = 3) -> bytes:
        deadline = asyncio.get_running_loop().time() + timeout
        snapshot = b""
        while asyncio.get_running_loop().time() < deadline:
            snapshot, queue = self.manager.attach(session_id)
            self.manager.detach(session_id, queue)
            if marker in snapshot:
                return snapshot
            await asyncio.sleep(0.02)
        self.fail(f"terminal output did not contain {marker!r}: {snapshot!r}")

    async def test_output_is_buffered_and_replayed(self) -> None:
        session = self.manager.create("Test")
        snapshot = await self.wait_for_output(session.id, b"ready-marker")
        self.assertIn(b"ready-marker", snapshot)
        self.assertTrue(session.active)

    async def test_local_workspace_root_is_the_shell_working_directory(self) -> None:
        workspace = Path(self.directory.name) / "project"
        workspace.mkdir()
        session = self.manager.create("Project", workspace_root=str(workspace))
        await self.wait_for_output(session.id, b"ready-marker")
        self.manager.write(session.id, b"pwd\r")
        snapshot = await self.wait_for_output(session.id, str(workspace).encode())

        self.assertEqual(str(workspace), session.cwd)
        self.assertEqual(str(workspace), session.workspace_current_path)
        self.assertIn(str(workspace).encode(), snapshot)

    async def test_sessions_have_unique_random_ids(self) -> None:
        first = self.manager.create("First")
        second = self.manager.create("Second")
        self.assertNotEqual(first.id, second.id)
        self.assertRegex(first.id, re.compile(r"^[a-f0-9]{32}$"))
        self.assertRegex(second.id, re.compile(r"^[a-f0-9]{32}$"))

    async def test_sessions_are_filtered_by_owner(self) -> None:
        alice = self.manager.create("Alice", owner="alice")
        bob = self.manager.create("Bob", owner="bob")

        self.assertEqual([alice.id], [item["id"] for item in self.manager.list("alice")])
        self.assertIs(alice, self.manager.get_for_owner(alice.id, "alice"))
        self.assertIsNone(self.manager.get_for_owner(bob.id, "alice"))

    async def test_decodes_split_artifact_osc_payload(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        self.manager.artifact_callback = lambda session, event: events.append(
            (session.id, event)
        )
        session = TerminalSession(
            id="artifact-session",
            name="Artifact",
            pid=-1,
            fd=-1,
            command="shell",
            cwd=self.directory.name,
            owner="alice",
        )
        payload = base64.urlsafe_b64encode(
            json.dumps({"type": "created", "path": "charts/result.png"}).encode()
        ).decode().rstrip("=")
        marker = f"\x1b]633;artifact;{payload}\x07".encode()

        self.manager._discover_artifacts(session, marker[:13])
        self.manager._discover_artifacts(session, marker[13:])

        self.assertEqual(1, len(events))
        self.assertEqual(session.id, events[0][0])
        self.assertEqual("charts/result.png", events[0][1]["path"])
        self.assertEqual("terminal-osc", events[0][1]["source"])

    async def test_decodes_tmux_safe_artifact_line_marker(self) -> None:
        events: list[dict[str, object]] = []
        self.manager.artifact_callback = lambda _session, event: events.append(event)
        session = TerminalSession(
            id="line-artifact-session",
            name="Artifact",
            pid=-1,
            fd=-1,
            command="shell",
            cwd=self.directory.name,
            owner="alice",
        )
        payload = base64.urlsafe_b64encode(
            json.dumps({"type": "modified", "path": "output/report.pdf"}).encode()
        ).decode().rstrip("=")
        marker = (
            f"__AGENTSERVER_ARTIFACT__:{payload}:AGENTSERVER_END__\r\x1b[2K"
        ).encode()

        self.manager._discover_artifacts(session, marker[:21])
        self.manager._discover_artifacts(session, marker[21:])
        self.manager._discover_artifacts(session, b"ordinary output")

        self.assertEqual(1, len(events))
        self.assertEqual("output/report.pdf", events[0]["path"])
        self.assertEqual("terminal-marker", events[0]["source"])

    async def test_overloaded_subscriber_gets_a_gap_marker_not_a_hole(self) -> None:
        """A backed-up client must not silently receive a spliced VT stream."""
        session = self.manager.create("Backpressure")
        _snapshot, queue = self.manager.attach(session.id)
        healthy_snapshot, healthy = self.manager.attach(session.id)
        del _snapshot, healthy_snapshot
        try:
            while not queue.full():
                queue.put_nowait(b"queued")
            queued_before = queue.qsize()

            self.manager._broadcast(session, b"\x1b[31mlate\x1b[0m")

            # Everything pending is discarded in favour of one explicit marker,
            # so nothing can mistake a hole for continuous output.
            self.assertEqual(1, queue.qsize())
            self.assertIs(STREAM_GAP, queue.get_nowait())
            self.assertGreater(queued_before, 1)

            # The slow client's overflow must not cost a healthy subscriber
            # either chunk. Live PTY output may be interleaved, so assert on
            # order rather than on exact queue contents.
            self.manager._broadcast(session, b"still fine")
            delivered: list[bytes] = []
            while not healthy.empty():
                delivered.append(healthy.get_nowait())
            self.assertNotIn(STREAM_GAP, delivered)
            self.assertLess(
                delivered.index(b"\x1b[31mlate\x1b[0m"),
                delivered.index(b"still fine"),
            )
        finally:
            self.manager.detach(session.id, queue)
            self.manager.detach(session.id, healthy)

    async def test_session_cleanup_never_signals_special_pids(self) -> None:
        with patch("app.terminal.subprocess.run") as process_scan, patch(
            "app.terminal.os.kill"
        ) as kill:
            for unsafe_pid in (-1, 0, 1):
                self.assertEqual([], self.manager._session_pids(unsafe_pid))
                self.manager._signal_session(unsafe_pid, signal.SIGTERM)

        process_scan.assert_not_called()
        kill.assert_not_called()

    async def test_session_cleanup_does_not_signal_unowned_positive_pid(self) -> None:
        with patch("app.terminal.os.waitpid") as waitpid, patch(
            "app.terminal.subprocess.run"
        ) as process_scan, patch("app.terminal.os.kill") as kill:
            self.manager._signal_session(987654, signal.SIGTERM)

        waitpid.assert_not_called()
        process_scan.assert_not_called()
        kill.assert_not_called()

    async def test_session_cleanup_signals_registered_live_child_tree(self) -> None:
        self.manager._owned_pids.add(123)
        with patch("app.terminal.os.waitpid", return_value=(0, 0)), patch.object(
            self.manager, "_session_pids", return_value=[123, 124]
        ), patch("app.terminal.os.kill") as kill:
            self.manager._signal_session(123, signal.SIGTERM)

        self.assertEqual(
            [(124, signal.SIGTERM), (123, signal.SIGTERM)],
            [call.args for call in kill.call_args_list],
        )

    async def test_delete_safely_discards_active_sentinel_session(self) -> None:
        session = TerminalSession(
            id="sentinel-session",
            name="Sentinel",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
        )
        self.manager.sessions[session.id] = session

        with patch.object(self.manager, "_signal_session") as signal_session, patch(
            "app.terminal.os.waitpid"
        ) as waitpid, patch("app.terminal.os.close") as close:
            self.assertTrue(await self.manager.delete(session.id))

        signal_session.assert_not_called()
        waitpid.assert_not_called()
        close.assert_not_called()
        self.assertFalse(session.active)
        self.assertIsNone(self.manager.get(session.id))

    async def test_direct_process_session_keeps_device_metadata(self) -> None:
        session = self.manager.create_process(
            name="Remote device",
            argv=["/bin/sh", "-c", "printf 'ssh-ready-marker\\n'; sleep 30"],
            device_id="device-001",
            device_name="Device 001",
            remote_port=20001,
        )
        snapshot = await self.wait_for_output(session.id, b"ssh-ready-marker")
        self.assertIn(b"ssh-ready-marker", snapshot)
        self.assertEqual("ssh", session.kind)
        self.assertEqual("device-001", session.device_id)
        self.assertEqual(20001, session.remote_port)

    async def test_detects_local_web_services_from_terminal_output(self) -> None:
        session = self.manager.create_process(
            name="Remote device",
            argv=[
                "/bin/sh",
                "-c",
                "printf '\\033[32mVITE ready\\033[0m\\nLocal: http://localhost:5173/app\\n'; sleep 30",
            ],
            device_id="device-001",
            device_name="Device 001",
            remote_port=20001,
        )
        await self.wait_for_output(session.id, b"localhost:5173")

        self.assertEqual([], session.as_dict()["services"])
        service = session.services[5173]
        self.manager.update_service_status(session.id, 5173, online=True)
        services = session.as_dict()["services"]
        self.assertEqual(1, len(services))
        self.assertEqual(5173, services[0]["port"])
        self.assertEqual("Vite", services[0]["label"])
        self.assertEqual("online", services[0]["status"])
        self.assertEqual("online", service.status)

    async def test_ignores_local_urls_without_service_context(self) -> None:
        session = TerminalSession(
            id="quiet-discovery",
            name="Remote device",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=0,
        )
        self.manager.sessions[session.id] = session
        self.manager._append(
            session,
            b"curl http://localhost:3000/api\n"
            b"documentation: http://127.0.0.1:8080/\n"
            b"server configuration value: 9000\n",
        )
        self.assertEqual({}, session.services)

    async def test_detects_plain_and_chinese_health_check_urls(self) -> None:
        session = TerminalSession(
            id="chinese-health-output",
            name="Remote device",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=0,
        )
        self.manager.sessions[session.id] = session
        self.manager.service_discovery_event.clear()
        self.manager._append(
            session,
            "• 前端和后端服务均正常：\n"
            "  - 前端 http://localhost:3000/：HTTP 200\n"
            "  - 后端 http://localhost:18080/api/v1/healthz：HTTP 200\n"
            "  - PostgreSQL：db: ok\n".encode(),
        )

        self.assertEqual([3000, 18080], list(session.services))
        self.assertTrue(self.manager.service_discovery_event.is_set())
        self.assertEqual("前端服务", session.services[3000].label)
        self.assertEqual("后端服务", session.services[18080].label)

    async def test_detects_plain_local_url_but_keeps_references_quiet(self) -> None:
        session = TerminalSession(
            id="plain-local-url",
            name="Remote device",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=0,
        )
        self.manager.sessions[session.id] = session
        self.manager._append(session, b"http://localhost:4321/\n")
        self.manager._append(session, b"example: http://localhost:4322/\n")

        self.assertEqual([4321], list(session.services))

    async def test_ignores_public_urls_and_tracks_service_lifecycle(self) -> None:
        session = TerminalSession(
            id="service-session",
            name="Remote device",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=0,
        )
        self.manager.sessions[session.id] = session
        self.manager._append(
            session,
            b"docs https://example.com:443 then Server running on 0.0.0.0:3000\n"
            b"Serving HTTP on 0.0.0.0 port 8000\n",
        )
        self.assertEqual([3000, 8000], list(session.services))

        service, became_offline = self.manager.update_service_status(
            session.id, 3000, online=True
        )
        self.assertEqual("online", service.status)
        self.assertFalse(became_offline)
        _, became_offline = self.manager.update_service_status(
            session.id, 3000, online=False, error="refused", failure_threshold=2
        )
        self.assertEqual("online", service.status)
        self.assertFalse(became_offline)
        _, became_offline = self.manager.update_service_status(
            session.id, 3000, online=False, error="refused", failure_threshold=2
        )
        self.assertEqual("offline", service.status)
        self.assertTrue(became_offline)
        self.assertNotIn(service, [item for _session, item in self.manager.service_candidates()])
        self.assertEqual([], session.as_dict()["services"])

        session.exited_at = None
        try:
            self.manager._append(session, b"Server ready at http://127.0.0.1:3000/\n")
            self.assertEqual("offline", service.status)
            self.assertNotIn(service, [item for _session, item in self.manager.service_candidates()])

            service.retry_after = 0
            self.manager._append(session, b"Server ready at http://127.0.0.1:3000/\n")
            self.assertEqual("checking", service.status)
            self.assertIn(service, [item for _session, item in self.manager.service_candidates()])
        finally:
            session.exited_at = 0

    async def test_caps_unverified_service_candidates(self) -> None:
        session = TerminalSession(
            id="candidate-cap",
            name="Remote device",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=0,
        )
        self.manager.sessions[session.id] = session
        for port in range(4100, 4112):
            self.manager._append(
                session, f"Local: http://localhost:{port}/\n".encode()
            )
        self.assertEqual(8, len(session.services))
        self.assertEqual([], session.as_dict()["services"])

    async def test_process_scan_discovers_assigns_and_removes_service(self) -> None:
        older = TerminalSession(
            id="older-terminal",
            name="Older",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=None,
            last_activity_at=10,
        )
        newer = TerminalSession(
            id="newer-terminal",
            name="Newer",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=None,
            last_activity_at=20,
        )
        self.manager.sessions = {older.id: older, newer.id: newer}
        try:
            removed = self.manager.sync_process_listeners(
                "device-001", [ListeningProcess(3000, 123, "node")]
            )
            self.assertEqual([], removed)
            self.assertNotIn(3000, older.services)
            service = newer.services[3000]
            self.assertEqual("process", service.source)
            self.assertEqual("Node.js", service.label)
            self.manager.update_service_status(newer.id, 3000, online=True)
            self.assertEqual(
                [3000], [item["port"] for item in newer.as_dict()["services"]]
            )

            self.assertEqual([], self.manager.sync_process_listeners("device-001", []))
            self.assertIn(3000, newer.services)
            self.assertEqual(
                [(newer.id, 3000)],
                self.manager.sync_process_listeners("device-001", []),
            )
            self.assertEqual("offline", newer.services[3000].status)
            self.assertEqual("监听进程已停止", newer.services[3000].error)

            self.manager.sync_process_listeners(
                "device-001", [ListeningProcess(3000, 123, "node")]
            )
            self.assertEqual("checking", newer.services[3000].status)
        finally:
            older.exited_at = older.exited_at or 1
            newer.exited_at = newer.exited_at or 1

    async def test_process_scan_enriches_output_service_without_reassigning_it(self) -> None:
        owner = TerminalSession(
            id="output-owner",
            name="Output owner",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=None,
            last_activity_at=10,
        )
        newer = TerminalSession(
            id="newer-terminal",
            name="Newer",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=None,
            last_activity_at=20,
        )
        self.manager.sessions = {owner.id: owner, newer.id: newer}
        try:
            self.manager._append(owner, b"http://localhost:5173/\n")

            self.manager.sync_process_listeners(
                "device-001", [ListeningProcess(5173, 88, "vite")]
            )
            self.assertIn(5173, owner.services)
            self.assertNotIn(5173, newer.services)
            self.assertEqual("hybrid", owner.services[5173].source)
            self.assertEqual("Vite", owner.services[5173].label)

            self.manager.sync_process_listeners("device-001", [], missing_threshold=1)
            self.assertIn(5173, owner.services)
            self.assertEqual("offline", owner.services[5173].status)
            self.assertEqual("hybrid", owner.services[5173].source)
        finally:
            owner.exited_at = owner.exited_at or 1
            newer.exited_at = newer.exited_at or 1

    async def test_process_scan_deduplicates_same_port_across_terminals(self) -> None:
        older = TerminalSession(
            id="older-owner",
            name="Older",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=None,
            last_activity_at=10,
        )
        newer = TerminalSession(
            id="newer-owner",
            name="Newer",
            pid=-1,
            fd=-1,
            command="ssh",
            cwd=self.directory.name,
            kind="ssh",
            device_id="device-001",
            exited_at=None,
            last_activity_at=20,
        )
        older.services[3000] = DetectedService(
            3000, "http://localhost:3000/", "Web 服务 :3000", status="online", source="process"
        )
        newer.services[3000] = DetectedService(
            3000, "http://localhost:3000/", "Vite", status="online", source="output"
        )
        self.manager.sessions = {older.id: older, newer.id: newer}
        try:
            removed = self.manager.sync_process_listeners(
                "device-001", [ListeningProcess(3000, 55, "vite")]
            )
            self.assertEqual([(older.id, 3000)], removed)
            self.assertNotIn(3000, older.services)
            self.assertEqual("hybrid", newer.services[3000].source)
        finally:
            older.exited_at = 1
            newer.exited_at = 1

    async def test_multiple_clients_receive_the_same_live_output(self) -> None:
        session = self.manager.create("Shared")
        await self.wait_for_output(session.id, b"ready-marker")
        _, first_client = self.manager.attach(session.id)
        _, second_client = self.manager.attach(session.id)
        try:
            self.manager.write(session.id, b"printf 'multi-client-marker\\n'\r")

            async def receive_marker(queue: asyncio.Queue[bytes]) -> bytes:
                output = bytearray()
                while b"multi-client-marker" not in output:
                    output.extend(await asyncio.wait_for(queue.get(), timeout=2))
                return bytes(output)

            first_output, second_output = await asyncio.gather(
                receive_marker(first_client), receive_marker(second_client)
            )
            self.assertIn(b"multi-client-marker", first_output)
            self.assertIn(b"multi-client-marker", second_output)
        finally:
            self.manager.detach(session.id, first_client)
            self.manager.detach(session.id, second_client)

    async def test_interrupting_initial_command_returns_to_usable_shell(self) -> None:
        await self.manager.close()
        self.manager = TerminalManager(
            command="sleep 30",
            cwd=self.directory.name,
            shell="/bin/sh",
            scrollback_bytes=64 * 1024,
        )
        session = self.manager.create("Interruptible")

        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            if len(self.manager._session_pids(session.pid)) >= 2:
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("initial command did not start as a child of the interactive shell")

        self.manager.write(session.id, b"\x03")
        await asyncio.sleep(0.1)
        self.manager.write(session.id, b"printf 'after-interrupt-marker\\n'\r")
        snapshot = await self.wait_for_output(session.id, b"after-interrupt-marker")

        self.assertIn(b"after-interrupt-marker", snapshot)
        self.assertTrue(session.active)

    async def test_delete_removes_session_and_stops_process(self) -> None:
        session = self.manager.create("Disposable")
        await asyncio.sleep(0.2)
        process_ids = self.manager._session_pids(session.pid)
        self.assertTrue(await self.manager.delete(session.id))
        self.assertIsNone(self.manager.get(session.id))
        self.assertFalse(session.active)
        self.assertFalse(await self.manager.delete(session.id))
        for process_id in process_ids:
            with self.assertRaises(ProcessLookupError):
                os.kill(process_id, 0)
class TerminalStoreTests(unittest.TestCase):
    def test_session_metadata_round_trip_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TerminalStore(Path(directory) / "terminals.db")
            session = TerminalSession(
                id="session-id",
                name="Persistent device",
                pid=-1,
                fd=-1,
                command="ssh operator@example",
                cwd=directory,
                kind="ssh",
                device_id="device-001",
                device_name="Device 001",
                remote_port=20001,
                owner="alice",
                workspace_kind="sftp",
                workspace_root="/srv/project",
                workspace_platform="posix",
                tmux_name="agentserver-session-id",
                created_at=1234.5,
            )
            store.save(session)

            rows = store.list()
            self.assertEqual(1, len(rows))
            self.assertEqual("session-id", rows[0]["id"])
            self.assertEqual("agentserver-session-id", rows[0]["tmux_name"])
            self.assertEqual("device-001", rows[0]["device_id"])
            self.assertEqual(20001, rows[0]["remote_port"])
            self.assertEqual("alice", rows[0]["owner"])
            self.assertEqual("sftp", rows[0]["workspace_kind"])
            self.assertEqual("/srv/project", rows[0]["workspace_root"])

            store.delete(session.id)
            self.assertEqual([], store.list())

    def test_existing_schema_adds_owner_and_workspace_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE terminal_sessions (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        command TEXT NOT NULL,
                        cwd TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        device_id TEXT,
                        device_name TEXT,
                        remote_port INTEGER,
                        tmux_name TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO terminal_sessions
                    VALUES ('legacy', 'Legacy', 'ssh host', '/tmp', 'ssh',
                            'device-1', 'Device', 22001, 'agentserver-legacy', 1.0)
                    """
                )

            row = TerminalStore(database).list()[0]
            self.assertEqual("", row["owner"])
            self.assertEqual("local", row["workspace_kind"])
            self.assertEqual("", row["workspace_root"])
            self.assertEqual("posix", row["workspace_platform"])


class TmuxTerminalManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmux_artifact_fifo_is_machine_only_and_cleaned_up(self) -> None:
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.terminal.shutil.which", return_value="/usr/bin/tmux"
        ), patch.object(TerminalManager, "_restore_tmux_sessions"):
            manager = TerminalManager(
                command="",
                cwd=directory,
                shell="/bin/sh",
                backend="tmux",
                database_path=Path(directory) / "terminals.db",
                tmux_socket=Path(directory) / "tmux.sock",
                artifact_callback=lambda _session, event: events.append(event),
            )
            session = TerminalSession(
                id="artifact-pipe-session",
                name="Artifact pipe",
                pid=-1,
                fd=-1,
                command="/bin/sh -l",
                cwd=directory,
                tmux_name="agentserver-artifact-pipe-session",
            )
            manager.sessions[session.id] = session
            payload = base64.urlsafe_b64encode(
                json.dumps({"type": "created", "path": "build/chart.png"}).encode()
            ).decode().rstrip("=")
            marker = (
                f"__AGENTSERVER_ARTIFACT__:{payload}:AGENTSERVER_END__\r\x1b[2K"
            ).encode()

            with patch.object(manager, "_tmux_run") as tmux_run, patch.object(
                manager, "_broadcast"
            ) as broadcast:
                manager._start_tmux_artifact_capture(session)
                pipe_path = Path(session.artifact_pipe_path)
                self.assertTrue(stat.S_ISFIFO(pipe_path.stat().st_mode))
                os.write(session.artifact_fd, b"ordinary shell output\r\n" + marker)

                deadline = asyncio.get_running_loop().time() + 2
                while not events and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)

                self.assertTrue(events, "artifact FIFO did not deliver the marker")
                self.assertEqual("build/chart.png", events[0]["path"])
                self.assertEqual("terminal-marker", events[0]["source"])
                self.assertEqual([], list(session.chunks))
                broadcast.assert_not_called()

                manager._stop_tmux_artifact_capture(session)
                self.assertEqual(-1, session.artifact_fd)
                self.assertFalse(pipe_path.exists())

            start_call = tmux_run.call_args_list[0]
            self.assertEqual(("pipe-pane", "-t", session.tmux_name), start_call.args[:3])
            self.assertIn("exec cat >", start_call.args[3])
            await manager.close()

    async def test_tmux_server_uses_configured_shell_for_nologin_service_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.terminal.shutil.which", return_value="/usr/bin/tmux"
        ), patch.object(TerminalManager, "_restore_tmux_sessions"):
            manager = TerminalManager(
                command="",
                cwd=directory,
                shell="/bin/sh",
                backend="tmux",
                database_path=Path(directory) / "terminals.db",
                tmux_socket=Path(directory) / "tmux.sock",
            )
            with patch.object(manager, "_tmux_run") as tmux_run:
                tmux_run.return_value.returncode = 0
                manager._configure_tmux_server()

            tmux_run.assert_any_call("set-option", "-g", "default-shell", "/bin/sh")
            tmux_run.assert_any_call("set-option", "-g", "mouse", "off")
            tmux_run.assert_any_call(
                "set-option",
                "-s",
                "-g",
                "terminal-overrides",
                "xterm-256color:smcup@:rmcup@",
            )

    async def test_existing_tmux_session_is_restored_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "terminals.db"
            stored = TerminalSession(
                id="restored-id",
                name="Restored",
                pid=-1,
                fd=-1,
                command="ssh operator@example",
                cwd=directory,
                kind="ssh",
                device_id="device-001",
                device_name="Device 001",
                remote_port=20001,
                tmux_name="agentserver-restored-id",
                created_at=1234.5,
            )
            TerminalStore(database_path).save(stored)

            with patch("app.terminal.shutil.which", return_value="/usr/bin/tmux"), patch.object(
                TerminalManager, "_configure_tmux_server"
            ), patch.object(
                TerminalManager, "_tmux_session_exists", return_value=True
            ), patch.object(
                TerminalManager, "_capture_tmux_history", return_value=b"restored output\r\n"
            ), patch.object(
                TerminalManager, "_refresh_tmux_state"
            ), patch.object(
                TerminalManager, "_spawn_tmux_client"
            ):
                manager = TerminalManager(
                    command="",
                    cwd=directory,
                    shell="/bin/sh",
                    backend="tmux",
                    database_path=database_path,
                    tmux_socket=Path(directory) / "tmux.sock",
                    default_owner="admin",
                )

            restored = manager.sessions[stored.id]
            self.assertEqual("agentserver-restored-id", restored.tmux_name)
            self.assertEqual("device-001", restored.device_id)
            self.assertEqual(20001, restored.remote_port)
            self.assertEqual("admin", restored.owner)
            self.assertEqual("sftp", restored.workspace_kind)
            self.assertEqual(".", restored.workspace_root)
            self.assertIn(b"restored output", b"".join(restored.chunks))

    async def test_close_detaches_without_deleting_persistent_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.terminal.shutil.which", return_value="/usr/bin/tmux"
        ), patch.object(TerminalManager, "_restore_tmux_sessions"):
            manager = TerminalManager(
                command="",
                cwd=directory,
                shell="/bin/sh",
                backend="tmux",
                database_path=Path(directory) / "terminals.db",
                tmux_socket=Path(directory) / "tmux.sock",
            )
            session = TerminalSession(
                id="persistent-id",
                name="Persistent",
                pid=-1,
                fd=-1,
                command="/bin/sh -l",
                cwd=directory,
                tmux_name="agentserver-persistent-id",
            )
            manager.sessions[session.id] = session
            manager.store.save(session)

            with patch.object(manager, "_close_client") as close_client:
                await manager.close()

            close_client.assert_called_once_with(session)
            self.assertIs(session, manager.sessions.get(session.id))
            self.assertEqual(1, len(manager.store.list()))

    async def test_delete_kills_tmux_and_removes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.terminal.shutil.which", return_value="/usr/bin/tmux"
        ), patch.object(TerminalManager, "_restore_tmux_sessions"):
            manager = TerminalManager(
                command="",
                cwd=directory,
                shell="/bin/sh",
                backend="tmux",
                database_path=Path(directory) / "terminals.db",
                tmux_socket=Path(directory) / "tmux.sock",
            )
            session = TerminalSession(
                id="disposable-id",
                name="Disposable",
                pid=-1,
                fd=-1,
                command="/bin/sh -l",
                cwd=directory,
                tmux_name="agentserver-disposable-id",
            )
            manager.sessions[session.id] = session
            manager.store.save(session)

            with patch.object(manager, "_close_client"), patch.object(
                manager, "_tmux_run"
            ) as tmux_run:
                self.assertTrue(await manager.delete(session.id))

            tmux_run.assert_called_once_with(
                "kill-session", "-t", "agentserver-disposable-id", check=False
            )
            self.assertIsNone(manager.sessions.get(session.id))
            self.assertEqual([], manager.store.list())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    async def test_real_tmux_pipe_captures_immediately_erased_burst(self) -> None:
        tmux_binary = shutil.which("tmux")
        assert tmux_binary is not None
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "integration.sock"
            await asyncio.to_thread(
                subprocess.run,
                [
                    tmux_binary,
                    "-S",
                    str(socket_path),
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-s",
                    "bootstrap",
                    "sleep 30",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            manager: TerminalManager | None = None
            session: TerminalSession | None = None
            events: list[dict[str, object]] = []
            try:
                manager = TerminalManager(
                    command="",
                    cwd=directory,
                    shell="/bin/sh",
                    backend="tmux",
                    database_path=Path(directory) / "terminals.db",
                    tmux_binary=tmux_binary,
                    tmux_socket=socket_path,
                    artifact_callback=lambda _session, event: events.append(event),
                )
                session = manager.create("Artifact integration")
                self.assertGreaterEqual(session.artifact_fd, 0)

                # Establish a real rendered-channel barrier before the burst.
                # tmux attach starts asynchronously; a fixed sleep can let the raw
                # pipe win before the attach PTY has delivered its initial redraw.
                # The octal command form keeps the sentinel out of any input echo,
                # so observing it proves the command ran and the client read it.
                ready_marker = b"attach-client-ready"
                ready_escape = "".join(f"\\{byte:03o}" for byte in ready_marker) + "\\n"
                manager._tmux_run(
                    "send-keys",
                    "-t",
                    session.tmux_name or "",
                    "-l",
                    f"stty -echo; printf {shlex.quote(ready_escape)}",
                )
                manager._tmux_run("send-keys", "-t", session.tmux_name or "", "Enter")
                ready_deadline = asyncio.get_running_loop().time() + 5
                rendered = b""
                while asyncio.get_running_loop().time() < ready_deadline:
                    rendered = b"".join(session.chunks)
                    if ready_marker in rendered:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn(ready_marker, rendered)
                session.chunks.clear()
                session.buffer_size = 0

                payload = base64.urlsafe_b64encode(
                    json.dumps(
                        {"type": "created", "path": "artifacts/erased.png"}
                    ).encode()
                ).decode().rstrip("=")
                marker = (
                    f"__AGENTSERVER_ARTIFACT__:{payload}:AGENTSERVER_END__"
                )
                burst = 80
                # Keep the exact-once normal-output probe after the long burst.
                # Before it, tmux may coalesce redraws or scroll the earlier line;
                # the separate FIFO unit test already proves raw bytes are not
                # broadcast, while this probe exercises the real attach path.
                script = (
                    "i=0; while [ \"$i\" -lt "
                    f"{burst} ]; do printf '%s\\r\\033[2K' {shlex.quote(marker)}; "
                    "i=$((i+1)); done; "
                    "printf '%s\\n' artifact-burst-complete; "
                    "printf '%s\\n' ordinary-output-once"
                )
                manager._tmux_run(
                    "send-keys", "-t", session.tmux_name or "", "-l", script
                )
                manager._tmux_run("send-keys", "-t", session.tmux_name or "", "Enter")

                deadline = asyncio.get_running_loop().time() + 5
                rendered = b""
                while asyncio.get_running_loop().time() < deadline:
                    rendered = b"".join(session.chunks)
                    # The raw pipe and the rendered attach client are independent;
                    # wait for the complete success condition on both channels.
                    if (
                        len(events) >= burst
                        and b"artifact-burst-complete" in rendered
                        and b"ordinary-output-once" in rendered
                    ):
                        break
                    await asyncio.sleep(0.02)

                self.assertEqual(burst, len(events))
                self.assertTrue(
                    all(event["path"] == "artifacts/erased.png" for event in events)
                )
                self.assertTrue(
                    all(event["source"] == "terminal-marker" for event in events)
                )
                self.assertIn(b"artifact-burst-complete", rendered)
                self.assertEqual(
                    1,
                    rendered.count(b"ordinary-output-once"),
                    rendered[-4096:],
                )
            finally:
                try:
                    if manager is not None and session is not None:
                        await manager.delete(session.id)
                finally:
                    try:
                        if manager is not None:
                            await manager.close()
                    finally:
                        await asyncio.to_thread(
                            subprocess.run,
                            [tmux_binary, "-S", str(socket_path), "kill-server"],
                            check=False,
                            capture_output=True,
                        )


class TmuxPaneStateSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """The batched `list-panes -a` snapshot replaces 2N per-session tmux execs.

    tmux is not required: `_tmux_run` is replaced by a recorder that returns real
    CompletedProcess values, so the snapshot parsing and the fallback contract are
    both exercised without a tmux server.
    """

    def _manager(self, directory: str) -> TerminalManager:
        with patch("app.terminal.shutil.which", return_value="/usr/bin/tmux"), patch.object(
            TerminalManager, "_restore_tmux_sessions"
        ):
            # No artifact_callback: _start_tmux_artifact_capture then early-returns,
            # keeping these tests to the liveness logic under test.
            return TerminalManager(
                command="",
                cwd=directory,
                shell="/bin/sh",
                backend="tmux",
                database_path=Path(directory) / "terminals.db",
                tmux_socket=Path(directory) / "tmux.sock",
            )

    @staticmethod
    def _recorder(
        calls: list[tuple[str, ...]], stdout: str = "", returncode: int = 0
    ):
        def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(
                list(arguments), returncode, stdout=stdout, stderr=""
            )

        return run

    @staticmethod
    def _session(session_id: str) -> TerminalSession:
        return TerminalSession(
            id=session_id,
            name=session_id,
            pid=-1,
            fd=-1,
            command="/bin/sh -l",
            cwd="/tmp",
            tmux_name=f"agentserver-{session_id}",
        )

    async def test_snapshot_parses_every_pane_and_caches_within_ttl(self) -> None:
        stdout = (
            "agentserver-alive\t0\t\t1\n"
            "agentserver-dead\t1\t137\t0\n"
            "malformed-line-without-fields\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            calls: list[tuple[str, ...]] = []
            with patch.object(manager, "_tmux_run", self._recorder(calls, stdout)):
                states = manager._tmux_pane_states()
                self.assertEqual(
                    {
                        "agentserver-alive": (False, "", True),
                        "agentserver-dead": (True, "137", False),
                    },
                    states,
                )
                self.assertEqual(1, len(calls))
                self.assertEqual("list-panes", calls[0][0])
                self.assertEqual("-a", calls[0][1])

                # Inside the TTL the snapshot is reused without a second exec.
                self.assertEqual(states, manager._tmux_pane_states(max_age=60.0))
                self.assertEqual(1, len(calls))
                # max_age=0 always re-queries.
                manager._tmux_pane_states()
                self.assertEqual(2, len(calls))
            await manager.close()

    async def test_failed_snapshot_falls_back_instead_of_killing_every_session(self) -> None:
        """A transient tmux failure must not look like "all sessions died"."""
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            session = self._session("alive")
            manager.sessions[session.id] = session
            calls: list[tuple[str, ...]] = []

            def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                calls.append(arguments)
                if arguments[0] == "list-panes":
                    return subprocess.CompletedProcess(
                        list(arguments), 1, stdout="", stderr="tmux unavailable"
                    )
                if arguments[0] == "display-message":
                    return subprocess.CompletedProcess(
                        list(arguments), 0, stdout="0::0\n", stderr=""
                    )
                return subprocess.CompletedProcess(
                    list(arguments), 0, stdout="", stderr=""
                )

            with patch.object(manager, "_tmux_run", run):
                self.assertIsNone(manager._tmux_pane_states())
                manager.list()
            self.assertTrue(session.active, "a failed snapshot must not mark sessions dead")
            # Fallback path: has-session + display-message per session.
            self.assertIn("has-session", [call[0] for call in calls])
            await manager.close()

    async def test_list_refreshes_many_sessions_with_one_tmux_exec(self) -> None:
        names = [f"session-{index}" for index in range(5)]
        stdout = "".join(f"agentserver-{name}\t0\t\t0\n" for name in names)
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            for name in names:
                manager.sessions[name] = self._session(name)
            calls: list[tuple[str, ...]] = []
            with patch.object(manager, "_tmux_run", self._recorder(calls, stdout)):
                listed = manager.list()
            self.assertEqual(5, len(listed))
            self.assertTrue(all(item["active"] for item in listed))
            self.assertEqual(
                ["list-panes"],
                [call[0] for call in calls],
                "refreshing 5 sessions must cost exactly one tmux exec",
            )
            await manager.close()

    async def test_snapshot_marks_dead_and_missing_sessions_exited(self) -> None:
        stdout = "agentserver-dead\t1\t137\t0\n"
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            dead = self._session("dead")
            vanished = self._session("vanished")
            manager.sessions[dead.id] = dead
            manager.sessions[vanished.id] = vanished
            calls: list[tuple[str, ...]] = []
            with patch.object(manager, "_tmux_run", self._recorder(calls, stdout)):
                manager.list()
            self.assertFalse(dead.active)
            self.assertEqual(137, dead.return_code)
            self.assertFalse(vanished.active, "a pane absent from the snapshot has died")
            self.assertEqual(-1, vanished.return_code)
            self.assertEqual(["list-panes"], [call[0] for call in calls])
            await manager.close()

    async def test_session_mutating_commands_invalidate_the_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(directory)
            calls: list[tuple[str, ...]] = []

            def run(
                command: list[str], *, capture_output: bool, text: bool
            ) -> subprocess.CompletedProcess[str]:
                arguments = tuple(command[3:])
                calls.append(arguments)
                stdout = (
                    "agentserver-a\t0\t\t0\n"
                    if arguments and arguments[0] == "list-panes"
                    else ""
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout=stdout, stderr=""
                )

            with patch("app.terminal.subprocess.run", side_effect=run):
                manager._tmux_pane_states()
                self.assertIsNotNone(manager._tmux_states_cache)
                manager._tmux_run("kill-session", "-t", "agentserver-a", check=False)
                self.assertIsNone(
                    manager._tmux_states_cache,
                    "kill-session changes which sessions exist",
                )
                manager._tmux_pane_states()
                manager._tmux_run("new-session", "-d", "-s", "agentserver-b")
                self.assertIsNone(manager._tmux_states_cache)
                # Read-only commands keep the snapshot.
                manager._tmux_pane_states()
                manager._tmux_run("display-message", "-p", "x", check=False)
                self.assertIsNotNone(manager._tmux_states_cache)
            await manager.close()


if __name__ == "__main__":
    unittest.main()
