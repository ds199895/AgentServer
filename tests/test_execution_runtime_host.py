from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from app.execution.device_runtime import DeviceRuntimeService, DeviceRuntimeStore
from app.execution.device_runtime_api import build_device_runtime_router
from app.execution.runtime_host import (
    AdapterContext,
    DeviceEventSpool,
    DeviceRuntimeCycleError,
    DeviceRuntimeHost,
    DeviceRuntimeHTTPClient,
    DeviceRuntimeProtocolError,
    RuntimeEvent,
    load_private_text_file,
)
from app.execution.runtime_host_cli import (
    _codex_binary,
    _default_adapter_registry,
    _host as cli_host,
    build_parser as build_runtime_parser,
)
from app.execution.store import ExecutionStore
from app.execution.runtime_adapters.base import (
    ApprovalDecision,
    RuntimeAdapter as TypedRuntimeAdapter,
    RuntimeAdapterRegistry,
    RuntimeCapabilities,
    RuntimeEvent as TypedRuntimeEvent,
    RuntimeProbe,
    RuntimeSession,
    RuntimeSessionSpec,
    RuntimeThreadSnapshot,
    RuntimeTurn,
    RuntimeTurnInput,
)


_DEVICE_CREDENTIAL = "asdc1.credential-1.device-secret"
_REENROLLED_CREDENTIAL = "asdc1.credential-reenrolled.replacement-secret"


