from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

from app.execution import ExecutionStore
from app.execution.control import ExecutionControlBroker, read_linux_process_identity
from app.execution.provider_adapters import (
    ClaudeAdapter,
    CodexAdapter,
    KimiAdapter,
    ProviderAdapter,
    sanitize_runtime_payload,
)
from app.execution.provider_hook import report_provider_event
from app.execution.service import ExecutionService


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = CodexAdapter()

    def test_session_and_turn_hooks_do_not_copy_sensitive_text(self) -> None:
        registered = self.adapter.normalize_many(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "cwd": "/workspace",
                "model": "codex-model",
                "transcript_path": "/secret/transcript.jsonl",
                "prompt": "do not persist this prompt",
            }
        )
        self.assertEqual("agent.registered", registered[0].type)
        self.assertNotIn("provider_session_id", registered[0].payload)
        self.assertNotIn("thread-1", str(registered[0].payload))
        self.assertNotIn("prompt", registered[0].payload)
        self.assertNotIn("transcript_path", registered[0].payload)
        self.assertNotIn("cwd", registered[0].payload)
        self.assertNotIn("model", registered[0].payload)

        activity = self.adapter.normalize_many(
            {
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "turn-1",
                "prompt": "a credential-like value",
            }
        )
        self.assertEqual("thinking", activity[0].payload["activity"])
        self.assertNotIn("prompt", activity[0].payload)

    def test_tool_hooks_create_sanitized_span_and_activity(self) -> None:
        started = self.adapter.normalize_many(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_use_id": "call-1",
                "tool_input": {"command": "contains-a-secret"},
            }
        )
        self.assertEqual(
            ["run.activity.changed", "span.started"],
            [event.type for event in started],
        )
        self.assertEqual("coding", started[0].payload["activity"])
        self.assertTrue(started[1].payload["span_id"].startswith("provider-tool-"))
        self.assertNotIn("call-1", str(started[1].payload))
        self.assertNotIn("tool_input", started[1].payload)
        self.assertNotIn("command", started[1].payload)

        ended = self.adapter.normalize_many(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_use_id": "call-2",
                "tool_input": {"command": "private"},
                "tool_response": {"exit_code": 1, "output": "private"},
            }
        )
        self.assertEqual("span.ended", ended[0].type)
        self.assertEqual("failed", ended[0].payload["outcome"])
        self.assertNotIn("tool_response", ended[0].payload)

    def test_permission_subagent_and_stop_are_not_false_completion(self) -> None:
        waiting = self.adapter.normalize_many(
            {"hook_event_name": "PermissionRequest", "tool_name": "Bash"}
        )
        self.assertEqual("waiting", waiting[0].payload["activity"])
        self.assertEqual("approval", waiting[0].payload["wait_reason"])

        child = self.adapter.normalize_many(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "child-agent-1",
                "agent_type": "reviewer",
            }
        )
        self.assertEqual("child_run.requested", child[0].type)
        self.assertTrue(
            child[0].payload["delegation_id"].startswith("provider-delegation-")
        )
        self.assertNotIn("child-agent-1", str(child[0].payload))
        self.assertEqual("codex", child[0].payload["agent_kind"])
        self.assertIn("delegation_observation", self.adapter.capabilities)
        self.assertNotIn("child_delegation", self.adapter.capabilities)
        self.assertEqual(1, len(child))

        stopped = self.adapter.normalize_many(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "not a result authority",
            }
        )
        self.assertEqual("run.activity.changed", stopped[0].type)
        self.assertEqual("finalizing", stopped[0].payload["activity"])

    def test_official_jsonl_shapes_map_to_lifecycle_and_tool_span(self) -> None:
        self.assertEqual(
            "agent.registered",
            self.adapter.normalize_many(
                {"type": "thread.started", "thread_id": "thread-1"}
            )[0].type,
        )
        started = self.adapter.normalize_many(
            {
                "type": "item.started",
                "item": {
                    "id": "item-1",
                    "type": "command_execution",
                    "command": "never copy this",
                    "status": "in_progress",
                },
            }
        )
        self.assertEqual("tooling", started[0].payload["activity"])
        self.assertEqual("span.started", started[1].type)
        self.assertNotIn("command", started[1].payload)
        completed = self.adapter.normalize_many(
            {"type": "turn.completed", "usage": {"input_tokens": 10}}
        )[0]
        self.assertEqual("run.activity.changed", completed.type)
        self.assertEqual("finalizing", completed.payload["activity"])

    def test_untyped_provider_adapters_drop_prompt_command_and_nested_payloads(self) -> None:
        for adapter in (ClaudeAdapter(), KimiAdapter(), ProviderAdapter()):
            with self.subTest(adapter=adapter.kind):
                [event] = adapter.normalize_many(
                    {
                        "type": "run.activity.changed",
                        "payload": {
                            "activity": "coding",
                            "prompt": "TOP-SECRET-PROMPT",
                            "command": "TOP-SECRET-COMMAND",
                            "tool_input": {"token": "TOP-SECRET-TOKEN"},
                        },
                    }
                )
                self.assertEqual({"activity": "coding"}, event.payload)

    def test_untyped_provider_adapters_reject_unknown_types_and_classifications(self) -> None:
        for adapter in (ClaudeAdapter(), KimiAdapter(), ProviderAdapter()):
            with self.subTest(adapter=adapter.kind, case="event_type"):
                with self.assertRaisesRegex(
                    ValueError, "unsupported untyped"
                ) as raised:
                    adapter.normalize_many(
                        {
                            "type": "secret.event",
                            "payload": {"reason": "TOP_SECRET_REASON"},
                        }
                    )
                self.assertNotIn("secret.event", str(raised.exception))
            for event_type, payload in (
                ("run.activity.changed", {"activity": "TOP_SECRET_ACTIVITY"}),
                ("run.failed", {"code": "TOP_SECRET_FAILURE_CODE"}),
                ("agent.stopping", {"reason": "TOP_SECRET_STOP_REASON"}),
                ("run.progress.updated", {"progress": float("nan")}),
                (
                    "child_run.requested",
                    {"delegation_id": "child-1", "agent_kind": "secret_agent_kind"},
                ),
            ):
                with self.subTest(
                    adapter=adapter.kind, case=event_type, payload=payload
                ):
                    with self.assertRaises(ValueError):
                        adapter.normalize_many({"type": event_type, "payload": payload})

    def test_provider_tool_names_are_canonical_and_unknown_status_fails_closed(self) -> None:
        [started] = ClaudeAdapter().normalize_many(
            {
                "type": "span.started",
                "payload": {
                    "span_id": "span-1",
                    "name": "TOP_SECRET_TOOL_NAME",
                    "kind": "TOP_SECRET_KIND",
                },
            }
        )
        self.assertEqual(
            {"name": "other_tool", "kind": "tool"},
            {
                key: value
                for key, value in started.payload.items()
                if key != "span_id"
            },
        )
        self.assertTrue(started.payload["span_id"].startswith("provider-span-"))
        self.assertNotIn("span-1", str(started.payload))

        stopped = self.adapter.normalize_many(
            {
                "hook_event_name": "SessionEnd",
                "reason": "TOP_SECRET_STOP_REASON",
            }
        )[0]
        self.assertEqual("other", stopped.payload["reason"])
        self.assertNotIn("TOP_SECRET_STOP_REASON", str(stopped.payload))

        ended = self.adapter.normalize_many(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "TOP_SECRET_TOOL_NAME",
                "tool_use_id": "TOP-SECRET-TOOL-ID",
                "tool_response": {"status": "TOP_SECRET_STATUS"},
            }
        )[0]
        self.assertEqual("other_tool", ended.payload["name"])
        self.assertEqual("failed", ended.payload["outcome"])
        self.assertNotIn("TOP_SECRET", str(ended.payload))

        with self.assertRaisesRegex(ValueError, "completion status"):
            self.adapter.normalize_many(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "command_execution",
                        "status": "TOP_SECRET_STATUS",
                    },
                }
            )
        with self.assertRaisesRegex(ValueError, "completion status"):
            self.adapter.normalize_many(
                {
                    "type": "item.completed",
                    "item": {"id": "item-2", "type": "command_execution"},
                }
            )

        for response in (
            None,
            {},
            {"error": "TOP_SECRET_ERROR"},
            {"exit_code": "0"},
        ):
            with self.subTest(response=response):
                [ended, _activity] = self.adapter.normalize_many(
                    {
                        "hook_event_name": "PostToolUse",
                        "tool_name": "Bash",
                        "tool_use_id": "tool-outcome-negative",
                        "tool_response": response,
                    }
                )
                self.assertEqual("failed", ended.payload["outcome"])
                self.assertNotIn("TOP_SECRET_ERROR", str(ended.payload))

        for raw in (
            {"hook_event_name": "TOP_SECRET_HOOK_NAME"},
            {"type": "TOP_SECRET_JSONL_EVENT"},
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as raised:
                    self.adapter.normalize_many(raw)
                self.assertNotIn("TOP_SECRET", str(raised.exception))

    def test_persistence_boundary_rekeys_provider_identifiers(self) -> None:
        payload = {"span_id": "low-entropy-call-1", "name": "Bash"}
        first = sanitize_runtime_payload(
            "span.started",
            payload,
            provider_kind="codex",
            reference_key=b"a" * 32,
        )
        repeated = sanitize_runtime_payload(
            "span.started",
            payload,
            provider_kind="codex",
            reference_key=b"a" * 32,
        )
        other_tenant_key = sanitize_runtime_payload(
            "span.started",
            payload,
            provider_kind="codex",
            reference_key=b"b" * 32,
        )
        self.assertEqual(first["span_id"], repeated["span_id"])
        self.assertNotEqual(first["span_id"], other_tenant_key["span_id"])
        self.assertNotIn("low-entropy-call-1", str(first))

    def test_artifact_payload_has_a_dedicated_workspace_relative_schema(self) -> None:
        [artifact] = ProviderAdapter().normalize_many(
            {
                "type": "artifact.published",
                "payload": {
                    "path": "reports\\result.png",
                    "kind": "image",
                    "media_type": "IMAGE/PNG",
                    "summary": "must not be copied",
                },
            }
        )
        self.assertEqual(
            {
                "path": "reports/result.png",
                "kind": "image",
                "media_type": "image/png",
            },
            artifact.payload,
        )
        self.assertIn("artifact_reporting", ProviderAdapter.capabilities)

        for payload in (
            {"path": "/etc/passwd", "kind": "file"},
            {"path": "../secret.txt", "kind": "file"},
            {"path": "result.txt", "kind": "secret_kind"},
            {
                "path": "result.txt",
                "kind": "file",
                "media_type": "secret_token_123",
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as raised:
                    ProviderAdapter().normalize_many(
                        {"type": "artifact.published", "payload": payload}
                    )
                self.assertNotIn("secret_token_123", str(raised.exception))


class ProviderHookTests(unittest.TestCase):
    def test_hook_sends_each_normalized_event_with_static_scope(self) -> None:
        requests: list[tuple[str, dict[str, object]]] = []

        def send(address: str, request: dict[str, object]) -> dict[str, object]:
            requests.append((address, request))
            return {"ok": True}

        responses = report_provider_event(
            "codex",
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_use_id": "tool-1",
                "tool_input": {"command": "sensitive"},
            },
            environment={
                "AGENTSERVER_CONTROL_SOCKET": "/tmp/control.sock",
                "AGENTSERVER_OWNER_ID": "alice",
                "AGENTSERVER_TERMINAL_ID": "terminal-1",
                "AGENTSERVER_LAUNCH_ID": "launch-1",
            },
            sender=send,
        )

        self.assertEqual(2, len(responses))
        self.assertEqual(2, len(requests))
        self.assertEqual("/tmp/control.sock", requests[0][0])
        self.assertEqual("alice", requests[0][1]["scope"]["owner_id"])
        self.assertEqual("span.started", requests[1][1]["event_type"])
        self.assertNotIn("tool_input", requests[1][1]["payload"])

    def test_hook_requires_a_managed_control_channel(self) -> None:
        with self.assertRaisesRegex(ValueError, "CONTROL_SOCKET"):
            report_provider_event(
                "codex",
                {"hook_event_name": "Stop"},
                environment={},
                sender=lambda _address, _request: {"ok": True},
            )


@unittest.skipUnless(
    hasattr(socket, "SO_PEERCRED") and Path("/proc/self/stat").is_file(),
    "requires Linux peer PID credentials",
)
class CodexProviderHookEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Exercise provider stdin through a bound process into durable state."""

    OWNER_ID = "alice"
    TERMINAL_ID = "terminal-1"
    LAUNCH_ID = "launch-1"

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = ExecutionStore(root / "execution.db")
        self.service = ExecutionService(self.store)
        self.service.register_terminal(
            owner_id=self.OWNER_ID,
            terminal_id=self.TERMINAL_ID,
            launch_id=self.LAUNCH_ID,
        )
        self.service.terminal_ready(
            owner_id=self.OWNER_ID, terminal_id=self.TERMINAL_ID
        )
        self.broker = ExecutionControlBroker(
            self.service, root / "control" / "agentserver.sock"
        )
        await self.broker.start()

        # This long-lived process represents the managed terminal root. Each
        # provider invocation is a real child process, so SO_PEERCRED and the
        # launch ancestry check exercise the production authorization path.
        worker_source = r'''
import json
import subprocess
import sys

for encoded in sys.stdin.buffer:
    completed = subprocess.run(
        [sys.executable, "-m", "app.execution.provider_hook", "--provider", "codex"],
        input=encoded,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    sys.stdout.write(json.dumps({
        "returncode": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
    }, separators=(",", ":")) + "\n")
    sys.stdout.flush()
'''
        environment = os.environ.copy()
        repository_root = str(Path(__file__).resolve().parents[1])
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            repository_root
            if not existing_pythonpath
            else repository_root + os.pathsep + existing_pythonpath
        )
        environment.update(
            {
                "AGENTSERVER_CONTROL_SOCKET": str(self.broker.path),
                "AGENTSERVER_CONTROL_TRANSPORT": "local-broker",
                "AGENTSERVER_OWNER_ID": self.OWNER_ID,
                "AGENTSERVER_TERMINAL_ID": self.TERMINAL_ID,
                "AGENTSERVER_LAUNCH_ID": self.LAUNCH_ID,
            }
        )
        server_identity = read_linux_process_identity(os.getpid())
        assert server_identity is not None
        environment.update(
            {
                "AGENTSERVER_CONTROL_SERVER_PID": str(server_identity.pid),
                "AGENTSERVER_CONTROL_SERVER_START_TIME": str(
                    server_identity.start_time_ticks
                ),
            }
        )
        self.worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            worker_source,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=repository_root,
        )
        assert self.worker.pid is not None
        self.broker.bind_launch(
            owner_id=self.OWNER_ID,
            terminal_id=self.TERMINAL_ID,
            launch_id=self.LAUNCH_ID,
            root_pid=self.worker.pid,
        )

    async def asyncTearDown(self) -> None:
        if self.worker.stdin is not None:
            self.worker.stdin.close()
        try:
            await asyncio.wait_for(self.worker.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.worker.kill()
            await self.worker.wait()
        await self.broker.close()
        self.directory.cleanup()

    async def provider_event(self, raw_event: dict[str, object]) -> dict[str, object]:
        assert self.worker.stdin is not None
        assert self.worker.stdout is not None
        self.worker.stdin.write(
            json.dumps(raw_event, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        await self.worker.stdin.drain()
        encoded = await asyncio.wait_for(self.worker.stdout.readline(), timeout=10)
        if not encoded:
            stderr = b""
            if self.worker.stderr is not None:
                stderr = await self.worker.stderr.read()
            self.fail(f"provider worker exited without a result: {stderr!r}")
        result = json.loads(encoded)
        self.assertEqual(0, result["returncode"], result["stderr"])
        # A successful provider hook is deliberately silent so it cannot steer
        # a provider that interprets hook stdout as control input.
        self.assertEqual("", result["stdout"])
        self.assertEqual("", result["stderr"])
        return result

    def assign_run(self) -> str:
        task = self.service.create_task(
            owner_id=self.OWNER_ID, title="Provider hook end-to-end"
        )
        assigned = self.service.assign_task(
            owner_id=self.OWNER_ID,
            task_id=str(task["id"]),
            terminal_id=self.TERMINAL_ID,
            agent_kind="codex",
            expected_task_revision=int(task["revision"]),
        )
        return str(assigned["runs"][0]["id"])

    async def test_event_without_active_assignment_is_ignored(self) -> None:
        before = self.store.snapshot(owner_id=self.OWNER_ID).as_of_sequence
        await self.provider_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "unassigned-session",
                "prompt": "must never be stored even when ignored",
            }
        )

        after = self.store.snapshot(owner_id=self.OWNER_ID)
        self.assertEqual(before, after.as_of_sequence)
        context = self.service.terminal_context(
            owner_id=self.OWNER_ID,
            terminal_id=self.TERMINAL_ID,
            launch_id=self.LAUNCH_ID,
        )
        self.assertIsNone(context["active_run_id"])
        self.assertIsNone(context["assignment"])

    async def test_first_hook_fact_after_ignored_session_registers_and_activates(self) -> None:
        await self.provider_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "session-before-assignment",
            }
        )
        run_id = self.assign_run()

        # The provider process is already alive, so no second SessionStart occurs.
        await self.provider_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-before-assignment",
                "turn_id": "first-turn-after-assignment",
                "prompt": "private first prompt",
            }
        )

        run = self.service.get_run(owner_id=self.OWNER_ID, run_id=run_id)
        self.assertEqual("running", run["state"]["lifecycle"])
        self.assertEqual("thinking", run["state"]["activity"])
        run_events = [
            event
            for event in self.store.snapshot(owner_id=self.OWNER_ID).events
            if event.scope.run_id == run_id
        ]
        self.assertEqual(
            1, sum(event.type == "agent.registered" for event in run_events)
        )
        self.assertTrue(
            any(
                event.type == "run.activity.changed"
                and event.producer.adapter == "codex"
                for event in run_events
            )
        )

    async def test_first_jsonl_fact_after_assignment_registers_and_activates(self) -> None:
        run_id = self.assign_run()
        await self.provider_event(
            {
                "type": "item.started",
                "thread_id": "already-running-thread",
                "item": {
                    "id": "first-item",
                    "type": "file_change",
                    "status": "in_progress",
                    "changes": "private patch body",
                },
            }
        )

        run = self.service.get_run(owner_id=self.OWNER_ID, run_id=run_id)
        self.assertEqual("running", run["state"]["lifecycle"])
        self.assertEqual("coding", run["state"]["activity"])
        run_events = [
            event
            for event in self.store.snapshot(owner_id=self.OWNER_ID).events
            if event.scope.run_id == run_id
        ]
        self.assertEqual(
            1, sum(event.type == "agent.registered" for event in run_events)
        )

    async def test_active_hook_and_jsonl_change_projected_activity(self) -> None:
        run_id = self.assign_run()
        await self.provider_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "codex-thread",
                "model": "codex-model",
            }
        )
        await self.provider_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "turn-1",
                "prompt": "private prompt",
            }
        )
        thinking = self.service.get_run(owner_id=self.OWNER_ID, run_id=run_id)
        self.assertEqual("thinking", thinking["state"]["activity"])

        await self.provider_event(
            {
                "type": "item.started",
                "thread_id": "codex-thread",
                "turn_id": "turn-1",
                "item": {
                    "id": "file-change-1",
                    "type": "file_change",
                    "status": "in_progress",
                    "changes": "private patch body",
                },
            }
        )
        coding = self.service.get_run(owner_id=self.OWNER_ID, run_id=run_id)
        self.assertEqual("running", coding["state"]["lifecycle"])
        self.assertEqual("coding", coding["state"]["activity"])
        self.assertEqual("adapter", coding["evidence"]["activity"]["source"])
        stored_span_ids = [
            event.scope.span_id
            for event in self.store.snapshot(owner_id=self.OWNER_ID).events
            if event.type == "span.started"
            and event.scope.run_id == run_id
            and event.scope.span_id is not None
        ]
        self.assertEqual(1, len(stored_span_ids))
        self.assertTrue(stored_span_ids[0].startswith("provider-span-"))
        transport_span_id = self.adapter_span_id(
            item_id="file-change-1", item_type="file_change"
        )
        self.assertNotEqual(transport_span_id, stored_span_ids[0])
        self.assertNotIn(
            "file-change-1",
            json.dumps(
                [
                    event.as_dict()
                    for event in self.store.snapshot(owner_id=self.OWNER_ID).events
                ],
                sort_keys=True,
            ),
        )

    @staticmethod
    def adapter_span_id(*, item_id: str, item_type: str) -> str:
        normalized = CodexAdapter().normalize_many(
            {
                "type": "item.started",
                "item": {"id": item_id, "type": item_type, "status": "in_progress"},
            }
        )
        return str(normalized[1].payload["span_id"])

    async def test_stop_is_finalizing_not_run_success(self) -> None:
        run_id = self.assign_run()
        await self.provider_event(
            {"hook_event_name": "SessionStart", "session_id": "codex-thread"}
        )
        await self.provider_event(
            {
                "hook_event_name": "Stop",
                "turn_id": "turn-1",
                "last_assistant_message": "not result authority",
            }
        )

        run = self.service.get_run(owner_id=self.OWNER_ID, run_id=run_id)
        self.assertEqual("running", run["state"]["lifecycle"])
        self.assertEqual("finalizing", run["state"]["activity"])
        run_events = [
            event.type
            for event in self.store.snapshot(owner_id=self.OWNER_ID).events
            if event.scope.run_id == run_id
        ]
        self.assertNotIn("run.succeeded", run_events)

    async def test_sensitive_prompt_and_tool_content_never_reaches_event_log(self) -> None:
        self.assign_run()
        secrets = (
            "TOP-SECRET-PROMPT-4b856",
            "TOP-SECRET-TOOL-INPUT-c1392",
            "TOP-SECRET-TOOL-OUTPUT-a02cf",
            "TOP-SECRET-JSONL-COMMAND-0dc11",
            "TOP-SECRET-SESSION-ID-0deda",
            "TOP_SECRET_TOOL_NAME_a71e0",
            "TOP_SECRET_STOP_REASON_b24a1",
            "TOP-SECRET-MODEL-dc331",
            "TOP-SECRET-TURN-ID-531f3",
            "TOP-SECRET-TOOL-ID-a109c",
            "TOP_SECRET_STATUS_417a",
            "TOP-SECRET-ITEM-ID-34b20",
        )
        await self.provider_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": secrets[4],
                "model": secrets[7],
                "transcript_path": "/private/transcript.jsonl",
                "prompt": secrets[0],
            }
        )
        await self.provider_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "turn_id": secrets[8],
                "prompt": secrets[0],
            }
        )
        await self.provider_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": secrets[5],
                "tool_use_id": secrets[9],
                "tool_input": {"patch": secrets[1]},
            }
        )
        await self.provider_event(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": secrets[5],
                "tool_use_id": secrets[9],
                "tool_response": {
                    "status": secrets[10],
                    "output": secrets[2],
                },
            }
        )
        await self.provider_event(
            {
                "type": "item.started",
                "thread_id": secrets[4],
                "item": {
                    "id": secrets[11],
                    "type": "command_execution",
                    "command": secrets[3],
                    "status": "in_progress",
                },
            }
        )
        await self.provider_event(
            {
                "hook_event_name": "SessionEnd",
                "session_id": secrets[4],
                "reason": secrets[6],
            }
        )

        events = self.store.snapshot(owner_id=self.OWNER_ID).events
        serialized = json.dumps(
            [event.as_dict() for event in events],
            separators=(",", ":"),
            sort_keys=True,
        )
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        forbidden_payload_keys = {
            "prompt",
            "transcript_path",
            "tool_input",
            "tool_response",
            "command",
            "output",
            "changes",
            "cwd",
            "model",
        }
        provider_events = [
            event for event in events if event.producer.adapter == "codex"
        ]
        self.assertTrue(provider_events)
        for event in provider_events:
            self.assertTrue(forbidden_payload_keys.isdisjoint(event.payload))


if __name__ == "__main__":
    unittest.main()
