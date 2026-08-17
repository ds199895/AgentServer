from __future__ import annotations

import tempfile
import unittest
import os
import socket
from unittest.mock import patch
from pathlib import Path

from app.execution.cli import (
    _expected_control_server,
    _verify_unix_control_server,
    build_parser,
    event_from_args,
    report,
)
from app.execution.control import read_linux_process_identity


class ExecutionReporterCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()

    def test_every_public_command_maps_to_a_normalized_event(self) -> None:
        cases = {
            ("phase", "coding"): ("run.activity.changed", "coding"),
            ("wait", "--reason", "approval"): ("run.activity.changed", "waiting"),
            ("heartbeat",): ("agent.heartbeat", None),
            ("complete", "--summary", "done"): ("run.succeeded", None),
            ("fail", "--summary", "safe failure"): ("run.failed", None),
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                event_type, payload = event_from_args(self.parser.parse_args(argv))
                self.assertEqual(expected[0], event_type)
                if expected[1]:
                    self.assertEqual(expected[1], payload["activity"])

    def test_invalid_progress_is_rejected_before_it_reaches_the_spool(self) -> None:
        arguments = self.parser.parse_args(("progress", "--current", "2", "--total", "1"))
        with self.assertRaisesRegex(ValueError, "0 <= current <= total"):
            event_from_args(arguments)

    def test_without_bridge_the_cli_queues_using_managed_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = report(
                self.parser.parse_args(("phase", "testing", "--summary", "unit tests")),
                {
                    "AGENTSERVER_OWNER_ID": "alice",
                    "AGENTSERVER_DEVICE_ID": "device-1",
                    "AGENTSERVER_TERMINAL_ID": "terminal-1",
                    "AGENTSERVER_LAUNCH_ID": "launch-1",
                    "AGENTSERVER_RUN_ID": "run-1",
                    "AGENTSERVER_TASK_ID": "task-1",
                    "AGENTSERVER_ASSIGNMENT_ID": "assignment-1",
                    "AGENTSERVER_REPORT_STATE_DIR": directory,
                },
            )
            self.assertTrue(result["ok"])
            self.assertEqual(1, result["queued"])
            self.assertTrue((Path(directory) / "agentserver" / "run-1.db").is_file())

    def test_heartbeat_uses_dedicated_local_bridge_action(self) -> None:
        arguments = self.parser.parse_args(("heartbeat",))
        with patch("app.execution.cli._send_bridge_request") as send:
            send.return_value = {"ok": True, "heartbeat": {"status": "active"}}
            result = report(
                arguments,
                {
                    "AGENTSERVER_CONTROL_SOCKET": "/tmp/agentserver.sock",
                    "AGENTSERVER_CONTROL_TRANSPORT": "device-bridge",
                    "AGENTSERVER_OWNER_ID": "alice",
                    "AGENTSERVER_TERMINAL_ID": "terminal-1",
                    "AGENTSERVER_LAUNCH_ID": "launch-1",
                },
            )
        self.assertTrue(result["ok"])
        self.assertEqual("heartbeat", send.call_args.args[1]["action"])

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux peer credentials required")
    def test_local_control_server_is_verified_before_runtime_data_is_sent(self) -> None:
        identity = read_linux_process_identity(os.getpid())
        assert identity is not None
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            _verify_unix_control_server(
                client,
                expected_pid=identity.pid,
                expected_start_time=identity.start_time_ticks,
            )
            with self.assertRaisesRegex(RuntimeError, "process identity"):
                _verify_unix_control_server(
                    client,
                    expected_pid=identity.pid + 1,
                    expected_start_time=identity.start_time_ticks,
                )
            with self.assertRaisesRegex(RuntimeError, "incarnation"):
                _verify_unix_control_server(
                    client,
                    expected_pid=identity.pid,
                    expected_start_time=identity.start_time_ticks + 1,
                )
        finally:
            client.close()
            server.close()

    def test_partial_control_server_identity_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "incomplete"):
            _expected_control_server(
                {
                    "AGENTSERVER_CONTROL_TRANSPORT": "local-broker",
                    "AGENTSERVER_CONTROL_SERVER_PID": "123",
                }
            )

    def test_missing_control_transport_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing"):
            _expected_control_server({})


if __name__ == "__main__":
    unittest.main()
