from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest import mock

from app.execution.runtime_adapters import (
    ApprovalDecision,
    CodexRuntimeAdapter,
    DEFAULT_RUNTIME_ADAPTER_REGISTRY,
    PermissionMode,
    RuntimeAttachment,
    RuntimeInteractionResolvedError,
    RuntimeInvalidDecisionError,
    RuntimeOperationTimeoutError,
    RuntimeProtocolError,
    RuntimeRequestError,
    RuntimeSessionSpec,
    RuntimeSpawnError,
    RuntimeTransportError,
    RuntimeTurnInput,
    codex_permission_config,
    create_default_runtime_adapter_registry,
)
from app.execution.runtime_adapters.base import RuntimeEvent
from app.execution.runtime_adapters.jsonrpc import (
    AsyncJsonRpcPeer,
    JsonRpcProtocolError,
    JsonRpcTransportError,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "codex_app_server_peer.py"


class CodexRuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.adapters: list[CodexRuntimeAdapter] = []
        self.counter = 0

    async def asyncTearDown(self) -> None:
        await asyncio.gather(
            *(adapter.close() for adapter in self.adapters),
            return_exceptions=True,
        )
        self.directory.cleanup()

    def adapter(
        self,
        *,
        scenario: str = "normal",
        approval: str = "none",
        user_input: bool = False,
        turn_status: str = "completed",
        burst: int = 0,
        stderr_bytes: int = 0,
        event_queue_size: int = 128,
        request_timeout: float = 2,
    ) -> tuple[CodexRuntimeAdapter, Path]:
        self.counter += 1
        transcript = self.root / f"transcript-{self.counter}.jsonl"
        command = [
            sys.executable,
            str(FIXTURE),
            "--transcript",
            str(transcript),
            "--scenario",
            scenario,
            "--approval",
            approval,
            "--turn-status",
            turn_status,
            "--burst",
            str(burst),
            "--stderr-bytes",
            str(stderr_bytes),
        ]
        if user_input:
            command.append("--user-input")
        adapter = CodexRuntimeAdapter(
            command=command,
            isolation_enabled=False,
            initialize_timeout=2,
            request_timeout=request_timeout,
            interrupt_timeout=2,
            kill_timeout=0.5,
            event_queue_size=event_queue_size,
        )
        self.adapters.append(adapter)
        return adapter, transcript

    @staticmethod
    def transcript(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    async def next_event(
        stream,
        event_type: str,
        *,
        request_type: str | None = None,
        timeout: float = 3,
    ) -> RuntimeEvent:
        async def find() -> RuntimeEvent:
            async for event in stream:
                if event.type != event_type:
                    continue
                if (
                    request_type is not None
                    and event.payload.get("request_type") != request_type
                ):
                    continue
                return event
            raise AssertionError(f"event stream ended before {event_type}")

        return await asyncio.wait_for(find(), timeout=timeout)

    async def test_registry_permission_matrix_and_probe(self) -> None:
        expected = {
            PermissionMode.APPROVAL_REQUIRED: (
                "untrusted",
                "user",
                "read-only",
                {"type": "readOnly"},
            ),
            PermissionMode.WORKSPACE_WRITE: (
                "on-request",
                "user",
                "workspace-write",
                {"type": "workspaceWrite"},
            ),
            PermissionMode.AUTO: (
                "on-request",
                "auto_review",
                "workspace-write",
                {"type": "workspaceWrite"},
            ),
            PermissionMode.FULL_ACCESS: (
                "never",
                "user",
                "danger-full-access",
                {"type": "dangerFullAccess"},
            ),
        }
        for mode, values in expected.items():
            with self.subTest(mode=mode.value):
                config = codex_permission_config(mode)
                self.assertEqual(values[0], config["approvalPolicy"])
                self.assertEqual(values[1], config["approvalsReviewer"])
                self.assertEqual(values[2], config["sandbox"])
                self.assertEqual(values[3], config["sandboxPolicy"])
        self.assertEqual(
            codex_permission_config(PermissionMode.WORKSPACE_WRITE),
            codex_permission_config("auto-accept-edits"),
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            codex_permission_config("root-everything")

        self.assertEqual(("codex",), DEFAULT_RUNTIME_ADAPTER_REGISTRY.providers)
        isolated = create_default_runtime_adapter_registry()
        self.assertEqual(("codex",), isolated.providers)
        registered = isolated.create(
            "CODEX",
            command=(sys.executable, "-c", "pass"),
            isolation_enabled=False,
        )
        self.assertIsInstance(registered, CodexRuntimeAdapter)
        await registered.close()

        adapter, _transcript = self.adapter()
        probe = await adapter.probe()
        self.assertTrue(probe.available)
        missing = CodexRuntimeAdapter(
            binary_path=str(self.root / "missing-codex"),
            isolation_enabled=False,
        )
        self.adapters.append(missing)
        self.assertFalse((await missing.probe()).available)

    async def test_session_preflight_distinguishes_new_and_resumed_threads(self) -> None:
        adapter, _transcript = self.adapter()
        adapter.validate_session(
            RuntimeSessionSpec(
                session_id="new-session",
                cwd=self.root,
                permission_mode=PermissionMode.WORKSPACE_WRITE,
                resume_cursor=None,
            )
        )
        with self.assertRaisesRegex(ValueError, "resume_cursor requires thread_id"):
            adapter.validate_session(
                RuntimeSessionSpec(
                    session_id="invalid-resume",
                    cwd=self.root,
                    permission_mode=PermissionMode.WORKSPACE_WRITE,
                    resume_cursor={},
                )
            )

    async def test_bubblewrap_command_is_fail_closed_and_state_mask_is_last(self) -> None:
        state_dir = self.root / "runtime-state"
        codex_home = self.root / "codex-home"
        workspace = self.root / "workspace"
        for path in (state_dir, codex_home, workspace):
            path.mkdir()
        adapter = CodexRuntimeAdapter(
            command=(sys.executable, "-c", "raise SystemExit(0)"),
            home_path=codex_home,
            host_state_dir=state_dir,
            bubblewrap_path="/bin/true",
        )
        self.adapters.append(adapter)
        command = adapter._bubblewrap_command(
            workspace.resolve(),
            (sys.executable, "-c", "raise SystemExit(0)"),
        )
        self.assertEqual("/bin/true", command[0])
        for option in (
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
        ):
            self.assertIn(option, command)
        self.assertNotIn("--unshare-net", command)
        self.assertEqual(("--ro-bind", "/", "/"), command[6:9])
        self.assertIn(
            ("--proc", "/proc", "--tmpfs", "/tmp", "--dev", "/dev"),
            tuple(command[index : index + 6] for index in range(len(command) - 5)),
        )
        separator = command.index("--")
        self.assertEqual(
            ("--tmpfs", str(state_dir.resolve())),
            command[separator - 4 : separator - 2],
        )
        self.assertEqual(("--chdir", str(workspace.resolve())), command[separator - 2 : separator])
        self.assertEqual(sys.executable, command[separator + 1])
        self.assertLess(
            command.index(str(codex_home.resolve())),
            command.index(str(state_dir.resolve())),
        )

    async def test_isolation_rejects_path_overlap_and_loader_environment(self) -> None:
        state_dir = self.root / "runtime-state"
        codex_home = self.root / "codex-home"
        workspace = self.root / "workspace"
        for path in (state_dir, codex_home, workspace):
            path.mkdir()

        state_workspace = state_dir / "workspace"
        state_workspace.mkdir()
        overlapping_state = CodexRuntimeAdapter(
            command=(sys.executable, "-c", "pass"),
            home_path=codex_home,
            host_state_dir=state_dir,
            bubblewrap_path="/bin/true",
        )
        self.adapters.append(overlapping_state)
        with self.assertRaisesRegex(ValueError, "state directory overlaps session cwd"):
            overlapping_state._bubblewrap_command(
                state_workspace.resolve(), (sys.executable, "-c", "pass")
            )

        home_workspace = codex_home / "workspace"
        home_workspace.mkdir()
        with self.assertRaisesRegex(ValueError, "CODEX_HOME overlaps session cwd"):
            overlapping_state._bubblewrap_command(
                home_workspace.resolve(), (sys.executable, "-c", "pass")
            )

        state_inside_home = codex_home / "host-state"
        state_inside_home.mkdir()
        overlapping_home = CodexRuntimeAdapter(
            command=(sys.executable, "-c", "pass"),
            home_path=codex_home,
            host_state_dir=state_inside_home,
            bubblewrap_path="/bin/true",
        )
        self.adapters.append(overlapping_home)
        with self.assertRaisesRegex(ValueError, "state directory overlaps CODEX_HOME"):
            overlapping_home._bubblewrap_command(
                workspace.resolve(), (sys.executable, "-c", "pass")
            )

        inherited_secret = "AGENTSERVER_TEST_HOST_SECRET"
        with mock.patch.dict(
            os.environ,
            {
                inherited_secret: "must-not-cross-boundary",
                "LD_PRELOAD": "/tmp/inherited-loader.so",
                "PYTHONPATH": "/tmp/inherited-python",
            },
        ):
            clean = overlapping_state._spawn_environment(
                None, cwd=workspace.resolve()
            )
        self.assertNotIn(inherited_secret, clean)
        self.assertFalse(any(name.startswith("LD_") for name in clean))
        self.assertFalse(any(name.startswith("PYTHON") for name in clean))

        for name in ("LD_PRELOAD", "PYTHONPATH", "BASH_ENV", "NODE_OPTIONS"):
            with self.subTest(name=name):
                unsafe = CodexRuntimeAdapter(
                    command=(sys.executable, "-c", "pass"),
                    isolation_enabled=False,
                    environment={name: "/tmp/injected"},
                )
                self.adapters.append(unsafe)
                with self.assertRaisesRegex(ValueError, "is forbidden"):
                    await unsafe.start_session(
                        RuntimeSessionSpec(session_id=f"unsafe-{name}", cwd=workspace)
                    )

    async def test_failed_bubblewrap_probe_prevents_provider_spawn(self) -> None:
        state_dir = self.root / "runtime-state"
        codex_home = self.root / "codex-home"
        workspace = self.root / "workspace"
        for path in (state_dir, codex_home, workspace):
            path.mkdir()
        marker = self.root / "provider-was-started"
        adapter = CodexRuntimeAdapter(
            command=(
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ),
            home_path=codex_home,
            host_state_dir=state_dir,
            bubblewrap_path="/bin/false",
        )
        self.adapters.append(adapter)
        probe = await adapter.probe()
        self.assertFalse(probe.available)
        self.assertEqual("isolation_probe_failed", probe.detail_code)
        with self.assertRaises(RuntimeSpawnError):
            await adapter.start_session(
                RuntimeSessionSpec(session_id="bwrap-failed", cwd=workspace)
            )
        self.assertFalse(marker.exists())

    async def test_real_bubblewrap_hides_host_state_and_keeps_only_scoped_writes(self) -> None:
        bubblewrap = shutil.which("bwrap")
        if not sys.platform.startswith("linux") or not bubblewrap:
            self.skipTest("bubblewrap is unavailable")
        sandbox_directory = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1]
        )
        self.addCleanup(sandbox_directory.cleanup)
        sandbox_root = Path(sandbox_directory.name)
        state_dir = sandbox_root / "runtime-state"
        codex_home = sandbox_root / "codex-home"
        workspace = sandbox_root / "workspace"
        for path in (state_dir, codex_home, workspace):
            path.mkdir()
        (state_dir / "device.credential").write_text("host-secret", encoding="utf-8")
        read_only_target = sandbox_root / "outside-writable-scopes"
        read_only_target.write_text("unchanged", encoding="utf-8")
        adapter = CodexRuntimeAdapter(
            command=(sys.executable, "-c", "pass"),
            home_path=codex_home,
            host_state_dir=state_dir,
            bubblewrap_path=bubblewrap,
        )
        self.adapters.append(adapter)
        probe = await adapter.probe()
        if not probe.available:
            self.skipTest(f"bubblewrap namespaces unavailable: {probe.detail_code}")
        script = """
import pathlib
import sys
state, workspace, codex_home, outside = map(pathlib.Path, sys.argv[1:])
assert not (state / 'device.credential').exists()
(state / 'device.credential').write_text('sandbox-only', encoding='utf-8')
(workspace / 'workspace-write').write_text('ok', encoding='utf-8')
(codex_home / 'home-write').write_text('ok', encoding='utf-8')
try:
    outside.write_text('changed', encoding='utf-8')
except OSError:
    pass
else:
    raise AssertionError('read-only root unexpectedly writable')
"""
        command = adapter._bubblewrap_command(
            workspace.resolve(),
            (
                sys.executable,
                "-c",
                script,
                str(state_dir.resolve()),
                str(workspace.resolve()),
                str(codex_home.resolve()),
                str(read_only_target.resolve()),
            ),
        )
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=str(workspace),
            env=adapter._spawn_environment(None, cwd=workspace.resolve()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode())
        self.assertEqual("host-secret", (state_dir / "device.credential").read_text())
        self.assertTrue((workspace / "workspace-write").exists())
        self.assertTrue((codex_home / "home-write").exists())
        self.assertEqual("unchanged", read_only_target.read_text())

    async def test_handshake_turn_thread_operations_sanitization_and_close(self) -> None:
        adapter, transcript = self.adapter(
            turn_status="hold", stderr_bytes=192 * 1024
        )
        session = await adapter.start_session(
            RuntimeSessionSpec(
                session_id="session-main",
                cwd=self.root,
                permission_mode=PermissionMode.WORKSPACE_WRITE,
                model="gpt-fixture",
                service_tier="fast",
            )
        )
        self.assertEqual("ready", session.state)
        self.assertEqual({"thread_id": "thread-fixture"}, session.resume_cursor)
        stream = adapter.events("session-main")

        messages = self.transcript(transcript)
        self.assertEqual(
            ["initialize", "initialized", "thread/start"],
            [message.get("method") for message in messages[:3]],
        )
        self.assertNotIn("jsonrpc", messages[0])
        self.assertNotIn("params", messages[1])
        initialize = messages[0]["params"]
        self.assertTrue(initialize["capabilities"]["experimentalApi"])
        thread_start = messages[2]["params"]
        self.assertEqual("on-request", thread_start["approvalPolicy"])
        self.assertEqual("user", thread_start["approvalsReviewer"])
        self.assertEqual("workspace-write", thread_start["sandbox"])
        self.assertEqual("gpt-fixture", thread_start["model"])
        self.assertEqual("fast", thread_start["serviceTier"])

        turn = await adapter.send_turn(
            "session-main",
            RuntimeTurnInput(
                text="LEAK_USER_PROMPT",
                attachments=(
                    RuntimeAttachment(type="image", url="https://example.test/a.png"),
                ),
                model="gpt-turn",
                service_tier="priority",
                effort="high",
            ),
        )
        self.assertEqual("turn-1", turn.turn_id)
        await adapter.interrupt_turn("session-main", turn.turn_id)
        snapshot = await adapter.read_thread("session-main")
        rolled_back = await adapter.rollback_thread("session-main", 1)
        self.assertEqual("thread-fixture", snapshot.thread_id)
        self.assertEqual(snapshot, rolled_back)
        self.assertEqual(
            {"id": "snapshot-item", "type": "commandExecution", "status": "completed"},
            dict(snapshot.turns[0].items[0]),
        )

        messages = self.transcript(transcript)
        by_method = {
            message["method"]: message
            for message in messages
            if isinstance(message.get("method"), str)
        }
        turn_params = by_method["turn/start"]["params"]
        self.assertEqual(
            [
                {"type": "text", "text": "LEAK_USER_PROMPT"},
                {"type": "image", "url": "https://example.test/a.png"},
            ],
            turn_params["input"],
        )
        self.assertEqual({"type": "workspaceWrite"}, turn_params["sandboxPolicy"])
        self.assertEqual("gpt-turn", turn_params["model"])
        self.assertEqual("priority", turn_params["serviceTier"])
        self.assertEqual("high", turn_params["effort"])
        self.assertEqual(1, by_method["thread/rollback"]["params"]["numTurns"])

        async def collect_through_turn_completion() -> list[RuntimeEvent]:
            collected: list[RuntimeEvent] = []
            async for event in stream:
                collected.append(event)
                if event.type == "turn.completed":
                    return collected
            raise AssertionError("event stream ended before turn.completed")

        events = await asyncio.wait_for(collect_through_turn_completion(), 3)
        command_started = next(
            event
            for event in events
            if event.type == "item.started" and event.item_id == "command-item"
        )
        self.assertEqual("Command", command_started.payload["title"])
        self.assertEqual("[redacted]", command_started.payload["input"]["command"])
        self.assertEqual(
            "[redacted]", command_started.payload["output"]["aggregatedOutput"]
        )
        self.assertIn("tool.output.delta", [event.type for event in events])
        summaries = [
            event.payload["text"]
            for event in events
            if event.type == "reasoning.delta"
        ]
        self.assertEqual(["Reviewed the public interface."], summaries)
        plan = next(event for event in events if event.type == "turn.plan.updated")
        self.assertEqual(
            [{"step": "Inspect the interface", "status": "completed"}],
            plan.payload["plan"],
        )
        encoded_snapshot = json.dumps(asdict(snapshot), sort_keys=True)
        self.assertNotIn("LEAK_SNAPSHOT_COMMAND", encoded_snapshot)
        self.assertNotIn("LEAK_SNAPSHOT_OUTPUT", encoded_snapshot)

        state = adapter._sessions["session-main"]  # lifecycle/process assertion
        process = state.process
        await adapter.stop_session("session-main")
        remaining = []
        async for event in stream:
            remaining.append(event)
        all_public = json.dumps(
            [
                {
                    "type": event.type,
                    "payload": dict(event.payload),
                    "turn_id": event.turn_id,
                    "item_id": event.item_id,
                }
                for event in [*events, *remaining]
            ],
            sort_keys=True,
        )
        for secret in (
            "LEAK_COMMAND",
            "LEAK_OUTPUT",
            "LEAK_OUTPUT_DELTA",
            "LEAK_ASSISTANT_DELTA",
            "LEAK_RAW_PROVIDER_PAYLOAD",
            "LEAK_USER_PROMPT",
            "LEAK_HIDDEN_REASONING",
            "LEAK_HIDDEN_REASONING_DELTA",
            "LEAK_PLAN_PRIVATE",
        ):
            self.assertNotIn(secret, all_public)
        self.assertIn("session.stopped", [event.type for event in remaining])
        self.assertIn("session.exited", [event.type for event in remaining])
        self.assertIsNotNone(process.returncode)
        self.assertEqual((), await adapter.list_sessions())
        await adapter.close()
        await adapter.close()

    async def test_approval_decisions_user_input_and_exactly_once(self) -> None:
        adapter, transcript = self.adapter(approval="command")
        await adapter.start_session(
            RuntimeSessionSpec(session_id="approval-session", cwd=self.root)
        )
        stream = adapter.events("approval-session")
        decisions = (
            (ApprovalDecision.APPROVE_ONCE, "accept"),
            (ApprovalDecision.APPROVE_SESSION, "acceptForSession"),
            (ApprovalDecision.DENY, "decline"),
            (ApprovalDecision.CANCEL_TURN, "cancel"),
        )
        for index, (decision, _provider_value) in enumerate(decisions, start=1):
            turn = await adapter.send_turn(
                "approval-session", RuntimeTurnInput(text=f"turn {index}")
            )
            opened = await self.next_event(
                stream,
                "interaction.opened",
                request_type="command_execution_approval",
            )
            self.assertEqual(opened.interaction_id, opened.payload["interaction_id"])
            public = json.dumps(dict(opened.payload), sort_keys=True)
            self.assertNotIn("LEAK_APPROVAL_COMMAND", public)
            self.assertNotIn("LEAK_APPROVAL_REASON", public)
            self.assertEqual("Approve command", opened.payload["title"])
            self.assertEqual("[redacted]", opened.payload["input"]["command"])
            self.assertEqual("[redacted]", opened.payload["detail"])
            await adapter.respond_to_approval(
                "approval-session", opened.interaction_id or "", decision
            )
            with self.assertRaises(RuntimeInteractionResolvedError):
                await adapter.respond_to_approval(
                    "approval-session", opened.interaction_id or "", decision
                )
            resolved = await self.next_event(stream, "interaction.resolved")
            self.assertEqual(opened.interaction_id, resolved.interaction_id)
            self.assertEqual(opened.interaction_id, resolved.payload["interaction_id"])
            completed = await self.next_event(stream, "turn.completed")
            self.assertEqual(turn.turn_id, completed.turn_id)

        responses = [
            message
            for message in self.transcript(transcript)
            if str(message.get("id") or "").startswith("srv-approval-")
            and "result" in message
        ]
        self.assertEqual(
            [provider for _decision, provider in decisions],
            [message["result"]["decision"] for message in responses],
        )

        file_adapter, _file_transcript = self.adapter(approval="file")
        await file_adapter.start_session(
            RuntimeSessionSpec(session_id="file-session", cwd=self.root)
        )
        file_stream = file_adapter.events("file-session")
        await file_adapter.send_turn(
            "file-session", RuntimeTurnInput(text="change the file")
        )
        file_opened = await self.next_event(
            file_stream,
            "interaction.opened",
            request_type="file_change_approval",
        )
        await file_adapter.respond_to_approval(
            "file-session",
            file_opened.interaction_id or "",
            ApprovalDecision.DENY,
        )

        input_adapter, input_transcript = self.adapter(
            approval="command", user_input=True
        )
        await input_adapter.start_session(
            RuntimeSessionSpec(session_id="input-session", cwd=self.root)
        )
        input_stream = input_adapter.events("input-session")
        await input_adapter.send_turn(
            "input-session", RuntimeTurnInput(text="ask a question")
        )
        approval = await self.next_event(
            input_stream,
            "interaction.opened",
            request_type="command_execution_approval",
        )
        await input_adapter.respond_to_approval(
            "input-session",
            approval.interaction_id or "",
            ApprovalDecision.APPROVE_ONCE,
        )
        user_input = await self.next_event(
            input_stream,
            "interaction.opened",
            request_type="tool_user_input",
        )
        self.assertEqual("environment", user_input.payload["questions"][0]["id"])
        with self.assertRaises(RuntimeInvalidDecisionError):
            await input_adapter.respond_to_user_input(
                "input-session",
                user_input.interaction_id or "",
                {"wrong-question": "Staging"},
            )
        await input_adapter.respond_to_user_input(
            "input-session",
            user_input.interaction_id or "",
            {"environment": "Staging"},
        )
        with self.assertRaises(RuntimeInteractionResolvedError):
            await input_adapter.respond_to_user_input(
                "input-session",
                user_input.interaction_id or "",
                {"environment": "Staging"},
            )
        input_resolved = await self.next_event(input_stream, "interaction.resolved")
        self.assertNotIn("Staging", json.dumps(dict(input_resolved.payload)))
        input_responses = [
            message
            for message in self.transcript(input_transcript)
            if str(message.get("id") or "").startswith("srv-user-input-")
            and "result" in message
        ]
        self.assertEqual(
            {"answers": {"environment": {"answers": ["Staging"]}}},
            input_responses[-1]["result"],
        )

    async def test_resume_fallback_and_nonrecoverable_resume_error(self) -> None:
        fallback, fallback_transcript = self.adapter(scenario="resume-missing")
        await fallback.start_session(
            RuntimeSessionSpec(
                session_id="resume-fallback",
                cwd=self.root,
                resume_cursor={"thread_id": "missing-thread"},
            )
        )
        methods = [
            message.get("method") for message in self.transcript(fallback_transcript)
        ]
        self.assertEqual(
            ["initialize", "initialized", "thread/resume", "thread/start"],
            methods[:4],
        )
        warning = await self.next_event(
            fallback.events("resume-fallback"), "runtime.warning"
        )
        self.assertEqual("thread_resume_fell_back", warning.payload["code"])

        fatal, fatal_transcript = self.adapter(scenario="resume-fatal")
        with self.assertRaises(RuntimeRequestError) as captured:
            await fatal.start_session(
                RuntimeSessionSpec(
                    session_id="resume-fatal",
                    cwd=self.root,
                    resume_cursor={"thread_id": "forbidden-thread"},
                )
            )
        self.assertEqual(-32003, captured.exception.request_code)
        fatal_methods = [
            message.get("method") for message in self.transcript(fatal_transcript)
        ]
        self.assertNotIn("thread/start", fatal_methods)
        self.assertEqual((), await fatal.list_sessions())

        invalid, _invalid_transcript = self.adapter(scenario="invalid-initialize")
        with self.assertRaises(RuntimeProtocolError):
            await invalid.start_session(
                RuntimeSessionSpec(session_id="invalid-init", cwd=self.root)
            )

    async def test_unsubscribed_global_stream_cannot_deadlock_session_stream(self) -> None:
        adapter, _transcript = self.adapter(
            burst=40,
            turn_status="hold",
            event_queue_size=16,
        )
        await adapter.start_session(
            RuntimeSessionSpec(session_id="burst-session", cwd=self.root)
        )
        stream = adapter.events("burst-session")

        async def wait_for_last_burst() -> RuntimeEvent:
            async for event in stream:
                if event.item_id == "burst-39":
                    return event
            raise AssertionError("burst event stream ended")

        last_event = asyncio.create_task(wait_for_last_burst())
        turn = await asyncio.wait_for(
            adapter.send_turn(
                "burst-session", RuntimeTurnInput(text="produce many events")
            ),
            timeout=3,
        )
        self.assertEqual("turn-1", turn.turn_id)
        self.assertEqual("burst-39", (await asyncio.wait_for(last_event, 3)).item_id)
        self.assertEqual(0, adapter._events.qsize())

    async def test_eof_fails_pending_request_and_emits_failed_session(self) -> None:
        adapter, _transcript = self.adapter(scenario="eof-on-read")
        await adapter.start_session(
            RuntimeSessionSpec(session_id="eof-session", cwd=self.root)
        )
        stream = adapter.events("eof-session")
        with self.assertRaises(RuntimeTransportError):
            await adapter.read_thread("eof-session")
        failed = await self.next_event(stream, "session.failed")
        self.assertEqual("runtime_exited", failed.payload["error"])
        await self.next_event(stream, "session.exited")
        self.assertEqual((), await adapter.list_sessions())

    async def test_request_timeout_is_typed_and_session_remains_usable(self) -> None:
        adapter, _transcript = self.adapter(
            scenario="timeout-on-read", request_timeout=0.05
        )
        await adapter.start_session(
            RuntimeSessionSpec(session_id="timeout-session", cwd=self.root)
        )
        with self.assertRaises(RuntimeOperationTimeoutError) as captured:
            await adapter.read_thread("timeout-session")
        self.assertEqual("thread/read", captured.exception.operation)
        sessions = await adapter.list_sessions()
        self.assertEqual(1, len(sessions))
        self.assertEqual("ready", sessions[0].state)

    async def test_failed_turn_is_recoverable_and_canonical(self) -> None:
        adapter, _transcript = self.adapter(turn_status="failed")
        await adapter.start_session(
            RuntimeSessionSpec(session_id="failed-turn-session", cwd=self.root)
        )
        stream = adapter.events("failed-turn-session")
        first = await adapter.send_turn(
            "failed-turn-session", RuntimeTurnInput(text="first")
        )
        failed = await self.next_event(stream, "turn.failed")
        self.assertEqual(first.turn_id, failed.turn_id)
        self.assertEqual("provider_turn_failed", failed.payload["error"])
        sessions = await adapter.list_sessions()
        self.assertEqual("ready", sessions[0].state)
        second = await adapter.send_turn(
            "failed-turn-session", RuntimeTurnInput(text="retry")
        )
        self.assertEqual("turn-2", second.turn_id)


class AsyncJsonRpcPeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_out_of_order_responses_and_inbound_request_do_not_block_reader(
        self,
    ) -> None:
        server_done: asyncio.Future[tuple[dict[str, Any], dict[str, Any]]] = (
            asyncio.get_running_loop().create_future()
        )

        async def server_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            first = json.loads(await reader.readline())
            second = json.loads(await reader.readline())
            messages = [
                {
                    "id": "server-wait",
                    "method": "fixture/wait",
                    "params": {"value": 42},
                },
                {"id": second["id"], "result": second["params"]},
                {"id": first["id"], "result": first["params"]},
            ]
            writer.write(
                b"".join(
                    json.dumps(message, separators=(",", ":")).encode() + b"\n"
                    for message in messages
                )
            )
            await writer.drain()
            inbound_response = json.loads(await reader.readline())
            writer.write(
                json.dumps(
                    {"id": "unknown", "method": "fixture/unknown", "params": {}},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            await writer.drain()
            unknown_response = json.loads(await reader.readline())
            if not server_done.done():
                server_done.set_result((inbound_response, unknown_response))
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(server_handler, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        peer = AsyncJsonRpcPeer(reader, writer)
        release = asyncio.Event()
        inbound_request_id: asyncio.Future[str | int] = (
            asyncio.get_running_loop().create_future()
        )

        async def inbound(params: Any) -> Any:
            request_id = peer.current_inbound_request_id()
            self.assertIsNotNone(request_id)
            if not inbound_request_id.done():
                inbound_request_id.set_result(request_id)  # type: ignore[arg-type]
            await release.wait()
            return {"echo": params["value"]}

        peer.handle_request("fixture/wait", inbound)
        await peer.start()
        first = asyncio.create_task(peer.request("first", {"name": "one"}))
        second = asyncio.create_task(peer.request("second", {"name": "two"}))
        self.assertEqual(
            [{"name": "one"}, {"name": "two"}],
            await asyncio.wait_for(asyncio.gather(first, second), timeout=2),
        )
        request_id = await asyncio.wait_for(inbound_request_id, timeout=2)
        release.set()
        await asyncio.wait_for(peer.wait_inbound_response(request_id), timeout=2)
        inbound_response, unknown_response = await asyncio.wait_for(server_done, 2)
        self.assertEqual({"echo": 42}, inbound_response["result"])
        self.assertEqual(-32601, unknown_response["error"]["code"])
        self.assertNotIn("jsonrpc", inbound_response)
        await peer.close()
        await peer.close()

    async def test_malformed_and_eof_fail_pending_requests(self) -> None:
        for malformed in (True, False):
            with self.subTest(malformed=malformed):
                async def server_handler(
                    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
                ) -> None:
                    await reader.readline()
                    if malformed:
                        writer.write(b"{not-json}\n")
                        await writer.drain()
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_server(server_handler, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                peer = AsyncJsonRpcPeer(reader, writer)
                await peer.start()
                expected = JsonRpcProtocolError if malformed else JsonRpcTransportError
                with self.assertRaises(expected):
                    await asyncio.wait_for(peer.request("pending", {}), timeout=2)
                self.assertIsInstance(peer.terminal_error, expected)
                await peer.close()
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
