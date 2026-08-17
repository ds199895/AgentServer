from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from app.execution.reporter import (
    ReporterConfigurationError,
    ReporterContext,
    ReporterSpool,
    ReporterSpoolFull,
    RuntimeReporter,
    load_reporter_token_file,
)


class ReporterSpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "spool.db"
        self.context = ReporterContext(
            owner_id="alice",
            device_id="device-1",
            terminal_id="terminal-1",
            launch_id="launch-1",
            run_id="run-1",
            assignment_id="assignment-1",
            task_id="task-1",
            agent_instance_id="agent-1",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def reporter(self, *, max_events: int = 10_000) -> RuntimeReporter:
        return RuntimeReporter(
            self.context,
            ReporterSpool(self.path, max_events=max_events),
            producer_id="bridge:device-1",
            adapter="kimi",
        )

    def test_events_survive_reopen_with_monotonic_epoch_and_sequence(self) -> None:
        reporter = self.reporter()
        first = reporter.emit("run.started")
        epoch = first["producer"]["epoch"]
        reopened = self.reporter()
        second = reopened.emit("run.activity.changed", {"activity": "coding"})
        self.assertEqual(epoch, second["producer"]["epoch"])
        self.assertEqual(first["producer"]["seq"] + 1, second["producer"]["seq"])
        self.assertEqual([first, second], reopened.spool.pending())

    def test_ack_keeps_missing_ranges_for_retry(self) -> None:
        reporter = self.reporter()
        events = [reporter.emit("run.activity.changed", {"index": index}) for index in range(3)]
        removed = reporter.spool.acknowledge(
            events[-1]["producer"]["seq"],
            missing_ranges=[(events[1]["producer"]["seq"], events[1]["producer"]["seq"])],
        )
        self.assertEqual(2, removed)
        self.assertEqual([events[1]], reporter.spool.pending())

    def test_full_spool_drops_only_coalescible_events(self) -> None:
        reporter = self.reporter(max_events=32)
        for index in range(31):
            reporter.emit("run.started", {"index": index})
        reporter.emit("run.progress.updated", {"progress": 0.1})
        reporter.emit("run.failed", {"summary": "safe"})
        self.assertEqual(32, len(reporter.spool))
        self.assertTrue(any(event["type"] == "run.failed" for event in reporter.spool.pending()))
        with self.assertRaises(ReporterSpoolFull):
            reporter.emit("run.succeeded")

    def test_full_spool_never_rewrites_an_event_that_may_have_reached_server(self) -> None:
        reporter = self.reporter(max_events=32)
        events = [
            reporter.emit("run.progress.updated", {"index": index})
            for index in range(32)
        ]
        attempted = reporter.spool.delivery_batch(limit=1)
        self.assertEqual([events[0]], attempted)

        replacement = reporter.emit("run.failed", {"summary": "safe"})
        pending = reporter.spool.pending(limit=100)
        sequences = [int(item["producer"]["seq"]) for item in pending]
        self.assertEqual(list(range(1, 33)), sequences)
        self.assertEqual(events[0], pending[0])
        self.assertEqual(32, int(replacement["producer"]["seq"]))
        self.assertEqual("run.failed", pending[-1]["type"])

        # Once every row has been attempted, replacing any of them could
        # conflict with the server's idempotency key and create a permanent gap.
        reporter.spool.delivery_batch(limit=100)
        with self.assertRaises(ReporterSpoolFull):
            reporter.emit("run.progress.updated", {"index": 99})

    def test_flush_retries_at_least_once_and_acks_server_cursor(self) -> None:
        reporter = self.reporter()
        first = reporter.emit("run.started")
        second = reporter.emit("run.activity.changed", {"activity": "testing"})
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual("Bearer report-token", request.headers["authorization"])
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "accepted_through_seq": second["producer"]["seq"],
                    "missing_ranges": [[first["producer"]["seq"], first["producer"]["seq"]]],
                    "results": [],
                },
            )

        reporter.flush(
            "https://agentserver.example",
            "report-token",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(2, len(captured[0]["events"]))
        self.assertEqual([first], reporter.spool.pending())

    def test_reporter_token_file_requires_private_regular_file(self) -> None:
        token_path = Path(self.directory.name) / "reporter.token"
        token_path.write_text("short-lived-token\n", encoding="utf-8")
        token_path.chmod(0o600)
        self.assertEqual("short-lived-token", load_reporter_token_file(token_path))
        if os.name != "nt":
            token_path.chmod(0o644)
            with self.assertRaises(ReporterConfigurationError):
                load_reporter_token_file(token_path)

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_spool_directory_and_sqlite_files_are_private(self) -> None:
        directory = Path(self.directory.name) / "private-state"
        reporter = RuntimeReporter(
            self.context,
            ReporterSpool(directory / "spool.db"),
            producer_id="bridge:device-1",
        )
        reporter.emit("run.started", expected_revision=0)
        self.assertEqual(0o700, directory.stat().st_mode & 0o777)
        self.assertEqual(0o600, (directory / "spool.db").stat().st_mode & 0o777)
        for suffix in ("-wal", "-shm"):
            path = Path(f"{directory / 'spool.db'}{suffix}")
            if path.exists():
                self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_revision_conflict_rebases_uncommitted_wal_event(self) -> None:
        reporter = self.reporter()
        event = reporter.emit(
            "run.activity.changed",
            {"activity": "testing"},
            expected_revision=2,
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "accepted_through_seq": 0,
                    "missing_ranges": [],
                    "results": [
                        {
                            "producer_seq": event["producer"]["seq"],
                            "status": "rejected",
                            "code": "revision_conflict",
                            "current_revision": 5,
                        }
                    ],
                },
            )

        reporter.flush(
            "https://agentserver.example",
            "report-token",
            transport=httpx.MockTransport(handler),
        )
        [rebased] = reporter.spool.pending()
        self.assertEqual(5, rebased["expected_revision"])


if __name__ == "__main__":
    unittest.main()
