from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("ADMIN_PASSWORD", "test-only-password")
os.environ.setdefault(
    "DATA_DIR", tempfile.mkdtemp(prefix="agentserver-main-lifecycle-import-")
)
os.environ.setdefault("PREVIEW_PUBLIC_ORIGIN", "http://preview.test")

from fastapi import FastAPI, HTTPException

from app.execution import ExecutionStore
from app.execution.service import ExecutionService
from app.main import (
    CreateTerminalBody,
    TerminalExecutionLifecycle,
    create_terminal,
    lifespan,
    managed_terminal_environment,
    reconcile_execution_state,
)
from app.terminal import TerminalSession


class ManagedTerminalEnvironmentTests(unittest.TestCase):
    def test_local_control_discovery_binds_the_server_process_incarnation(self) -> None:
        identity = SimpleNamespace(pid=321, start_time_ticks=654)
        with patch.dict(
            os.environ,
            {"AGENTSERVER_CONTROL_SOCKET": "/tmp/control.sock"},
            clear=False,
        ), patch("app.main.os.getpid", return_value=321), patch(
            "app.main.read_linux_process_identity", return_value=identity
        ):
            environment = managed_terminal_environment()

        self.assertEqual("/tmp/control.sock", environment["AGENTSERVER_CONTROL_SOCKET"])
        self.assertEqual("local-broker", environment["AGENTSERVER_CONTROL_TRANSPORT"])
        self.assertEqual("321", environment["AGENTSERVER_CONTROL_SERVER_PID"])
        self.assertEqual(
            "654", environment["AGENTSERVER_CONTROL_SERVER_START_TIME"]
        )

    def test_remote_bridge_path_does_not_claim_the_agentserver_process(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENTSERVER_REMOTE_CONTROL_SOCKET": "/run/bridge.sock"},
            clear=False,
        ), patch("app.main.read_linux_process_identity") as identity:
            environment = managed_terminal_environment(remote=True)

        identity.assert_not_called()
        self.assertEqual("device-bridge", environment["AGENTSERVER_CONTROL_TRANSPORT"])
        self.assertNotIn("AGENTSERVER_CONTROL_SERVER_PID", environment)

    def test_persistent_tmux_uses_explicit_path_compatibility_mode(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENTSERVER_CONTROL_SOCKET": "/tmp/control.sock"},
            clear=False,
        ), patch("app.main.read_linux_process_identity") as identity:
            environment = managed_terminal_environment(verify_server_process=False)

        identity.assert_not_called()
        self.assertEqual(
            "local-broker-path-compat",
            environment["AGENTSERVER_CONTROL_TRANSPORT"],
        )
        self.assertNotIn("AGENTSERVER_CONTROL_SERVER_PID", environment)

    def test_direct_control_fails_closed_without_server_process_identity(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENTSERVER_CONTROL_SOCKET": "/tmp/control.sock"},
            clear=False,
        ), patch("app.main.read_linux_process_identity", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "process identity"):
                managed_terminal_environment(verify_server_process=True)


class TerminalExecutionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = ExecutionStore(Path(self.directory.name) / "execution.db")
        self.service = ExecutionService(self.store)
        self.manager = SimpleNamespace(backend="direct", sessions={})
        self.lifecycle = TerminalExecutionLifecycle(
            self.service,
            asyncio.get_running_loop(),
            ready_grace=0.03,
        )
        self.lifecycle.bind_manager(self.manager)

    async def asyncTearDown(self) -> None:
        await self.lifecycle.close()
        self.directory.cleanup()

    def session(
        self,
        terminal_id: str,
        *,
        owner: str = "alice",
        launch_id: str | None = None,
        kind: str = "ssh",
    ) -> TerminalSession:
        session = TerminalSession(
            id=terminal_id,
            name=terminal_id,
            pid=123,
            fd=-1,
            command="process",
            cwd=self.directory.name,
            owner=owner,
            kind=kind,
            launch_id=launch_id or f"launch-{terminal_id}",
            managed=True,
            origin="agentserver",
        )
        self.manager.sessions[terminal_id] = session
        return session

    async def register_connecting(self, session: TerminalSession) -> None:
        await self.lifecycle.register_connecting(
            owner_id=session.owner,
            terminal_id=session.id,
            launch_id=session.launch_id,
        )

    def state(self, session: TerminalSession) -> str:
        projection = self.service.projection(
            owner_id=session.owner,
            kind="terminal",
            entity_id=session.id,
        )
        assert projection is not None
        return str(projection.state["lifecycle"])

    def event_types(self, owner: str = "alice") -> list[str]:
        return [event.type for event in self.store.snapshot(owner_id=owner).events]

    async def test_remote_exit_during_ready_grace_fails_launch_once(self) -> None:
        session = self.session("remote-failed")
        await self.register_connecting(session)

        self.lifecycle.callback(
            session, {"type": "terminal.ready", "source": "pty"}
        )
        session.exited_at = time.time()
        session.return_code = 255
        exit_event = {"type": "terminal.exited", "return_code": 255}
        self.lifecycle.callback(session, exit_event)
        self.lifecycle.callback(session, exit_event)
        await self.lifecycle.drain()

        self.assertEqual("failed", self.state(session))
        event_types = self.event_types()
        self.assertEqual(1, event_types.count("terminal.launch.failed"))
        self.assertNotIn("terminal.ready", event_types)
        self.assertNotIn("terminal.exited", event_types)

    async def test_remote_ready_then_exit_is_serialized_and_idempotent(self) -> None:
        session = self.session("remote-ready")
        await self.register_connecting(session)

        self.lifecycle.callback(
            session, {"type": "terminal.ready", "source": "pty"}
        )
        await asyncio.sleep(0.05)
        await self.lifecycle.drain()
        self.assertEqual("ready", self.state(session))

        session.exited_at = time.time()
        session.return_code = 7
        exit_event = {"type": "terminal.exited", "return_code": 7}
        self.lifecycle.callback(session, exit_event)
        self.lifecycle.callback(session, exit_event)
        await self.lifecycle.drain()

        self.assertEqual("exited", self.state(session))
        event_types = self.event_types()
        self.assertEqual(1, event_types.count("terminal.ready"))
        self.assertEqual(1, event_types.count("terminal.exited"))

    async def test_direct_terminal_is_ready_after_explicit_exec_handshake(self) -> None:
        session = self.session("direct-ready", kind="local")
        await self.register_connecting(session)

        # PTY evidence is intentionally ignored for direct local creation; the
        # endpoint calls mark_ready immediately after TerminalManager's exec
        # handshake succeeds.
        self.lifecycle.callback(
            session, {"type": "terminal.ready", "source": "pty"}
        )
        await asyncio.sleep(0)
        self.assertEqual("connecting", self.state(session))

        self.assertTrue(await self.lifecycle.mark_ready(session))
        self.assertEqual("ready", self.state(session))

    async def test_local_tmux_terminal_waits_for_pty_evidence(self) -> None:
        self.manager.backend = "tmux"
        session = self.session("tmux-ready", kind="local")
        await self.register_connecting(session)

        self.lifecycle.callback(
            session, {"type": "terminal.ready", "source": "pty"}
        )
        self.assertEqual("connecting", self.state(session))
        await self.lifecycle.drain()

        self.assertEqual("ready", self.state(session))

    async def test_legacy_terminal_callbacks_are_ignored(self) -> None:
        session = self.session("legacy")
        session.managed = False
        session.origin = "legacy"
        session.launch_id = ""

        self.lifecycle.callback(
            session, {"type": "terminal.ready", "source": "pty"}
        )
        self.lifecycle.callback(
            session, {"type": "terminal.exited", "return_code": 0}
        )
        await asyncio.sleep(0)

        self.assertEqual((), self.store.snapshot(owner_id="alice").events)

    async def test_reconcile_uses_durable_owners_and_isolates_terminal_state(self) -> None:
        orphan = self.session("orphan", owner="alice")
        missing_ready = self.session("missing-ready", owner="bob")
        active = self.session("active", owner="bob")
        for session in (orphan, missing_ready, active):
            await self.register_connecting(session)
        self.assertTrue(await self.lifecycle.mark_ready(missing_ready))
        self.manager.sessions.pop(orphan.id)
        self.manager.sessions.pop(missing_ready.id)

        application = SimpleNamespace(
            state=SimpleNamespace(
                terminals=self.manager,
                execution=self.service,
                execution_store=self.store,
                terminal_execution_lifecycle=self.lifecycle,
            )
        )
        with patch.dict(
            os.environ, {"TERMINAL_LAUNCH_ORPHAN_TIMEOUT": "1"}
        ):
            await reconcile_execution_state(application, now=time.time() + 60)

        self.assertEqual("failed", self.state(orphan))
        self.assertEqual("exited", self.state(missing_ready))
        self.assertEqual("connecting", self.state(active))


class LifespanCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_broker_starts_and_is_removed_on_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "control" / "agentserver.sock"
            application = FastAPI()
            with patch("app.main.DATA_DIR", root), patch.dict(
                os.environ,
                {
                    "AGENTSERVER_CONTROL_SOCKET": str(socket_path),
                    "TERMINAL_BACKEND": "direct",
                    "TERMINAL_CWD": directory,
                    "TERMINAL_CMD": "",
                    "FRPS_DASHBOARD_URL": "",
                },
            ):
                async with lifespan(application):
                    self.assertIsNotNone(application.state.execution_control._server)
                    self.assertTrue(socket_path.is_socket())

            self.assertIsNone(application.state.execution_control._server)
            self.assertFalse(socket_path.exists())

    async def test_direct_create_records_connecting_ready_and_shutdown_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = FastAPI()
            with patch("app.main.DATA_DIR", root), patch.dict(
                os.environ,
                {
                    "AGENTSERVER_CONTROL_SOCKET": str(root / "control.sock"),
                    "TERMINAL_BACKEND": "direct",
                    "TERMINAL_CWD": directory,
                    "TERMINAL_CMD": "",
                    "FRPS_DASHBOARD_URL": "",
                },
            ):
                async with lifespan(application):
                    payload = await create_terminal(
                        CreateTerminalBody(name="Lifecycle"),
                        SimpleNamespace(app=application),
                        manager=application.state.terminals,
                        username="alice",
                    )
                    terminal_id = str(payload["id"])
                    projection = application.state.execution.projection(
                        owner_id="alice",
                        kind="terminal",
                        entity_id=terminal_id,
                    )
                    assert projection is not None
                    self.assertEqual("ready", projection.state["lifecycle"])
                    terminal_events = [
                        event.type
                        for event in application.state.execution_store.snapshot(
                            owner_id="alice",
                            aggregate_kind="terminal",
                            aggregate_id=terminal_id,
                        ).events
                    ]
                    self.assertEqual(
                        [
                            "terminal.launch.requested",
                            "terminal.connecting",
                            "terminal.ready",
                        ],
                        terminal_events,
                    )

            projection = application.state.execution.projection(
                owner_id="alice", kind="terminal", entity_id=terminal_id
            )
            assert projection is not None
            self.assertEqual("exited", projection.state["lifecycle"])

    async def test_direct_create_failure_is_compensated_after_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = FastAPI()
            with patch("app.main.DATA_DIR", root), patch.dict(
                os.environ,
                {
                    "AGENTSERVER_CONTROL_SOCKET": str(root / "control.sock"),
                    "TERMINAL_BACKEND": "direct",
                    "TERMINAL_CWD": directory,
                    "TERMINAL_CMD": "",
                    "FRPS_DASHBOARD_URL": "",
                },
            ):
                async with lifespan(application):
                    with patch.object(
                        application.state.terminals,
                        "create",
                        side_effect=RuntimeError("exec handshake failed"),
                    ):
                        with self.assertRaises(HTTPException) as failed:
                            await create_terminal(
                                CreateTerminalBody(name="Broken"),
                                SimpleNamespace(app=application),
                                manager=application.state.terminals,
                                username="alice",
                            )
                    self.assertEqual(500, failed.exception.status_code)
                    view = application.state.execution.execution_view(
                        owner_id="alice"
                    )
                    self.assertEqual(1, len(view["terminals"]))
                    self.assertEqual(
                        "failed", view["terminals"][0]["state"]["lifecycle"]
                    )

    async def test_failure_after_control_start_still_removes_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "control" / "agentserver.sock"
            application = FastAPI()
            with patch("app.main.DATA_DIR", root), patch.dict(
                os.environ,
                {
                    "AGENTSERVER_CONTROL_SOCKET": str(socket_path),
                    "TERMINAL_BACKEND": "direct",
                    "TERMINAL_CWD": directory,
                    "FRPS_DASHBOARD_URL": "",
                },
            ), patch("app.main.TerminalManager", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    async with lifespan(application):
                        self.fail("lifespan unexpectedly reached yield")

            self.assertFalse(socket_path.exists())


if __name__ == "__main__":
    unittest.main()
