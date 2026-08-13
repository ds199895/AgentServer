import asyncio
import os
import re
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.terminal import (
    TerminalManager,
    TerminalSession,
    TerminalStore,
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

    async def test_sessions_have_unique_random_ids(self) -> None:
        first = self.manager.create("First")
        second = self.manager.create("Second")
        self.assertNotEqual(first.id, second.id)
        self.assertRegex(first.id, re.compile(r"^[a-f0-9]{32}$"))
        self.assertRegex(second.id, re.compile(r"^[a-f0-9]{32}$"))

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

            store.delete(session.id)
            self.assertEqual([], store.list())


class TmuxTerminalManagerTests(unittest.IsolatedAsyncioTestCase):
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
                )

            restored = manager.sessions[stored.id]
            self.assertEqual("agentserver-restored-id", restored.tmux_name)
            self.assertEqual("device-001", restored.device_id)
            self.assertEqual(20001, restored.remote_port)
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


if __name__ == "__main__":
    unittest.main()