class FakeDeviceClient:
    def __init__(self) -> None:
        self.enroll_calls: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.server_commands: list[dict[str, Any]] = []
        self.command_requests: list[dict[str, Any]] = []
        self.acknowledgements: list[tuple[str, dict[str, Any]]] = []
        self.fail_ack_responses = 0
        self.event_batches: list[list[dict[str, Any]]] = []
        self.event_identities: list[dict[str, Any]] = []
        self.fail_event_responses = 0
        self.closed = False
        self.rotations = 0
        self.rotation_requests: list[dict[str, Any]] = []

    async def enroll(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.enroll_calls.append(dict(payload))
        return {"credential": _DEVICE_CREDENTIAL}

    async def heartbeat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.heartbeats.append(dict(payload))
        return {"server_time": time.time(), "lease": {"revision": 1}}

    async def commands(
        self,
        *,
        after_sequence: int,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> Mapping[str, Any]:
        identity = {
            "device_id": device_id,
            "instance_id": instance_id,
            "boot_id": boot_id,
            "runtime_session_id": runtime_session_id,
            "generation": generation,
        }
        self.command_requests.append(
            {"after_sequence": after_sequence, **identity}
        )
        commands: list[dict[str, Any]] = []
        for original in self.server_commands:
            if int(original["sequence"]) <= after_sequence:
                continue
            value = dict(original)
            payload = dict(value.get("payload") or {})
            payload.update(
                {
                    "device_id": device_id,
                    "runtime_session_id": runtime_session_id,
                    "runtime_generation": generation,
                }
            )
            value["payload"] = payload
            commands.append(value)
        return {
            "server_time": time.time(),
            "commands": commands,
        }

    async def acknowledge_command(
        self,
        command_id: str,
        payload: Mapping[str, Any],
        *,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> Mapping[str, Any]:
        body = {
            **dict(payload),
            "device_id": device_id,
            "instance_id": instance_id,
            "boot_id": boot_id,
            "runtime_session_id": runtime_session_id,
            "generation": generation,
        }
        self.acknowledgements.append((command_id, body))
        if self.fail_ack_responses:
            self.fail_ack_responses -= 1
            # The server is assumed to have committed the body before the
            # response was lost. The retry must carry the same ack_id.
            raise OSError("simulated lost ACK response")
        return {
            "command": {
                "command_id": command_id,
                "status": body["status"],
            }
        }

    async def send_events(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> Mapping[str, Any]:
        batch = [dict(event) for event in events]
        self.event_batches.append(batch)
        self.event_identities.append(
            {
                "device_id": device_id,
                "instance_id": instance_id,
                "boot_id": boot_id,
                "runtime_session_id": runtime_session_id,
                "generation": generation,
            }
        )
        if self.fail_event_responses:
            self.fail_event_responses -= 1
            raise OSError("simulated lost event response")
        accepted = max(int(event["producer"]["seq"]) for event in batch)
        return {
            "accepted_through_seq": accepted,
            "missing_ranges": [],
            "results": [
                {
                    "event_id": str(event["event_id"]),
                    "producer_seq": int(event["producer"]["seq"]),
                    "status": "accepted",
                }
                for event in batch
            ],
        }

    async def rotate_credential(
        self,
        payload: Mapping[str, Any],
        *,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> Mapping[str, Any]:
        self.rotations += 1
        self.rotation_requests.append(
            {
                **dict(payload),
                "device_id": device_id,
                "instance_id": instance_id,
                "boot_id": boot_id,
                "runtime_session_id": runtime_session_id,
                "generation": generation,
            }
        )
        return {
            "credential": (
                f"asdc1.credential-{self.rotations + 1}."
                f"rotated-secret-{self.rotations}"
            )
        }

    async def close(self) -> None:
        self.closed = True


class PagedFakeDeviceClient(FakeDeviceClient):
    def __init__(self, pages: Sequence[Mapping[str, Any]]) -> None:
        super().__init__()
        self.pages = [dict(page) for page in pages]

    async def commands(
        self,
        *,
        after_sequence: int,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> Mapping[str, Any]:
        identity = {
            "device_id": device_id,
            "instance_id": instance_id,
            "boot_id": boot_id,
            "runtime_session_id": runtime_session_id,
            "generation": generation,
        }
        self.command_requests.append(
            {"after_sequence": after_sequence, **identity}
        )
        page = self.pages.pop(0)
        commands: list[dict[str, Any]] = []
        for original in page.get("commands", []):
            value = dict(original)
            payload = dict(value.get("payload") or {})
            payload.update(
                {
                    "device_id": device_id,
                    "runtime_session_id": runtime_session_id,
                    "runtime_generation": generation,
                }
            )
            value["payload"] = payload
            commands.append(value)
        return {
            "server_time": time.time(),
            "commands": commands,
            "next_sequence": page.get("next_sequence", after_sequence),
        }


class FakeAdapter:
    def __init__(self, context: AdapterContext, emit) -> None:
        self.context = context
        self.emit = emit
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def probe(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("probe", dict(payload)))
        return {"available": True, "provider": self.context.provider}

    async def start_session(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("start_session", dict(payload)))
        await self.emit(
            RuntimeEvent(
                "runtime.session.started",
                {"provider": self.context.provider},
            )
        )
        return {"session_id": self.context.session_id}

    async def stop_session(self, payload: Mapping[str, Any]) -> None:
        self.calls.append(("stop_session", dict(payload)))

    async def start_turn(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("start_turn", dict(payload)))
        await self.emit(
            RuntimeEvent(
                "runtime.turn.started",
                {"turn_id": payload.get("turn_id")},
            )
        )
        return {"turn_id": payload.get("turn_id")}

    async def interrupt_turn(self, payload: Mapping[str, Any]) -> None:
        self.calls.append(("interrupt_turn", dict(payload)))

    async def respond_to_approval(self, payload: Mapping[str, Any]) -> None:
        self.calls.append(("respond_to_approval", dict(payload)))

    async def respond_to_user_input(self, payload: Mapping[str, Any]) -> None:
        self.calls.append(("respond_to_user_input", dict(payload)))

    async def close(self) -> None:
        self.closed = True


class FakeAdapterFactory:
    def __init__(self, adapter_type=FakeAdapter) -> None:
        self.adapter_type = adapter_type
        self.adapters: list[FakeAdapter] = []

    def __call__(self, context: AdapterContext, emit):
        adapter = self.adapter_type(context, emit)
        self.adapters.append(adapter)
        return adapter


class CancelledStartAdapter(FakeAdapter):
    async def start_session(self, payload: Mapping[str, Any]) -> None:
        self.calls.append(("start_session", dict(payload)))
        raise asyncio.CancelledError


_TYPED_END = object()


class FakeTypedAdapter(TypedRuntimeAdapter):
    provider = "typed"
    capabilities = RuntimeCapabilities(
        interrupt=True,
        approvals=True,
        user_input=True,
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.queue: asyncio.Queue[TypedRuntimeEvent | object] = asyncio.Queue()
        self.session_id: str | None = None
        self.closed = False

    async def probe(self) -> RuntimeProbe:
        self.calls.append(("probe", None))
        return RuntimeProbe(available=True, version="test-1")

    async def start_session(self, spec: RuntimeSessionSpec) -> RuntimeSession:
        self.calls.append(("start_session", spec))
        self.session_id = spec.session_id
        await self.queue.put(
            TypedRuntimeEvent(
                event_id="provider-session-started",
                provider=self.provider,
                session_id=spec.session_id,
                type="session.started",
                payload={"provider_session_id": "provider-session"},
                occurred_at=time.time(),
            )
        )
        return RuntimeSession(
            session_id=spec.session_id,
            provider=self.provider,
            state="ready",
            cwd=str(spec.cwd),
        )

    async def send_turn(
        self, session_id: str, turn: RuntimeTurnInput
    ) -> RuntimeTurn:
        self.calls.append(("send_turn", (session_id, turn)))
        await self.queue.put(
            TypedRuntimeEvent(
                event_id="provider-turn-started",
                provider=self.provider,
                session_id=session_id,
                type="turn.started",
                payload={"activity": "thinking"},
                turn_id="provider-turn",
                occurred_at=time.time(),
            )
        )
        return RuntimeTurn(session_id=session_id, turn_id="provider-turn")

    async def interrupt_turn(
        self, session_id: str, turn_id: str | None = None
    ) -> None:
        self.calls.append(("interrupt_turn", (session_id, turn_id)))

    async def respond_to_approval(
        self,
        session_id: str,
        interaction_id: str,
        decision: ApprovalDecision | str,
    ) -> None:
        self.calls.append(
            ("respond_to_approval", (session_id, interaction_id, decision))
        )

    async def respond_to_user_input(
        self,
        session_id: str,
        interaction_id: str,
        answers: Mapping[str, str | Sequence[str]],
    ) -> None:
        self.calls.append(
            ("respond_to_user_input", (session_id, interaction_id, dict(answers)))
        )

    async def read_thread(self, session_id: str) -> RuntimeThreadSnapshot:
        return RuntimeThreadSnapshot(thread_id=session_id)

    async def rollback_thread(
        self, session_id: str, num_turns: int
    ) -> RuntimeThreadSnapshot:
        return RuntimeThreadSnapshot(thread_id=session_id)

    async def stop_session(self, session_id: str) -> None:
        self.calls.append(("stop_session", session_id))
        await self.queue.put(
            TypedRuntimeEvent(
                event_id="provider-session-stopped",
                provider=self.provider,
                session_id=session_id,
                type="session.stopped",
                occurred_at=time.time(),
            )
        )
        await self.queue.put(_TYPED_END)

    async def list_sessions(self) -> tuple[RuntimeSession, ...]:
        return ()

    def events(self, session_id: str | None = None) -> AsyncIterator[TypedRuntimeEvent]:
        async def consume() -> AsyncIterator[TypedRuntimeEvent]:
            while True:
                value = await self.queue.get()
                if value is _TYPED_END:
                    return
                assert isinstance(value, TypedRuntimeEvent)
                yield value

        return consume()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.queue.put(_TYPED_END)


class FakeTypedAdapterFactory:
    capabilities = FakeTypedAdapter.capabilities
    transport = "typed-test"
    version = "test-1"

    def __init__(self) -> None:
        self.adapters: list[FakeTypedAdapter] = []
        self.validated_specs: list[RuntimeSessionSpec] = []

    def validate_session(self, spec: RuntimeSessionSpec) -> None:
        self.validated_specs.append(spec)
        if spec.resume_cursor == {}:
            raise ValueError("resume_cursor requires thread_id")

    def __call__(self) -> FakeTypedAdapter:
        adapter = FakeTypedAdapter()
        self.adapters.append(adapter)
        return adapter


class CloseCountingTypedAdapter(FakeTypedAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        await super().close()


class EndingTypedAdapter(CloseCountingTypedAdapter):
    def __init__(self, failure: BaseException | None) -> None:
        super().__init__()
        self.failure = failure

    def events(self, session_id: str | None = None) -> AsyncIterator[TypedRuntimeEvent]:
        async def consume() -> AsyncIterator[TypedRuntimeEvent]:
            value = await self.queue.get()
            assert isinstance(value, TypedRuntimeEvent)
            yield value
            if self.failure is not None:
                raise self.failure

        return consume()


class TerminalEndingTypedAdapter(CloseCountingTypedAdapter):
    async def start_session(self, spec: RuntimeSessionSpec) -> RuntimeSession:
        session = await super().start_session(spec)
        await self.queue.put(
            TypedRuntimeEvent(
                event_id="provider-session-failed",
                provider=self.provider,
                session_id=spec.session_id,
                type="session.failed",
                payload={"error": "provider_runtime_exited"},
                occurred_at=time.time(),
            )
        )
        await self.queue.put(
            TypedRuntimeEvent(
                event_id="provider-session-exited",
                provider=self.provider,
                session_id=spec.session_id,
                type="session.exited",
                payload={"exit_kind": "error", "exit_code": 1},
                occurred_at=time.time(),
            )
        )
        return session

    def events(self, session_id: str | None = None) -> AsyncIterator[TypedRuntimeEvent]:
        async def consume() -> AsyncIterator[TypedRuntimeEvent]:
            for _index in range(3):
                value = await self.queue.get()
                assert isinstance(value, TypedRuntimeEvent)
                yield value

        return consume()


def command(
    sequence: int,
    command_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    device_id: str = "device-1",
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "command_id": f"command-{sequence}",
        "owner_id": "alice",
        "target_kind": "device",
        "target_id": device_id,
        "type": command_type,
        "payload": dict(payload or {}),
        "status": "delivered",
        "expected_revision": None,
        "created_at": time.time(),
        "expires_at": None,
    }


class DeviceRuntimeHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def host(
        self,
        client: FakeDeviceClient,
        *,
        registry: Any = None,
        state_name: str = "state",
        credential_rotation_window: float = 7 * 24 * 60 * 60,
        event_spool_limit: int = 10_000,
        event_dead_letter_limit: int = 10_000,
    ) -> DeviceRuntimeHost:
        host = DeviceRuntimeHost(
            device_id="device-1",
            base_url="https://agentserver.example",
            state_dir=self.root / state_name,
            adapter_registry=registry,
            client=client,
            heartbeat_interval=3600,
            poll_interval=0.01,
            initial_backoff=0.01,
            max_backoff=0.04,
            credential_rotation_window=credential_rotation_window,
            event_spool_limit=event_spool_limit,
            event_dead_letter_limit=event_dead_letter_limit,
        )
        if not host.credential_file.exists:
            host.credential_file.replace(_DEVICE_CREDENTIAL)
        return host

    async def wait_for_task_done(self, task: asyncio.Task[Any]) -> None:
        for _attempt in range(100):
            if task.done():
                return
            await asyncio.sleep(0.01)
        self.fail("background task did not finish")

    async def fill_event_spool(
        self,
        host: DeviceRuntimeHost,
        *,
        reserve: int = 0,
    ) -> None:
        for index in range(host.event_spool.max_events - reserve):
            await host._emit_adapter_event(
                RuntimeEvent(
                    "item.completed",
                    {"prefill": index},
                ),
                default_session_id=None,
            )

    def assert_runtime_identity(
        self, host: DeviceRuntimeHost, value: Mapping[str, Any]
    ) -> None:
        self.assertEqual(host.device_id, value["device_id"])
        self.assertEqual(host.instance_id, value["instance_id"])
        self.assertEqual(host.boot_id, value["boot_id"])
        self.assertEqual(host.runtime_session_id, value["runtime_session_id"])
        self.assertEqual(host.generation, value["generation"])

    async def test_enrollment_is_one_time_and_identity_survives_boots(self) -> None:
        token_path = self.root / "enroll.token"
        token_path.write_text("one-time-enrollment-token\n", encoding="utf-8")
        token_path.chmod(0o600)
        client = FakeDeviceClient()
        first = DeviceRuntimeHost(
            device_id="device-1",
            base_url="https://agentserver.example",
            state_dir=self.root / "state",
            client=client,
        )
        first_result = await first.enroll_from_file(token_path)
        first_instance = first.instance_id
        first_boot = first.boot_id
        first_generation = first.generation
        self.assertFalse(first_result["already_enrolled"])
        self.assertEqual(first.boot_id, first.runtime_session_id)
        self.assertEqual(1, first_generation)
        self.assertEqual(1, len(client.enroll_calls))
        self.assertEqual(
            "one-time-enrollment-token",
            client.enroll_calls[0]["enrollment_token"],
        )
        await first.close()

        second_client = FakeDeviceClient()
        second = DeviceRuntimeHost(
            device_id="device-1",
            base_url="https://agentserver.example",
            state_dir=self.root / "state",
            client=second_client,
        )
        second_result = await second.enroll_from_file(token_path)
        self.assertTrue(second_result["already_enrolled"])
        self.assertEqual([], second_client.enroll_calls)
        self.assertEqual(first_instance, second.instance_id)
        self.assertNotEqual(first_boot, second.boot_id)
        self.assertEqual(second.boot_id, second.runtime_session_id)
        self.assertEqual(first_generation + 1, second.generation)
        self.assertEqual(_DEVICE_CREDENTIAL, second.credential_file.load())
        await second.close()

    async def test_explicit_reenrollment_replaces_a_revoked_local_credential(self) -> None:
        token_path = self.root / "replacement-enrollment.token"
        token_path.write_text("fresh-one-time-token\n", encoding="utf-8")
        token_path.chmod(0o600)

        class ReplacementClient(FakeDeviceClient):
            async def enroll(
                self, payload: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                self.enroll_calls.append(dict(payload))
                return {"credential": _REENROLLED_CREDENTIAL}

        client = ReplacementClient()
        host = self.host(client)
        self.assertEqual(_DEVICE_CREDENTIAL, host.credential_file.load())
        result = await host.enroll_from_file(
            token_path,
            replace_existing=True,
        )
        self.assertFalse(result["already_enrolled"])
        self.assertTrue(result["replaced_existing"])
        self.assertEqual(1, len(client.enroll_calls))
        self.assertEqual(
            _REENROLLED_CREDENTIAL,
            host.credential_file.load(),
        )
        await host.close()

    async def test_state_directory_has_one_owner_and_generation_is_locked(self) -> None:
        first = self.host(FakeDeviceClient())
        with self.assertRaisesRegex(RuntimeError, "another AgentServer runtime"):
            DeviceRuntimeHost(
                device_id="device-1",
                base_url="https://agentserver.example",
                state_dir=self.root / "state",
                client=FakeDeviceClient(),
            )
        self.assertEqual("1", first.generation_path.read_text(encoding="utf-8").strip())
        await first.close()

        second = self.host(FakeDeviceClient())
        self.assertEqual(2, second.generation)
        await second.close()

    async def test_two_sessions_route_to_distinct_adapters_and_events(self) -> None:
        client = FakeDeviceClient()
        factory = FakeAdapterFactory()
        client.server_commands = [
            command(
                1,
                "session.start",
                {"session_id": "session-a", "provider": "fake"},
            ),
            command(
                2,
                "session.start",
                {"session_id": "session-b", "provider": "fake"},
            ),
            command(
                3,
                "session.turn",
                {
                    "session_id": "session-a",
                    "turn_id": "turn-a",
                    "input": "first",
                },
            ),
            command(
                4,
                "session.turn",
                {
                    "session_id": "session-b",
                    "turn_id": "turn-b",
                    "input": "second",
                },
            ),
            command(
                5,
                "session.respond",
                {
                    "session_id": "session-a",
                    "request_id": "approval-1",
                    "response": {"decision": "accept"},
                },
            ),
            command(
                6,
                "session.respond",
                {
                    "session_id": "session-b",
                    "request_id": "input-1",
                    "response": {"answers": {"question-1": "yes"}},
                },
            ),
            command(
                7,
                "session.interrupt",
                {"session_id": "session-a", "turn_id": "turn-a"},
            ),
        ]
        host = self.host(client, registry={"fake": factory})
        await host.poll_commands()

        self.assertEqual({"session-a", "session-b"}, set(host.sessions))
        by_session = {
            adapter.context.session_id: adapter for adapter in factory.adapters
        }
        self.assertIn(
            (
                "start_turn",
                {
                    "session_id": "session-a",
                    "turn_id": "turn-a",
                    "input": "first",
                },
            ),
            by_session["session-a"].calls,
        )
        self.assertNotIn(
            (
                "start_turn",
                {
                    "session_id": "session-b",
                    "turn_id": "turn-b",
                    "input": "second",
                },
            ),
            by_session["session-a"].calls,
        )
        self.assertIn(
            (
                "respond_to_user_input",
                {
                    "session_id": "session-b",
                    "request_id": "input-1",
                    "response": {"answers": {"question-1": "yes"}},
                },
            ),
            by_session["session-b"].calls,
        )
        pending = host.event_spool.pending()
        self.assertEqual(
            ["session-a", "session-b", "session-a", "session-b"],
            [event["session_id"] for event in pending],
        )
        self.assertEqual(7, len(client.acknowledgements))
        self.assertEqual([], host.command_journal.pending(now=time.time()))
        await host.close()

    async def test_frozen_commands_use_typed_adapter_spi_and_event_pump(self) -> None:
        client = FakeDeviceClient()
        factory = FakeTypedAdapterFactory()
        client.server_commands = [
            command(
                1,
                "session.start",
                {
                    "session_id": "typed-session",
                    "provider": "typed",
                    "workspace": str(self.root),
                    "options": {
                        "permission_mode": "workspace-write",
                        "model": "test-model",
                    },
                },
            ),
            command(
                2,
                "session.turn",
                {"session_id": "typed-session", "input": "hello"},
            ),
            command(
                3,
                "session.respond",
                {
                    "session_id": "typed-session",
                    "request_id": "approval-1",
                    "response": {"decision": "accept"},
                },
            ),
            command(
                4,
                "session.respond",
                {
                    "session_id": "typed-session",
                    "request_id": "input-1",
                    "response": {"answers": {"question-1": ["yes"]}},
                },
            ),
            command(
                5,
                "session.interrupt",
                {"session_id": "typed-session", "turn_id": "provider-turn"},
            ),
            command(6, "session.stop", {"session_id": "typed-session"}),
        ]
        host = self.host(client, registry={"typed": factory})
        await host.poll_commands()

        [adapter] = factory.adapters
        start_call = next(value for name, value in adapter.calls if name == "start_session")
        self.assertIsInstance(start_call, RuntimeSessionSpec)
        self.assertEqual(str(self.root), str(start_call.cwd))
        turn_call = next(value for name, value in adapter.calls if name == "send_turn")
        self.assertIsInstance(turn_call[1], RuntimeTurnInput)
        self.assertEqual("hello", turn_call[1].text)
        self.assertIn(
            (
                "respond_to_approval",
                ("typed-session", "approval-1", ApprovalDecision.APPROVE_ONCE),
            ),
            adapter.calls,
        )
        self.assertIn(
            (
                "respond_to_user_input",
                ("typed-session", "input-1", {"question-1": ["yes"]}),
            ),
            adapter.calls,
        )
        self.assertIn(
            ("interrupt_turn", ("typed-session", "provider-turn")),
            adapter.calls,
        )
        self.assertTrue(adapter.closed)
        self.assertEqual({}, host.sessions)
        pending = host.event_spool.pending()
        self.assertEqual(
            ["session.started", "turn.started", "session.stopped"],
            [event["type"] for event in pending],
        )
        self.assertEqual("provider-turn", pending[1]["payload"]["turn_id"])
        self.assertEqual(6, len(client.acknowledgements))
        await host.close()

    async def test_spool_full_event_pump_is_reaped_after_flush_frees_space(
        self,
    ) -> None:
        client = FakeDeviceClient()
        adapter = CloseCountingTypedAdapter()
        host = self.host(
            client,
            registry={"typed": lambda: adapter},
            state_name="event-pump-spool-full",
            event_spool_limit=32,
        )
        await self.fill_event_spool(host, reserve=1)
        await host._start_session(
            {
                "session_id": "spool-full-session",
                "provider": "typed",
                "workspace": str(self.root),
                "options": {},
            }
        )
        handle = host._sessions["spool-full-session"]
        assert handle.event_task is not None
        for _attempt in range(100):
            if len(host.event_spool) == host.event_spool.max_events:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(host.event_spool.max_events, len(host.event_spool))
        await adapter.queue.put(
            TypedRuntimeEvent(
                event_id="provider-event-that-fills-the-pump",
                provider=adapter.provider,
                session_id="spool-full-session",
                type="item.completed",
                occurred_at=time.time(),
            )
        )
        await self.wait_for_task_done(handle.event_task)
        self.assertEqual("DeviceEventSpoolFull", handle.event_task_outcome)

        result = await host.run_once(force_heartbeat=True)

        self.assertEqual(
            {"reaped": ["spool-full-session"]},
            result["event_pumps"],
        )
        self.assertNotIn("spool-full-session", host.sessions)
        self.assertTrue(adapter.closed)
        self.assertEqual(1, adapter.close_calls)
        [failed] = host.event_spool.pending()
        self.assertEqual("session.failed", failed["type"])
        self.assertEqual("spool-full-session", failed["session_id"])
        self.assertEqual(
            {
                "error": "runtime_event_pump_failed",
                "error_code": "event_pump_failed",
                "cause": "DeviceEventSpoolFull",
            },
            failed["payload"],
        )
        self.assertNotIn(
            failed["event_id"],
            [event["event_id"] for event in client.event_batches[0]],
        )

        await host.run_once()
        self.assertEqual(0, len(host.event_spool))
        self.assertEqual(1, adapter.close_calls)
        await host.close()
        self.assertEqual(1, adapter.close_calls)

    async def test_spool_full_pump_failure_remains_retryable_when_flush_fails(
        self,
    ) -> None:
        client = FakeDeviceClient()
        client.fail_event_responses = 1
        adapter = CloseCountingTypedAdapter()
        host = self.host(
            client,
            registry={"typed": lambda: adapter},
            state_name="event-pump-spool-retry",
            event_spool_limit=32,
        )
        await self.fill_event_spool(host, reserve=1)
        await host._start_session(
            {
                "session_id": "retry-pump-session",
                "provider": "typed",
                "workspace": str(self.root),
                "options": {},
            }
        )
        handle = host._sessions["retry-pump-session"]
        assert handle.event_task is not None
        for _attempt in range(100):
            if len(host.event_spool) == host.event_spool.max_events:
                break
            await asyncio.sleep(0.01)
        await adapter.queue.put(
            TypedRuntimeEvent(
                event_id="provider-event-before-retry",
                provider=adapter.provider,
                session_id="retry-pump-session",
                type="item.completed",
                occurred_at=time.time(),
            )
        )
        await self.wait_for_task_done(handle.event_task)

        with self.assertRaises(DeviceRuntimeCycleError) as raised:
            await host.run_once(force_heartbeat=True)

        self.assertEqual(("events", "event_pumps"), raised.exception.components)
        self.assertIn("retry-pump-session", host.sessions)
        self.assertTrue(handle.event_gate.accepting)
        self.assertEqual("DeviceEventSpoolFull", handle.event_task_outcome)
        self.assertEqual(host.event_spool.max_events, len(host.event_spool))
        self.assertFalse(adapter.closed)
        self.assertEqual(0, adapter.close_calls)

        recovered = await host.run_once()
        self.assertEqual(
            {"reaped": ["retry-pump-session"]},
            recovered["event_pumps"],
        )
        self.assertNotIn("retry-pump-session", host.sessions)
        [failed] = host.event_spool.pending()
        self.assertEqual("session.failed", failed["type"])
        self.assertEqual("DeviceEventSpoolFull", failed["payload"]["cause"])
        self.assertTrue(adapter.closed)
        self.assertEqual(1, adapter.close_calls)
        await host.close()

    async def test_active_event_pump_exception_and_eof_are_fail_closed(self) -> None:
        cases = (
            (
                "exception",
                EndingTypedAdapter(RuntimeError("provider stream failed")),
                "RuntimeError",
            ),
            ("eof", EndingTypedAdapter(None), "completed"),
        )
        for label, adapter, expected_cause in cases:
            with self.subTest(label=label):
                client = FakeDeviceClient()
                host = self.host(
                    client,
                    registry={"typed": lambda adapter=adapter: adapter},
                    state_name=f"event-pump-{label}",
                )
                session_id = f"pump-{label}"
                await host._start_session(
                    {
                        "session_id": session_id,
                        "provider": "typed",
                        "workspace": str(self.root),
                        "options": {},
                    }
                )
                handle = host._sessions[session_id]
                assert handle.event_task is not None
                await self.wait_for_task_done(handle.event_task)

                result = await host.run_once(force_heartbeat=True)

                self.assertEqual({"reaped": [session_id]}, result["event_pumps"])
                self.assertNotIn(session_id, host.sessions)
                self.assertEqual(1, adapter.close_calls)
                [failed] = host.event_spool.pending()
                self.assertEqual("session.failed", failed["type"])
                self.assertEqual(expected_cause, failed["payload"]["cause"])
                await host.close()
                self.assertEqual(1, adapter.close_calls)

    async def test_terminal_event_then_eof_reaps_without_duplicate_failure(
        self,
    ) -> None:
        client = FakeDeviceClient()
        adapter = TerminalEndingTypedAdapter()
        host = self.host(
            client,
            registry={"typed": lambda: adapter},
            state_name="event-pump-terminal-eof",
        )
        await host._start_session(
            {
                "session_id": "terminal-eof",
                "provider": "typed",
                "workspace": str(self.root),
                "options": {},
            }
        )
        handle = host._sessions["terminal-eof"]
        assert handle.event_task is not None
        await self.wait_for_task_done(handle.event_task)
        self.assertTrue(handle.event_gate.terminal_event_spooled)

        result = await host.run_once(force_heartbeat=True)

        self.assertEqual({"reaped": ["terminal-eof"]}, result["event_pumps"])
        self.assertNotIn("terminal-eof", host.sessions)
        self.assertEqual(1, adapter.close_calls)
        self.assertEqual(0, len(host.event_spool))
        self.assertEqual(
            ["session.started", "session.failed", "session.exited"],
            [event["type"] for event in client.event_batches[0]],
        )
        self.assertEqual(
            1,
            sum(
                event["type"] == "session.failed"
                for batch in client.event_batches
                for event in batch
            ),
        )
        await host.close()
        self.assertEqual(1, adapter.close_calls)

    async def test_cancelled_reaper_does_not_duplicate_durable_failure(
        self,
    ) -> None:
        client = FakeDeviceClient()
        adapter = EndingTypedAdapter(RuntimeError("provider stream failed"))
        host = self.host(
            client,
            registry={"typed": lambda: adapter},
            state_name="event-pump-reaper-cancel",
        )
        await host._start_session(
            {
                "session_id": "cancelled-reaper",
                "provider": "typed",
                "workspace": str(self.root),
                "options": {},
            }
        )
        handle = host._sessions["cancelled-reaper"]
        assert handle.event_task is not None
        await self.wait_for_task_done(handle.event_task)
        await host.flush_events()

        original_emit = host._emit_adapter_event
        committed = asyncio.Event()
        release = asyncio.Event()

        async def commit_then_wait(
            value: RuntimeEvent | TypedRuntimeEvent | Mapping[str, Any],
            *,
            default_session_id: str | None,
        ) -> dict[str, Any]:
            result = await original_emit(
                value,
                default_session_id=default_session_id,
            )
            if isinstance(value, RuntimeEvent) and value.type == "session.failed":
                committed.set()
                await release.wait()
            return result

        host._emit_adapter_event = commit_then_wait  # type: ignore[method-assign]
        reaper = asyncio.create_task(host._reap_failed_event_pumps())
        await asyncio.wait_for(committed.wait(), timeout=1)
        reaper.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(reaper, timeout=1)
        delattr(host, "_emit_adapter_event")

        self.assertIn("cancelled-reaper", host.sessions)
        self.assertTrue(handle.event_pump_failure_enqueued)
        self.assertEqual(1, len(host.event_spool))
        self.assertEqual(0, adapter.close_calls)

        recovered = await host._reap_failed_event_pumps()

        self.assertEqual({"reaped": ["cancelled-reaper"]}, recovered)
        [failed] = host.event_spool.pending()
        self.assertEqual("session.failed", failed["type"])
        self.assertEqual(1, adapter.close_calls)
        await host.close()
        self.assertEqual(1, adapter.close_calls)

    async def test_normal_typed_stop_does_not_trigger_event_pump_reaper(self) -> None:
        client = FakeDeviceClient()
        adapter = CloseCountingTypedAdapter()
        host = self.host(
            client,
            registry={"typed": lambda: adapter},
            state_name="event-pump-normal-stop",
        )
        await host._start_session(
            {
                "session_id": "normally-stopped",
                "provider": "typed",
                "workspace": str(self.root),
                "options": {},
            }
        )

        await host._stop_session({"session_id": "normally-stopped"})
        reaped = await host._reap_failed_event_pumps()

        self.assertEqual({"reaped": []}, reaped)
        self.assertEqual(1, adapter.close_calls)
        self.assertEqual(
            ["session.started", "session.stopped"],
            [event["type"] for event in host.event_spool.pending()],
        )
        await host.close()
        self.assertEqual(1, adapter.close_calls)

    async def test_workspace_browse_lists_only_directories(self) -> None:
        root = self.root / "browse-root"
        (root / "project-a").mkdir(parents=True)
        (root / "project-b").mkdir()
        (root / ".hidden").mkdir()
        (root / "notes.txt").write_text("not a directory", encoding="utf-8")

        client = FakeDeviceClient()
        client.server_commands = [
            command(1, "workspace.browse", {"path": str(root)})
        ]
        host = self.host(client, registry={"fake": FakeAdapterFactory()})
        await host.poll_commands()
        await host.flush_command_acks()

        [(_command_id, body)] = client.acknowledgements
        self.assertEqual("completed", body["status"])
        result = body["payload"]
        self.assertEqual(str(root), result["path"])
        self.assertEqual(str(root.parent), result["parent"])
        # Directories only, hidden entries and plain files excluded.
        self.assertEqual(
            ["project-a", "project-b"],
            [entry["name"] for entry in result["entries"]],
        )
        self.assertFalse(result["truncated"])
        await host.close()

    async def test_workspace_browse_rejects_a_missing_directory(self) -> None:
        client = FakeDeviceClient()
        client.server_commands = [
            command(1, "workspace.browse", {"path": str(self.root / "nope")})
        ]
        host = self.host(client, registry={"fake": FakeAdapterFactory()})
        # Rejected in the preflight, like every other invalid command, so it
        # never reaches a handler and never degrades the poll cycle.
        await host.poll_commands()

        [(_command_id, body)] = client.acknowledgements
        self.assertEqual("rejected", body["status"])
        self.assertEqual("invalid_command", body["payload"]["error_code"])
        await host.close()

    async def test_typed_runtime_registry_is_consumed_directly(self) -> None:
        client = FakeDeviceClient()
        factory = FakeTypedAdapterFactory()
        registry = RuntimeAdapterRegistry()
        registry.register("typed", factory)
        client.server_commands = [
            command(1, "runtime.probe", {"provider": "typed"})
        ]
        host = self.host(client, registry=registry)
        await host.poll_commands()
        [adapter] = factory.adapters
        self.assertIn(("probe", None), adapter.calls)
        self.assertTrue(adapter.closed)
        await host.close()

    async def test_cli_run_defaults_to_codex_but_injection_takes_precedence(self) -> None:
        arguments = argparse.Namespace(
            device_id="device-1",
            base_url="https://agentserver.example",
            state_dir=self.root / "cli-default",
            heartbeat_interval=10.0,
            poll_interval=1.0,
            max_backoff=30.0,
            command="run",
        )
        default_host = cli_host(arguments, None)
        self.assertEqual({"codex"}, set(default_host.adapter_registry))
        await default_host.close()

        arguments.state_dir = self.root / "cli-injected"
        injected = FakeAdapterFactory()
        injected_host = cli_host(arguments, {"fake": injected})
        self.assertEqual({"fake"}, set(injected_host.adapter_registry))
        await injected_host.close()

    async def test_lost_ack_response_reuses_stable_ack_id(self) -> None:
        client = FakeDeviceClient()
        factory = FakeAdapterFactory()
        client.server_commands = [
            command(1, "runtime.probe", {"provider": "fake"})
        ]
        client.fail_ack_responses = 1
        host = self.host(client, registry={"fake": factory})
        with self.assertRaises(DeviceRuntimeCycleError):
            await host.poll_commands()
        self.assertEqual(1, len(client.acknowledgements))
        first_body = client.acknowledgements[0][1]

        await host.flush_command_acks()
        self.assertEqual(2, len(client.acknowledgements))
        self.assertEqual(first_body, client.acknowledgements[1][1])
        self.assertTrue(first_body["ack_id"])
        self.assert_runtime_identity(host, first_body)
        summary = host.command_journal.status_summary(now=time.time())
        self.assertEqual([], summary["uncertain"])
        await host.close()

    async def test_events_wait_for_durable_command_ack_settlement(self) -> None:
        class OrderedClient(FakeDeviceClient):
            def __init__(self) -> None:
                super().__init__()
                self.operations: list[str] = []

            async def acknowledge_command(
                self,
                command_id: str,
                payload: Mapping[str, Any],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                self.operations.append("ack")
                return await super().acknowledge_command(
                    command_id,
                    payload,
                    device_id=device_id,
                    instance_id=instance_id,
                    boot_id=boot_id,
                    runtime_session_id=runtime_session_id,
                    generation=generation,
                )

            async def send_events(
                self,
                events: Sequence[Mapping[str, Any]],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                self.operations.append("events")
                return await super().send_events(
                    events,
                    device_id=device_id,
                    instance_id=instance_id,
                    boot_id=boot_id,
                    runtime_session_id=runtime_session_id,
                    generation=generation,
                )

        client = OrderedClient()
        factory = FakeAdapterFactory()
        client.server_commands = [
            command(
                1,
                "session.start",
                {
                    "session_id": "ack-before-event",
                    "provider": "fake",
                    "workspace": str(self.root),
                },
            )
        ]
        client.fail_ack_responses = 1
        host = self.host(
            client,
            registry={"fake": factory},
            state_name="ack-before-events",
        )
        with self.assertRaises(DeviceRuntimeCycleError):
            await host.poll_commands()
        [queued] = host.event_spool.pending()
        self.assertEqual("ack-before-event", queued["session_id"])
        self.assertEqual([], client.event_batches)

        client.operations.clear()
        client.fail_ack_responses = 1
        with self.assertRaises(DeviceRuntimeCycleError):
            await host.flush_events()
        self.assertEqual(["ack"], client.operations)
        self.assertEqual([queued], host.event_spool.pending())
        self.assertEqual([], client.event_batches)

        client.operations.clear()
        await host.flush_events()
        self.assertEqual(["ack", "events"], client.operations)
        self.assertEqual(0, len(host.event_spool))
        self.assertEqual(1, len(client.event_batches))
        self.assertEqual(queued, client.event_batches[0][0])
        await host.close()

    async def test_uncertain_handler_result_remains_an_event_causal_barrier(
        self,
    ) -> None:
        client = FakeDeviceClient()
        client.server_commands = [
            command(
                1,
                "session.start",
                {"session_id": "uncertain-session", "provider": "fake"},
            )
        ]
        host = self.host(client, registry={"fake": FakeAdapterFactory()})

        with mock.patch.object(
            host.command_journal,
            "prepare_ack",
            side_effect=OSError("ACK journal write failed"),
        ):
            with self.assertRaises(DeviceRuntimeCycleError):
                await host.poll_commands()

        [event] = host.event_spool.pending()
        self.assertEqual("uncertain-session", event["session_id"])
        [barrier] = host.command_journal.causal_barriers(
            now=host._journal_time()
        )
        self.assertEqual("uncertain", barrier["status"])

        with self.assertRaises(DeviceRuntimeCycleError) as blocked:
            await host.flush_events()
        self.assertEqual(("causal_barrier",), blocked.exception.components)
        self.assertEqual([], client.event_batches)
        self.assertEqual([event], host.event_spool.pending())
        await host.close()

    async def test_expired_unsettled_ack_remains_an_event_causal_barrier(
        self,
    ) -> None:
        class RejectExpiredAckClient(FakeDeviceClient):
            async def acknowledge_command(self, *args, **kwargs):
                response = await super().acknowledge_command(*args, **kwargs)
                if len(self.acknowledgements) > 1:
                    raise DeviceRuntimeProtocolError(
                        "server rejected the ACK after command expiry"
                    )
                return response

        class AcceptedStartAdapter(FakeAdapter):
            async def start_session(
                self, payload: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                await super().start_session(payload)
                return {
                    "status": "accepted",
                    "payload": {"session_id": self.context.session_id},
                }

        client = RejectExpiredAckClient()
        expires_at = time.time() + 60
        start = command(
            1,
            "session.start",
            {"session_id": "expired-ack-session", "provider": "fake"},
        )
        start["expires_at"] = expires_at
        client.server_commands = [start]
        client.fail_ack_responses = 1
        host = self.host(
            client,
            registry={"fake": FakeAdapterFactory(AcceptedStartAdapter)},
        )

        with self.assertRaises(DeviceRuntimeCycleError):
            await host.poll_commands()
        [event] = host.event_spool.pending()
        host._server_clock_anchor = (expires_at + 1, time.monotonic())

        with self.assertRaises(DeviceRuntimeCycleError) as blocked:
            await host.flush_events()
        self.assertEqual(("causal_barrier",), blocked.exception.components)
        [barrier] = host.command_journal.causal_barriers(
            now=host._journal_time()
        )
        self.assertEqual("expired", barrier["status"])
        self.assertEqual("abandoned", barrier["delivery_state"])
        self.assertEqual([], client.event_batches)
        self.assertEqual([event], host.event_spool.pending())
        await host.close()

    async def test_lost_accepted_ack_response_recovers_after_local_ttl(
        self,
    ) -> None:
        class AcceptedStartAdapter(FakeAdapter):
            async def start_session(
                self, payload: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                await super().start_session(payload)
                return {
                    "status": "accepted",
                    "payload": {"session_id": self.context.session_id},
                }

        client = FakeDeviceClient()
        expires_at = time.time() + 60
        start = command(
            1,
            "session.start",
            {"session_id": "lost-response-session", "provider": "fake"},
        )
        start["expires_at"] = expires_at
        client.server_commands = [start]
        client.fail_ack_responses = 1
        host = self.host(
            client,
            registry={"fake": FakeAdapterFactory(AcceptedStartAdapter)},
        )

        with self.assertRaises(DeviceRuntimeCycleError):
            await host.poll_commands()
        [event] = host.event_spool.pending()
        first_ack = dict(client.acknowledgements[0][1])
        host._server_clock_anchor = (expires_at + 1, time.monotonic())

        await host.flush_events()

        self.assertEqual(2, len(client.acknowledgements))
        self.assertEqual(first_ack, client.acknowledgements[1][1])
        self.assertEqual(
            [],
            host.command_journal.causal_barriers(now=host._journal_time()),
        )
        self.assertEqual(0, len(host.event_spool))
        self.assertEqual([[event]], client.event_batches)
        await host.close()

    async def test_ack_flush_propagates_cancellation_and_releases_command_lock(
        self,
    ) -> None:
        class BlockingAckClient(FakeDeviceClient):
            def __init__(self) -> None:
                super().__init__()
                self.block_ack = False
                self.ack_started = asyncio.Event()

            async def acknowledge_command(self, *args, **kwargs):
                if self.block_ack:
                    self.ack_started.set()
                    await asyncio.Future()
                return await super().acknowledge_command(*args, **kwargs)

        client = BlockingAckClient()
        client.server_commands = [
            command(1, "runtime.probe", {"provider": "fake"})
        ]
        client.fail_ack_responses = 1
        host = self.host(client, registry={"fake": FakeAdapterFactory()})
        with self.assertRaises(DeviceRuntimeCycleError):
            await host.poll_commands()

        client.block_ack = True
        task = asyncio.create_task(host.flush_command_acks())
        await asyncio.wait_for(client.ack_started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        client.block_ack = False
        await asyncio.wait_for(host.flush_command_acks(), timeout=1)
        await host.close()

    async def test_run_once_propagates_heartbeat_cancellation(self) -> None:
        class BlockingHeartbeatClient(FakeDeviceClient):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_started = asyncio.Event()

            async def heartbeat(
                self, payload: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                self.heartbeats.append(dict(payload))
                self.heartbeat_started.set()
                await asyncio.Future()
                raise AssertionError("unreachable")

        client = BlockingHeartbeatClient()
        host = self.host(client)
        task = asyncio.create_task(host.run_once(force_heartbeat=True))
        await asyncio.wait_for(client.heartbeat_started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual([], client.command_requests)
        await host.close()

    async def test_filtered_stale_page_advances_cursor_across_restart(self) -> None:
        stale_page = PagedFakeDeviceClient(
            [{"commands": [], "next_sequence": 100}]
        )
        first = self.host(stale_page)
        await first.poll_commands()
        self.assertEqual(100, first.command_journal.cursor)
        self.assertEqual(0, stale_page.command_requests[0]["after_sequence"])
        await first.close()

        current = command(101, "runtime.probe", {"provider": "fake"})
        current_pages = PagedFakeDeviceClient(
            [
                {"commands": [current], "next_sequence": 101},
                {"commands": [], "next_sequence": 50},
            ]
        )
        factory = FakeAdapterFactory()
        second = self.host(current_pages, registry={"fake": factory})
        self.assertEqual(100, second.command_journal.cursor)
        await second.poll_commands()
        self.assertEqual(100, current_pages.command_requests[0]["after_sequence"])
        self.assertEqual(101, second.command_journal.cursor)
        self.assertEqual(1, len(current_pages.acknowledgements))

        await second.poll_commands()
        self.assertEqual(101, current_pages.command_requests[1]["after_sequence"])
        self.assertEqual(101, second.command_journal.cursor)
        await second.close()

    async def test_invalid_server_page_cursor_is_rejected_without_advancing(self) -> None:
        client = PagedFakeDeviceClient(
            [{"commands": [], "next_sequence": "not-an-integer"}]
        )
        host = self.host(client)
        with self.assertRaisesRegex(
            DeviceRuntimeProtocolError, "invalid next_sequence"
        ):
            await host.poll_commands()
        self.assertEqual(0, host.command_journal.cursor)
        await host.close()

    async def test_preflight_failures_are_rejected_before_provider_side_effects(
        self,
    ) -> None:
        client = FakeDeviceClient()
        factory = FakeTypedAdapterFactory()
        client.server_commands = [
            command(
                1,
                "session.start",
                {
                    "session_id": "missing-provider",
                    "provider": "not-installed",
                    "workspace": str(self.root),
                },
            ),
            command(
                2,
                "session.start",
                {
                    "session_id": "missing-workspace",
                    "provider": "typed",
                    "workspace": str(self.root / "does-not-exist"),
                },
            ),
            command(
                3,
                "session.start",
                {
                    "session_id": "invalid-resume",
                    "provider": "typed",
                    "workspace": str(self.root),
                    "options": {"resume_cursor": {}},
                },
            ),
        ]
        host = self.host(client, registry={"typed": factory})
        await host.poll_commands()

        self.assertEqual([], factory.adapters)
        self.assertEqual(3, len(client.acknowledgements))
        for command_id, acknowledgement in client.acknowledgements:
            self.assertIn(command_id, {"command-1", "command-2", "command-3"})
            self.assertEqual("rejected", acknowledgement["status"])
            self.assertEqual(
                "invalid_command", acknowledgement["payload"]["error_code"]
            )
            recorded = host.command_journal.command(command_id, now=time.time())
            assert recorded is not None
            self.assertEqual("rejected", recorded["status"])
            self.assertEqual(0, recorded["handler_attempts"])
        self.assertEqual([], host.command_journal.status_summary()["uncertain"])
        await host.close()

    async def test_missing_session_stop_emits_a_durable_terminal_event(self) -> None:
        client = FakeDeviceClient()
        client.server_commands = [
            command(1, "session.stop", {"session_id": "missing-session"})
        ]
        host = self.host(client)
        await host.poll_commands()

        self.assertEqual("completed", client.acknowledgements[0][1]["status"])
        [event] = host.event_spool.pending()
        self.assertEqual("session.stopped", event["type"])
        self.assertEqual("missing-session", event["session_id"])
        self.assertTrue(event["payload"]["already_stopped"])
        await host.close()

    async def test_restart_quarantines_uncertain_work_from_old_generation(self) -> None:
        client = FakeDeviceClient()
        factory = FakeAdapterFactory(CancelledStartAdapter)
        client.server_commands = [
            command(
                1,
                "session.start",
                {"session_id": "session-a", "provider": "fake"},
            )
        ]
        first = self.host(client, registry={"fake": factory})
        with self.assertRaises(asyncio.CancelledError):
            await first.poll_commands()
        executing = first.command_journal.pending(now=time.time())
        self.assertEqual("executing", executing[0]["status"])
        await first.close()

        second_client = FakeDeviceClient()
        second = self.host(
            second_client,
            registry={"fake": FakeAdapterFactory()},
        )
        self.assertEqual(1, second.stale_generation_commands)
        self.assertEqual(0, second.stale_generation_command_acks)
        self.assertEqual([], second.command_journal.pending(now=time.time()))
        quarantined = second.command_journal.command(
            "command-1", now=time.time()
        )
        assert quarantined is not None
        self.assertEqual("quarantined", quarantined["status"])
        self.assertEqual(
            "stale_runtime_generation", quarantined["quarantine_reason"]
        )
        self.assertEqual(
            [], second.command_journal.causal_barriers(now=time.time())
        )
        await second.poll_commands()
        self.assertEqual([], second_client.acknowledgements)
        await second.close()

    async def test_restart_quarantines_old_ack_and_unblocks_new_events(self) -> None:
        first_client = FakeDeviceClient()
        first_client.server_commands = [
            command(1, "runtime.probe", {"provider": "fake"})
        ]
        # poll_commands and close both retry. Keep the response-loss window
        # open until a replacement Host allocates the next generation.
        first_client.fail_ack_responses = 100
        first = self.host(
            first_client,
            registry={"fake": FakeAdapterFactory()},
            state_name="stale-command-ack",
        )
        with self.assertRaises(DeviceRuntimeCycleError):
            await first.poll_commands()
        [old_ack] = first.command_journal.pending_acks(now=time.time())
        self.assertEqual("command-1", old_ack.command_id)

        settled = command(2, "runtime.probe", {"provider": "fake"})
        settled["payload"].update(
            {
                "device_id": first.device_id,
                "runtime_session_id": first.runtime_session_id,
                "runtime_generation": first.generation,
            }
        )
        first.command_journal.record_server_commands([settled], now=time.time())
        first.command_journal.begin_handler("command-2", now=time.time())
        settled_ack = first.command_journal.prepare_ack(
            command_id="command-2",
            status="completed",
            payload={"settled": True},
            now=time.time(),
        )
        first.command_journal.mark_acknowledged(
            settled_ack.ack_id,
            {"command": {"command_id": "command-2", "status": "completed"}},
            now=time.time(),
        )
        self.assertEqual(2, first.command_journal.cursor)
        await first.close()

        second_client = FakeDeviceClient()
        second = self.host(
            second_client,
            registry={"fake": FakeAdapterFactory()},
            state_name="stale-command-ack",
        )
        self.assertEqual(1, second.stale_generation_commands)
        self.assertEqual(1, second.stale_generation_command_acks)
        self.assertEqual([], second.command_journal.replayable_acks(now=time.time()))
        self.assertEqual([], second.command_journal.causal_barriers(now=time.time()))
        self.assertEqual(2, second.command_journal.cursor)
        quarantined_ack = second.command_journal.acknowledgement(
            command_id="command-1", status="completed"
        )
        assert quarantined_ack is not None
        self.assertEqual("quarantined", quarantined_ack.delivery_state)
        quarantined_command = second.command_journal.command(
            "command-1", now=time.time()
        )
        assert quarantined_command is not None
        self.assertEqual("completed", quarantined_command["status"])
        self.assertEqual(
            "stale_runtime_generation",
            quarantined_command["quarantine_reason"],
        )
        settled_command = second.command_journal.command(
            "command-2", now=time.time()
        )
        assert settled_command is not None
        self.assertEqual("completed", settled_command["status"])
        self.assertIsNone(settled_command["quarantine_reason"])

        new_event = await second._emit_adapter_event(
            RuntimeEvent(
                type="session.started",
                session_id="new-generation-session",
                payload={"provider_session_id": "new-provider-session"},
            ),
            default_session_id="new-generation-session",
        )
        await second.flush_events()
        self.assertEqual([[new_event]], second_client.event_batches)
        self.assertEqual([], second_client.acknowledgements)
        await second.close()

    async def test_event_response_loss_replays_exact_envelope(self) -> None:
        client = FakeDeviceClient()
        factory = FakeAdapterFactory()
        host = self.host(client, registry={"fake": factory})
        await host._start_session(
            {
                "session_id": "session-a",
                "provider": "fake",
                "workspace": "/workspace",
            }
        )
        [event] = host.event_spool.pending()
        [adapter] = factory.adapters
        client.fail_event_responses = 1
        with self.assertRaises(OSError):
            await host.flush_events()
        self.assertEqual(1, len(host.event_spool))
        self.assertEqual(0, host.event_spool.dead_letter_count)
        self.assertIn("session-a", host.sessions)
        self.assertFalse(adapter.closed)

        await host.flush_events()
        self.assertEqual(0, len(host.event_spool))
        self.assertEqual(2, len(client.event_batches))
        self.assertEqual(client.event_batches[0], client.event_batches[1])
        self.assertEqual(event["event_id"], client.event_batches[1][0]["event_id"])
        self.assert_runtime_identity(host, event)
        self.assertEqual(client.event_identities[0], client.event_identities[1])
        self.assertIn("session-a", host.sessions)
        self.assertFalse(adapter.closed)
        await host.close()

    async def test_event_results_settle_accept_reject_and_retry_atomically(self) -> None:
        class ClassifiedEventClient(FakeDeviceClient):
            async def send_events(
                self,
                events: Sequence[Mapping[str, Any]],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                response = dict(
                    await super().send_events(
                        events,
                        device_id=device_id,
                        instance_id=instance_id,
                        boot_id=boot_id,
                        runtime_session_id=runtime_session_id,
                        generation=generation,
                    )
                )
                if len(self.event_batches) == 1:
                    first, second, third = events
                    response["results"] = [
                        {
                            "event_id": first["event_id"],
                            "producer_seq": first["producer"]["seq"],
                            "status": "rejected",
                            "permanent": True,
                            "error_code": "retention_quota",
                            "reason": "server_retention_quota",
                        },
                        {
                            "event_id": second["event_id"],
                            "producer_seq": second["producer"]["seq"],
                            "status": "duplicate",
                        },
                        {
                            "event_id": third["event_id"],
                            "producer_seq": third["producer"]["seq"],
                            "status": "rejected",
                            "permanent": False,
                            "error_code": "temporarily_unavailable",
                            "reason": "retry_later",
                        },
                    ]
                return response

        client = ClassifiedEventClient()
        host = self.host(client, state_name="event-settlement")
        queued = []
        for index in range(3):
            queued.append(
                await host._emit_adapter_event(
                    RuntimeEvent(
                        "item.completed",
                        {"index": index},
                        session_id="settlement-session",
                    ),
                    default_session_id="settlement-session",
                )
            )

        response = await host.flush_events()
        self.assertEqual(
            {
                "accepted": 0,
                "duplicate": 1,
                "rejected": 1,
                "retryable": 1,
                "missing": 0,
                "permanent_rejections": [
                    {
                        "event_id": queued[0]["event_id"],
                        "producer_seq": queued[0]["producer"]["seq"],
                        "session_id": "settlement-session",
                        "error_code": "retention_quota",
                        "reason": "server_retention_quota",
                    }
                ],
            },
            response["settlement"],
        )
        [retryable] = host.event_spool.pending()
        self.assertEqual(queued[2]["event_id"], retryable["event_id"])
        self.assertEqual(1, host.event_spool.dead_letter_count)
        [rejected] = host.event_spool.dead_letters()
        self.assertEqual(queued[0], rejected["envelope"])
        self.assertEqual("retention_quota", rejected["error_code"])
        self.assertEqual("server_retention_quota", rejected["reason"])

        await host.flush_events()
        self.assertEqual(0, len(host.event_spool))
        self.assertEqual(1, host.event_spool.dead_letter_count)
        self.assertEqual(
            [queued[2]["event_id"]],
            [event["event_id"] for event in client.event_batches[1]],
        )
        await host.close()

    async def test_permanent_rejections_fail_close_a_session_once_without_emitting(
        self,
    ) -> None:
        class RejectEveryEventClient(FakeDeviceClient):
            async def send_events(
                self,
                events: Sequence[Mapping[str, Any]],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                response = dict(
                    await super().send_events(
                        events,
                        device_id=device_id,
                        instance_id=instance_id,
                        boot_id=boot_id,
                        runtime_session_id=runtime_session_id,
                        generation=generation,
                    )
                )
                response["results"] = [
                    {
                        "event_id": event["event_id"],
                        "producer_seq": event["producer"]["seq"],
                        "status": "rejected",
                        "permanent": True,
                        "error_code": "session_state_conflict",
                        "reason": "session_state_conflict",
                    }
                    for event in events
                ]
                return response

        class CloseEmittingAdapter(FakeAdapter):
            def __init__(self, context: AdapterContext, emit) -> None:
                super().__init__(context, emit)
                self.close_calls = 0
                self.close_emit_result: Mapping[str, Any] | None = None

            async def close(self) -> None:
                self.close_calls += 1
                self.closed = True
                self.close_emit_result = await self.emit(
                    RuntimeEvent(
                        "session.failed",
                        {"error": "closed_after_permanent_rejection"},
                    )
                )

        client = RejectEveryEventClient()
        factory = FakeAdapterFactory(CloseEmittingAdapter)
        host = self.host(
            client,
            registry={"fake": factory},
            state_name="event-permanent-fail-close",
        )
        await host._start_session(
            {
                "session_id": "poisoned-session",
                "provider": "fake",
                "workspace": "/workspace",
            }
        )
        await host._emit_adapter_event(
            RuntimeEvent(
                "item.completed",
                {"index": 2},
                session_id="poisoned-session",
            ),
            default_session_id="poisoned-session",
        )
        [adapter] = factory.adapters
        self.assertEqual(2, len(host.event_spool))

        response = await host.flush_events()

        self.assertEqual(2, response["settlement"]["rejected"])
        self.assertEqual(2, len(response["settlement"]["permanent_rejections"]))
        self.assertNotIn("poisoned-session", host.sessions)
        self.assertTrue(adapter.closed)
        self.assertEqual(1, adapter.close_calls)
        self.assertEqual(
            {"suppressed": True, "session_id": "poisoned-session"},
            adapter.close_emit_result,
        )
        self.assertEqual(0, len(host.event_spool))
        self.assertEqual(2, host.event_spool.dead_letter_count)

        await host.flush_events()
        self.assertEqual(1, adapter.close_calls)
        self.assertEqual(0, len(host.event_spool))
        await host.close()

    async def test_nonpermanent_event_settlements_never_close_sessions(self) -> None:
        class NonPermanentClient(FakeDeviceClient):
            async def send_events(
                self,
                events: Sequence[Mapping[str, Any]],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                response = dict(
                    await super().send_events(
                        events,
                        device_id=device_id,
                        instance_id=instance_id,
                        boot_id=boot_id,
                        runtime_session_id=runtime_session_id,
                        generation=generation,
                    )
                )
                first, second, third, _missing = events
                response["results"] = [
                    {
                        "event_id": first["event_id"],
                        "producer_seq": first["producer"]["seq"],
                        "status": "accepted",
                    },
                    {
                        "event_id": second["event_id"],
                        "producer_seq": second["producer"]["seq"],
                        "status": "duplicate",
                    },
                    {
                        "event_id": third["event_id"],
                        "producer_seq": third["producer"]["seq"],
                        "status": "rejected",
                        "permanent": False,
                        "error_code": "temporarily_unavailable",
                        "reason": "retry_later",
                    },
                ]
                return response

        client = NonPermanentClient()
        factory = FakeAdapterFactory()
        host = self.host(
            client,
            registry={"fake": factory},
            state_name="event-nonpermanent-sessions",
        )
        session_ids = [f"session-{index}" for index in range(4)]
        for session_id in session_ids:
            await host._start_session(
                {
                    "session_id": session_id,
                    "provider": "fake",
                    "workspace": "/workspace",
                }
            )

        response = await host.flush_events()

        self.assertEqual(1, response["settlement"]["accepted"])
        self.assertEqual(1, response["settlement"]["duplicate"])
        self.assertEqual(1, response["settlement"]["retryable"])
        self.assertEqual(1, response["settlement"]["missing"])
        self.assertEqual([], response["settlement"]["permanent_rejections"])
        self.assertEqual(set(session_ids), set(host.sessions))
        self.assertTrue(all(not adapter.closed for adapter in factory.adapters))
        self.assertEqual(2, len(host.event_spool))
        self.assertEqual(0, host.event_spool.dead_letter_count)
        await host.close()

    async def test_permanent_rejection_cancels_typed_pump_before_adapter_close(
        self,
    ) -> None:
        class RejectEventClient(FakeDeviceClient):
            async def send_events(
                self,
                events: Sequence[Mapping[str, Any]],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                response = dict(
                    await super().send_events(
                        events,
                        device_id=device_id,
                        instance_id=instance_id,
                        boot_id=boot_id,
                        runtime_session_id=runtime_session_id,
                        generation=generation,
                    )
                )
                [event] = events
                response["results"] = [
                    {
                        "event_id": event["event_id"],
                        "producer_seq": event["producer"]["seq"],
                        "status": "rejected",
                        "permanent": True,
                        "error_code": "session_state_conflict",
                        "reason": "session_state_conflict",
                    }
                ]
                return response

        class CloseEmittingTypedAdapter(FakeTypedAdapter):
            async def close(self) -> None:
                if self.closed:
                    return
                assert self.session_id is not None
                await self.queue.put(
                    TypedRuntimeEvent(
                        event_id="close-generated-terminal-event",
                        provider=self.provider,
                        session_id=self.session_id,
                        type="session.failed",
                        payload={"error": "closed_after_permanent_rejection"},
                        occurred_at=time.time(),
                    )
                )
                await super().close()

        adapter = CloseEmittingTypedAdapter()
        client = RejectEventClient()
        host = self.host(
            client,
            registry={"typed": lambda: adapter},
            state_name="typed-event-permanent-fail-close",
        )
        await host._start_session(
            {
                "session_id": "typed-poisoned-session",
                "provider": "typed",
                "workspace": str(self.root),
                "options": {},
            }
        )
        handle = host._sessions["typed-poisoned-session"]
        for _attempt in range(100):
            if len(host.event_spool) == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(1, len(host.event_spool))

        await host.flush_events()

        self.assertNotIn("typed-poisoned-session", host.sessions)
        self.assertTrue(adapter.closed)
        self.assertIsNotNone(handle.event_task)
        assert handle.event_task is not None
        self.assertTrue(handle.event_task.done())
        self.assertEqual(0, len(host.event_spool))
        self.assertEqual(1, host.event_spool.dead_letter_count)
        self.assertNotIn(
            "close-generated-terminal-event",
            [event["event_id"] for event in host.event_spool.pending()],
        )
        await host.close()

    async def test_fail_close_attempts_every_session_when_one_adapter_close_fails(
        self,
    ) -> None:
        class SometimesFailingCloseAdapter(FakeAdapter):
            def __init__(self, context: AdapterContext, emit) -> None:
                super().__init__(context, emit)
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1
                self.closed = True
                if self.context.session_id == "close-fails":
                    raise RuntimeError("simulated adapter close failure")

        client = FakeDeviceClient()
        factory = FakeAdapterFactory(SometimesFailingCloseAdapter)
        host = self.host(
            client,
            registry={"fake": factory},
            state_name="event-fail-close-errors",
        )
        for session_id in ("close-fails", "close-succeeds"):
            await host._start_session(
                {
                    "session_id": session_id,
                    "provider": "fake",
                    "workspace": "/workspace",
                }
            )

        with self.assertRaises(DeviceRuntimeCycleError) as raised:
            await host._fail_closed_sessions(
                [
                    {"session_id": "close-fails"},
                    {"session_id": "close-succeeds"},
                ]
            )

        self.assertEqual(
            {"session_close:close-fails"},
            set(raised.exception.components),
        )
        self.assertEqual({}, host.sessions)
        self.assertEqual([1, 1], [adapter.close_calls for adapter in factory.adapters])
        self.assertTrue(all(adapter.closed for adapter in factory.adapters))
        await host.close()

    async def test_missing_and_malformed_event_results_never_delete_affected_rows(
        self,
    ) -> None:
        class PartialThenMalformedClient(FakeDeviceClient):
            async def send_events(
                self,
                events: Sequence[Mapping[str, Any]],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                response = dict(
                    await super().send_events(
                        events,
                        device_id=device_id,
                        instance_id=instance_id,
                        boot_id=boot_id,
                        runtime_session_id=runtime_session_id,
                        generation=generation,
                    )
                )
                if len(self.event_batches) == 1:
                    response["results"] = response["results"][:1]
                elif len(self.event_batches) == 2:
                    response["results"].append(
                        {
                            "event_id": "not-in-the-delivered-batch",
                            "producer_seq": 999,
                            "status": "accepted",
                        }
                    )
                return response

        client = PartialThenMalformedClient()
        factory = FakeAdapterFactory()
        host = self.host(
            client,
            registry={"fake": factory},
            state_name="event-result-validation",
        )
        await host._start_session(
            {
                "session_id": "validation",
                "provider": "fake",
                "workspace": "/workspace",
            }
        )
        [first] = host.event_spool.pending()
        [adapter] = factory.adapters
        second = await host._emit_adapter_event(
            RuntimeEvent("item.completed", {"index": 2}, session_id="validation"),
            default_session_id="validation",
        )

        await host.flush_events()
        [missing] = host.event_spool.pending()
        self.assertEqual(first["event_id"], client.event_batches[0][0]["event_id"])
        self.assertEqual(second["event_id"], missing["event_id"])

        with self.assertRaisesRegex(
            DeviceRuntimeProtocolError, "do not match the delivered batch"
        ):
            await host.flush_events()
        [unchanged] = host.event_spool.pending()
        self.assertEqual(second["event_id"], unchanged["event_id"])
        self.assertEqual(0, host.event_spool.dead_letter_count)
        self.assertIn("validation", host.sessions)
        self.assertFalse(adapter.closed)

        await host.flush_events()
        self.assertEqual(0, len(host.event_spool))
        await host.close()

    async def test_http_event_failure_never_dead_letters_or_deletes(self) -> None:
        class FailedHTTPEventClient(FakeDeviceClient):
            async def send_events(
                self,
                events: Sequence[Mapping[str, Any]],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                self.event_batches.append([dict(event) for event in events])
                raise DeviceRuntimeProtocolError(
                    "device runtime events failed with HTTP 500"
                )

        client = FailedHTTPEventClient()
        factory = FakeAdapterFactory()
        host = self.host(
            client,
            registry={"fake": factory},
            state_name="event-http-failure",
        )
        await host._start_session(
            {
                "session_id": "failed-http",
                "provider": "fake",
                "workspace": "/workspace",
            }
        )
        [event] = host.event_spool.pending()
        [adapter] = factory.adapters
        with self.assertRaises(DeviceRuntimeProtocolError):
            await host.flush_events()
        [pending] = host.event_spool.pending()
        self.assertEqual(event, pending)
        self.assertEqual(0, host.event_spool.dead_letter_count)
        self.assertIn("failed-http", host.sessions)
        self.assertFalse(adapter.closed)
        await host.close()

    async def test_event_batches_respect_server_body_limit(self) -> None:
        client = FakeDeviceClient()
        host = self.host(client)
        for index in range(7):
            await host._emit_adapter_event(
                RuntimeEvent(
                    type="item.completed",
                    session_id="large-session",
                    payload={"index": index, "body": "x" * 60_000},
                ),
                default_session_id="large-session",
            )
        while len(host.event_spool):
            await host.flush_events()
        self.assertGreater(len(client.event_batches), 1)
        for batch, identity in zip(
            client.event_batches,
            client.event_identities,
            strict=True,
        ):
            encoded = json.dumps(
                {**identity, "events": batch},
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertLessEqual(len(encoded), 256 * 1024)
        await host.close()

    async def test_restart_quarantines_events_from_the_stale_generation(self) -> None:
        first_client = FakeDeviceClient()
        first = self.host(first_client, state_name="stale-events")
        old_event = await first._emit_adapter_event(
            RuntimeEvent(
                type="turn.completed",
                session_id="old-session",
                payload={"turn_id": "old-turn"},
            ),
            default_session_id="old-session",
        )
        self.assertEqual(1, len(first.event_spool))
        first_client.fail_event_responses = 1
        await first.close()

        replacement_client = FakeDeviceClient()
        replacement = self.host(
            replacement_client,
            state_name="stale-events",
        )
        self.assertEqual(1, replacement.stale_generation_events)
        self.assertEqual(0, len(replacement.event_spool))
        self.assertEqual(1, replacement.event_spool.dead_letter_count)
        [dead_letter] = replacement.event_spool.dead_letters()
        self.assertEqual("stale_generation", dead_letter["reason"])
        self.assertEqual(old_event, dead_letter["envelope"])
        self.assertEqual(
            first.runtime_session_id,
            dead_letter["envelope"]["runtime_session_id"],
        )
        self.assertEqual(first.generation, dead_letter["envelope"]["generation"])
        await replacement._emit_adapter_event(
            RuntimeEvent(
                type="session.started",
                session_id="new-session",
                payload={},
            ),
            default_session_id="new-session",
        )
        await replacement.flush_events()
        self.assertEqual(0, len(replacement.event_spool))
        self.assertEqual(1, replacement.event_spool.dead_letter_count)
        self.assertEqual(1, len(replacement_client.event_batches))
        await replacement.close()

    async def test_legacy_spool_migrates_and_dead_letters_trim_oldest(self) -> None:
        database = self.root / "legacy-spool.db"
        legacy_envelope = {
            "schema": "agentserver.device-runtime-event/1",
            "event_id": "legacy-event",
            "type": "turn.completed",
            "device_id": "device-1",
            "instance_id": "instance-legacy",
            "boot_id": "boot-legacy",
            "runtime_session_id": "runtime-legacy",
            "generation": 4,
            "session_id": "session-legacy",
            "producer": {"epoch": "legacy-epoch", "seq": 40},
            "occurred_at": 100.0,
            "payload": {},
        }
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE device_event_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE device_runtime_events (
                    producer_seq INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    envelope_json TEXT NOT NULL,
                    attempted INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE device_runtime_event_dead_letters (
                    dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    producer_seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at REAL NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO device_event_metadata(key, value) VALUES ('epoch', ?)",
                ("legacy-epoch",),
            )
            connection.execute(
                """
                INSERT INTO device_runtime_events(
                    producer_seq, event_id, envelope_json, attempted, created_at
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (40, "legacy-event", json.dumps(legacy_envelope), 100.0),
            )

        spool = DeviceEventSpool(database, max_dead_letters=2)
        self.assertEqual(1, len(spool))
        self.assertEqual(0, spool.dead_letter_count)
        self.assertEqual(1, spool.quarantine_stale_generation(now=200.0))
        self.assertEqual(legacy_envelope, spool.dead_letters()[0]["envelope"])
        self.assertEqual("", spool.dead_letters()[0]["error_code"])

        current: list[dict[str, Any]] = []
        for index in range(3):
            current.append(
                spool.enqueue(
                    RuntimeEvent(
                        type="item.completed",
                        session_id="current-session",
                        payload={"index": index},
                    ),
                    device_id="device-1",
                    instance_id="instance-current",
                    boot_id="boot-current",
                    runtime_session_id="runtime-current",
                    generation=5,
                )
            )
        self.assertEqual(3, spool.quarantine_stale_generation(now=300.0))
        self.assertEqual(2, spool.dead_letter_count)
        retained = spool.dead_letters(limit=2)
        self.assertEqual(
            [current[2]["event_id"], current[1]["event_id"]],
            [item["event_id"] for item in retained],
        )
        self.assertTrue(all(item["reason"] == "stale_generation" for item in retained))
        self.assertEqual([300.0, 300.0], [item["quarantined_at"] for item in retained])

        reopened = DeviceEventSpool(database, max_dead_letters=2)
        self.assertEqual(2, reopened.dead_letter_count)
        self.assertEqual(
            [item["event_id"] for item in retained],
            [item["event_id"] for item in reopened.dead_letters(limit=2)],
        )

    async def test_run_loop_obeys_external_stop_event(self) -> None:
        client = FakeDeviceClient()
        host = self.host(client)
        stopped = asyncio.Event()
        task = asyncio.create_task(host.run(stop_event=stopped))
        deadline = asyncio.get_running_loop().time() + 2
        while not client.heartbeats:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("runtime loop did not heartbeat")
            await asyncio.sleep(0.01)
        stopped.set()
        await asyncio.wait_for(task, timeout=2)
        await host.close()

    async def test_every_control_plane_request_carries_the_runtime_fence(self) -> None:
        client = FakeDeviceClient()
        factory = FakeTypedAdapterFactory()
        host = self.host(client, registry={"typed": factory})

        await host.heartbeat()
        self.assert_runtime_identity(host, client.heartbeats[-1])
        self.assertEqual(
            {"providers", "features"},
            set(client.heartbeats[-1]["capabilities"]),
        )
        [provider] = client.heartbeats[-1]["capabilities"]["providers"]
        self.assertEqual("typed", provider["id"])
        self.assertEqual("typed-test", provider["transport"])
        self.assertTrue(provider["available"])
        self.assertEqual({"os", "arch", "hostname"}, set(client.heartbeats[-1]["platform"]))

        await host.poll_commands()
        self.assert_runtime_identity(host, client.command_requests[-1])

        envelope = await host._emit_adapter_event(
            RuntimeEvent("runtime.test", {}, session_id="session-a"),
            default_session_id="session-a",
        )
        self.assert_runtime_identity(host, envelope)
        await host.flush_events()
        self.assert_runtime_identity(host, client.event_identities[-1])

        await host.rotate_credential()
        self.assert_runtime_identity(host, client.rotation_requests[-1])
        await host.close()

    async def test_host_wire_contract_runs_against_device_runtime_api(self) -> None:
        database = self.root / "server.db"
        execution_store = ExecutionStore(database)
        runtime_store = DeviceRuntimeStore(database)
        service = DeviceRuntimeService(
            runtime_store,
            execution_store,
            device_exists=lambda owner_id, device_id: (
                owner_id,
                device_id,
            )
            == ("alice", "device-1"),
        )
        application = FastAPI()
        application.include_router(
            build_device_runtime_router(lambda: "alice")
        )
        application.state.device_runtime = service

        enrollment = service.issue_enrollment(
            owner_id="alice", device_id="device-1"
        )
        token_path = self.root / "wire-enrollment.token"
        token_path.write_text(enrollment.token + "\n", encoding="utf-8")
        token_path.chmod(0o600)
        factory = FakeTypedAdapterFactory()
        host = DeviceRuntimeHost(
            device_id="device-1",
            base_url="https://agentserver.test",
            state_dir=self.root / "wire-state",
            adapter_registry={"typed": factory},
            http_transport=httpx.ASGITransport(app=application),
        )
        await host.enroll_from_file(token_path)
        await host.heartbeat()

        service.create_session(
            owner_id="alice",
            device_id="device-1",
            provider="typed",
            workspace=str(self.root),
            options={"permission_mode": "workspace-write"},
            session_id="wire-session",
        )
        await host.poll_commands()
        await host.flush_events()
        self.assertEqual(
            "ready",
            service.get_session(
                owner_id="alice", session_id="wire-session"
            ).lifecycle,
        )

        service.send_turn(
            owner_id="alice",
            session_id="wire-session",
            input="wire turn",
            turn_id="wire-turn",
        )
        await host.poll_commands()
        await host.flush_events()
        self.assertEqual(
            "running",
            service.get_session(
                owner_id="alice", session_id="wire-session"
            ).lifecycle,
        )

        original_credential = host.credential_file.load()
        await host.close()
        previous_generation = host.generation
        replacement = DeviceRuntimeHost(
            device_id="device-1",
            base_url="https://agentserver.test",
            state_dir=self.root / "wire-state",
            adapter_registry={"typed": FakeTypedAdapterFactory()},
            http_transport=httpx.ASGITransport(app=application),
        )
        await replacement.rotate_credential()
        self.assertEqual(previous_generation + 1, replacement.generation)
        self.assertNotEqual(
            original_credential, replacement.credential_file.load()
        )
        await replacement.heartbeat()
        await replacement.close()

    async def test_credential_rotation_is_persisted_before_return(self) -> None:
        client = FakeDeviceClient()
        host = self.host(client)
        result = await host.rotate_credential()
        self.assertTrue(result["rotated"])
        self.assertEqual(
            "asdc1.credential-2.rotated-secret-1",
            host.credential_file.load(),
        )
        self.assert_runtime_identity(host, client.rotation_requests[-1])
        await host.close()

    async def test_run_once_automatically_rotates_before_credential_expiry(self) -> None:
        class ExpiringCredentialClient(FakeDeviceClient):
            async def heartbeat(
                self, payload: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                self.heartbeats.append(dict(payload))
                server_time = time.time()
                return {
                    "server_time": server_time,
                    "credential_expires_at": server_time + 120,
                }

        client = ExpiringCredentialClient()
        host = self.host(client, credential_rotation_window=300)
        result = await host.run_once(force_heartbeat=True)
        self.assertIn("credential_rotation", result)
        self.assertEqual(1, client.rotations)
        self.assertEqual(
            "asdc1.credential-2.rotated-secret-1",
            host.credential_file.load(),
        )
        # The heartbeat already claimed this generation; automatic rotation
        # does not need a redundant second heartbeat.
        self.assertEqual(1, len(client.heartbeats))
        await host.close()

    async def test_lost_rotation_response_reuses_persisted_request_and_fence(self) -> None:
        class IdempotentLostResponseClient(FakeDeviceClient):
            def __init__(self) -> None:
                super().__init__()
                self.response_lost = False

            async def rotate_credential(
                self,
                payload: Mapping[str, Any],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                self.rotation_requests.append(
                    {
                        **dict(payload),
                        "device_id": device_id,
                        "instance_id": instance_id,
                        "boot_id": boot_id,
                        "runtime_session_id": runtime_session_id,
                        "generation": generation,
                    }
                )
                if not self.response_lost:
                    self.response_lost = True
                    raise OSError("rotation response was lost after commit")
                return {
                    "credential": "asdc1.credential-2.idempotent-rotated-secret"
                }

        client = IdempotentLostResponseClient()
        first = self.host(client, state_name="rotation-retry")
        with self.assertRaises(OSError):
            await first.rotate_credential()
        [original_request] = client.rotation_requests
        self.assertTrue(first.rotation_request_path.exists())
        self.assertEqual(_DEVICE_CREDENTIAL, first.credential_file.load())
        await first.close()

        replacement = self.host(client, state_name="rotation-retry")
        result = await replacement.run_once(force_heartbeat=True)
        self.assertIn("credential_rotation", result)
        self.assertEqual(2, len(client.rotation_requests))
        retried = client.rotation_requests[-1]
        self.assertEqual(original_request["request_id"], retried["request_id"])
        self.assertEqual(
            original_request["runtime_session_id"],
            retried["runtime_session_id"],
        )
        self.assertEqual(original_request["generation"], retried["generation"])
        self.assertNotEqual(replacement.generation, retried["generation"])
        # First heartbeat claimed the original fence; run_once retries the
        # persisted rotation before its own heartbeat on the replacement boot.
        self.assertEqual(2, len(client.heartbeats))
        self.assertEqual(
            "asdc1.credential-2.idempotent-rotated-secret",
            replacement.credential_file.load(),
        )
        self.assertFalse(replacement.rotation_request_path.exists())
        await replacement.close()

    async def test_pending_rotation_failure_cannot_replace_the_recovery_fence(self) -> None:
        class UncommittedRotationClient(FakeDeviceClient):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            async def rotate_credential(
                self,
                payload: Mapping[str, Any],
                *,
                device_id: str,
                instance_id: str,
                boot_id: str,
                runtime_session_id: str,
                generation: int,
            ) -> Mapping[str, Any]:
                self.attempts += 1
                self.rotation_requests.append(
                    {
                        **dict(payload),
                        "device_id": device_id,
                        "instance_id": instance_id,
                        "boot_id": boot_id,
                        "runtime_session_id": runtime_session_id,
                        "generation": generation,
                    }
                )
                if self.attempts <= 2:
                    raise OSError("rotation request failed before commit")
                return {"credential": "asdc1.credential-2.recovered-secret"}

        client = UncommittedRotationClient()
        first = self.host(client, state_name="rotation-fence-recovery")
        with self.assertRaises(OSError):
            await first.rotate_credential()
        original_request = dict(client.rotation_requests[0])
        self.assertEqual(1, len(client.heartbeats))
        await first.close()

        replacement = self.host(client, state_name="rotation-fence-recovery")
        with self.assertRaises(DeviceRuntimeCycleError) as failure:
            await replacement.run_once(force_heartbeat=True)
        self.assertEqual(("credential_rotation",), failure.exception.components)
        self.assertEqual(1, len(client.heartbeats))
        self.assertEqual([], client.command_requests)
        self.assertEqual([], client.event_batches)
        self.assertTrue(replacement.rotation_request_path.exists())

        recovered = await replacement.run_once(force_heartbeat=True)
        self.assertIn("credential_rotation", recovered)
        self.assertEqual(2, len(client.heartbeats))
        self.assertFalse(replacement.rotation_request_path.exists())
        self.assertEqual(
            "asdc1.credential-2.recovered-secret",
            replacement.credential_file.load(),
        )
        self.assertEqual(3, len(client.rotation_requests))
        for retried in client.rotation_requests[1:]:
            self.assertEqual(original_request["request_id"], retried["request_id"])
            self.assertEqual(
                original_request["runtime_session_id"],
                retried["runtime_session_id"],
            )
            self.assertEqual(original_request["generation"], retried["generation"])
        await replacement.close()

    async def test_rotation_marker_after_local_commit_does_not_rotate_twice(self) -> None:
        client = FakeDeviceClient()
        first = self.host(client, state_name="rotation-local-commit")
        with mock.patch(
            "app.execution.runtime_host._remove_private_file",
            side_effect=OSError("crash before marker removal"),
        ):
            with self.assertRaises(OSError):
                await first.rotate_credential()
        installed = first.credential_file.load()
        self.assertEqual("asdc1.credential-2.rotated-secret-1", installed)
        self.assertTrue(first.rotation_request_path.exists())
        self.assertEqual(1, len(client.rotation_requests))
        await first.close()

        replacement = self.host(client, state_name="rotation-local-commit")
        result = await replacement.rotate_credential()
        self.assertTrue(result["recovered_after_local_commit"])
        self.assertEqual(installed, replacement.credential_file.load())
        self.assertEqual(1, len(client.rotation_requests))
        self.assertFalse(replacement.rotation_request_path.exists())
        await replacement.close()


class DeviceRuntimeHTTPClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_default_registry_exposes_codex_without_spawning_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "runtime-state"
            codex_home = root / "codex-home"
            state_dir.mkdir()
            codex_home.mkdir()
            registry = _default_adapter_registry(
                sys.executable,
                state_dir=state_dir,
                codex_home=codex_home,
                bubblewrap_binary="/bin/true",
            )
            self.assertEqual({"codex"}, set(registry))
            factory = registry["codex"]
            self.assertEqual("app-server", getattr(factory, "transport"))
            self.assertTrue(getattr(factory, "available"))
            self.assertTrue(getattr(factory, "capabilities").interrupt)
            adapter = factory()
            self.assertIsInstance(adapter, TypedRuntimeAdapter)
            self.assertTrue(adapter.isolation_enabled)
            self.assertEqual(str(state_dir.resolve()), adapter.host_state_dir)
            await adapter.close()

    async def test_cli_codex_binary_is_resolved_and_passed_without_a_shell(self) -> None:
        binary = Path(tempfile.mkdtemp()) / "codex custom"
        try:
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o700)
            arguments = build_runtime_parser().parse_args(
                [
                    "--device-id",
                    "device-1",
                    "--base-url",
                    "https://agentserver.example",
                    "--codex-binary",
                    str(binary),
                    "run",
                ]
            )
            codex_home = binary.parent / "codex-home"
            state_dir = binary.parent / "runtime-state"
            codex_home.mkdir()
            state_dir.mkdir()
            registry = _default_adapter_registry(
                arguments.codex_binary,
                state_dir=state_dir,
                codex_home=codex_home,
                bubblewrap_binary="/bin/true",
            )
            factory = registry["codex"]
            self.assertTrue(getattr(factory, "available"))
            adapter = factory()
            self.assertEqual(str(binary.resolve()), adapter._command[0])
            self.assertEqual(str(Path("/bin/true").resolve()), adapter.bubblewrap_path)
            await adapter.close()
            with self.assertRaisesRegex(ValueError, "safe executable path"):
                _codex_binary("codex\nother")
        finally:
            binary.unlink(missing_ok=True)
            (binary.parent / "codex-home").rmdir()
            (binary.parent / "runtime-state").rmdir()
            binary.parent.rmdir()

    async def test_cli_default_registry_marks_failed_bubblewrap_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "runtime-state"
            codex_home = root / "codex-home"
            state_dir.mkdir()
            codex_home.mkdir()
            registry = _default_adapter_registry(
                sys.executable,
                state_dir=state_dir,
                codex_home=codex_home,
                bubblewrap_binary="/bin/false",
            )
            factory = registry["codex"]
            self.assertFalse(getattr(factory, "available"))
            adapter = factory()
            self.assertFalse((await adapter.probe()).available)
            await adapter.close()

    async def test_paths_are_namespaced_and_only_enroll_omits_authorization(self) -> None:
        requests: list[httpx.Request] = []

        def server(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/enroll"):
                return httpx.Response(200, request=request, json={"credential": "new"})
            if request.url.path.endswith("/events:batch"):
                return httpx.Response(
                    200,
                    request=request,
                    json={"accepted_through_seq": 1, "missing_ranges": []},
                )
            if request.url.path.endswith("/commands"):
                return httpx.Response(
                    200,
                    request=request,
                    json={"commands": [], "server_time": time.time()},
                )
            return httpx.Response(200, request=request, json={"server_time": time.time()})

        client = DeviceRuntimeHTTPClient(
            "https://agentserver.example",
            lambda: "stored-device-secret",
            transport=httpx.MockTransport(server),
        )
        identity = {
            "device_id": "device-1",
            "instance_id": "instance-1",
            "boot_id": "boot-1",
            "runtime_session_id": "boot-1",
            "generation": 7,
        }
        await client.enroll({"enrollment_token": "one-time"})
        await client.heartbeat(identity)
        await client.commands(after_sequence=0, **identity)
        await client.acknowledge_command(
            "command-1",
            {"status": "completed", "ack_id": "ack-1"},
            **identity,
        )
        await client.send_events(
            [{"producer": {"seq": 1}, "event_id": "event-1"}],
            **identity,
        )
        await client.rotate_credential({}, **identity)
        await client.close()

        expected = [
            "/api/device-runtime/v1/enroll",
            "/api/device-runtime/v1/heartbeat",
            "/api/device-runtime/v1/commands",
            "/api/device-runtime/v1/commands/command-1/ack",
            "/api/device-runtime/v1/events:batch",
            "/api/device-runtime/v1/credential:rotate",
        ]
        self.assertEqual(expected, [request.url.path for request in requests])
        self.assertNotIn("authorization", requests[0].headers)
        for request in requests[1:]:
            self.assertEqual(
                "Bearer stored-device-secret", request.headers["authorization"]
            )
        query = dict(requests[2].url.params)
        self.assertEqual("0", query["after_sequence"])
        for key, value in identity.items():
            self.assertEqual(str(value), query[key])
        for request in (requests[1], requests[3], requests[4], requests[5]):
            body = json.loads(request.content)
            for key, value in identity.items():
                self.assertEqual(value, body[key])


@unittest.skipIf(os.name == "nt", "POSIX permission contract")
class DeviceRuntimePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_and_secret_files_are_owner_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeDeviceClient()
            host = DeviceRuntimeHost(
                device_id="device-1",
                base_url="https://agentserver.example",
                state_dir=root / "state",
                client=client,
            )
            host.credential_file.replace(_DEVICE_CREDENTIAL)
            self.assertEqual(0o700, host.state_dir.stat().st_mode & 0o777)
            for path in (
                host.instance_path,
                host.generation_path,
                host.credential_file.path,
                host.database_path,
                host._instance_lock.path,
            ):
                self.assertEqual(0o600, path.stat().st_mode & 0o777)

            loose = root / "loose.token"
            loose.write_text("secret\n", encoding="utf-8")
            loose.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                load_private_text_file(loose)
            await host.close()

    async def test_base_url_rejects_remote_plain_http_and_embedded_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                DeviceRuntimeHost(
                    device_id="device-1",
                    base_url="http://agentserver.example",
                    state_dir=Path(directory) / "one",
                    client=FakeDeviceClient(),
                )
            with self.assertRaises(ValueError):
                DeviceRuntimeHost(
                    device_id="device-1",
                    base_url="https://user:secret@agentserver.example",
                    state_dir=Path(directory) / "two",
                    client=FakeDeviceClient(),
                )


if __name__ == "__main__":
    unittest.main()
