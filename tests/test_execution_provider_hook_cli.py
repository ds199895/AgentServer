from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.execution.provider_hook import MAX_PROVIDER_EVENT_BYTES, main


class ProviderHookCliTests(unittest.TestCase):
    def test_script_imports_application_outside_repository_working_directory(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "agentserver_provider_hook.py"
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--jsonl", completed.stdout)

    def test_jsonl_stream_reports_each_object_and_stays_silent(self) -> None:
        stdin = io.TextIOWrapper(
            io.BytesIO(
                b'{"type":"thread.started","thread_id":"thread-1"}\n'
                b'\n'
                b'{"type":"turn.started","turn_id":"turn-1"}\n'
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("sys.stdin", stdin),
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
            patch(
                "app.execution.provider_hook.report_provider_event",
                return_value=[],
            ) as report,
        ):
            result = main(["--provider", "codex", "--jsonl"])

        self.assertEqual(0, result)
        self.assertEqual(2, report.call_count)
        self.assertEqual("thread.started", report.call_args_list[0].args[1]["type"])
        self.assertEqual("turn.started", report.call_args_list[1].args[1]["type"])
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_jsonl_rejects_an_oversized_line_without_reporting(self) -> None:
        stdin = io.TextIOWrapper(
            io.BytesIO(b'{' + b'x' * MAX_PROVIDER_EVENT_BYTES + b'}\n'),
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with (
            patch("sys.stdin", stdin),
            patch("sys.stderr", stderr),
            patch("app.execution.provider_hook.report_provider_event") as report,
        ):
            result = main(["--provider", "codex", "--jsonl"])

        self.assertEqual(2, result)
        report.assert_not_called()
        self.assertIn("exceeds 64 KiB", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
