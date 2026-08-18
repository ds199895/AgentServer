from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.execution.provider_exec import run_provider_command
from app.execution.provider_hook import (
    MAX_PROVIDER_STREAM_EVENT_BYTES,
    ProviderEventStream,
    consume_provider_jsonl,
)


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
        stdin = io.BytesIO(
            b'{"type":"thread.started","thread_id":"thread-1"}\n'
            b'not-json\n'
            b'\n'
            b'{"type":"turn.started","turn_id":"turn-1"}\n'
        )
        stderr = io.StringIO()
        requests: list[dict[str, object]] = []

        def send(_address: str, request: dict[str, object]) -> dict[str, object]:
            requests.append(request)
            return {"ok": True}

        result = consume_provider_jsonl(
            "codex",
            input_stream=stdin,
            error_stream=stderr,
            environment={"AGENTSERVER_CONTROL_SOCKET": "/tmp/control.sock"},
            sender=send,
        )

        self.assertEqual(0, result)
        self.assertEqual(
            ["agent.registered", "run.activity.changed", "run.activity.changed"],
            [request["event_type"] for request in requests],
        )
        self.assertEqual("finalizing", requests[-1]["payload"]["activity"])
        self.assertIn("Expecting value", stderr.getvalue())

    def test_jsonl_drains_an_oversized_line_and_continues(self) -> None:
        stdin = io.BytesIO(
            b'{' + b'x' * MAX_PROVIDER_STREAM_EVENT_BYTES + b'}\n'
            b'{"type":"thread.started","thread_id":"thread-1"}\n'
        )
        stderr = io.StringIO()
        requests: list[dict[str, object]] = []

        def send(_address: str, request: dict[str, object]) -> dict[str, object]:
            requests.append(request)
            return {"ok": True}

        result = consume_provider_jsonl(
            "codex",
            input_stream=stdin,
            error_stream=stderr,
            environment={"AGENTSERVER_CONTROL_SOCKET": "/tmp/control.sock"},
            sender=send,
        )

        self.assertEqual(0, result)
        self.assertEqual("agent.registered", requests[0]["event_type"])
        self.assertEqual("run.activity.changed", requests[-1]["event_type"])
        self.assertIn("exceeds 8 MiB", stderr.getvalue())

    def test_stream_end_closes_incomplete_spans_and_finalizes(self) -> None:
        stream = ProviderEventStream("kimi")
        started = stream.normalize(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "tool-1",
                        "function": {"name": "Bash", "arguments": "{}"},
                    }
                ],
            }
        )
        self.assertEqual("span.started", started[-1].type)
        ended = stream.finish(exit_code=7)
        self.assertEqual(
            ["span.ended", "run.activity.changed"],
            [event.type for event in ended],
        )
        self.assertEqual("failed", ended[0].payload["outcome"])
        self.assertEqual("finalizing", ended[1].payload["activity"])

    def test_provider_exec_preserves_stdout_and_provider_exit_status(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "agentserver_provider_exec.py"
        )
        payload = '{"role":"assistant","content":"visible"}'
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--provider",
                "kimi",
                "--",
                sys.executable,
                "-c",
                f"import sys; print({payload!r}); raise SystemExit(7)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(7, completed.returncode)
        self.assertEqual(payload + "\n", completed.stdout)
        self.assertIn("telemetry", completed.stderr)

    def test_provider_exec_forwards_an_oversized_line_without_truncation(self) -> None:
        payload = b"x" * (MAX_PROVIDER_STREAM_EVENT_BYTES + 17) + b"\n"
        stdout = io.BytesIO()
        stderr = io.StringIO()
        result = run_provider_command(
            "kimi",
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * "
                f"{MAX_PROVIDER_STREAM_EVENT_BYTES + 17} + b'\\n')",
            ),
            output_stream=stdout,
            error_stream=stderr,
            environment={},
        )

        self.assertEqual(0, result)
        self.assertEqual(payload, stdout.getvalue())
        self.assertIn("exceeds 8 MiB", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
