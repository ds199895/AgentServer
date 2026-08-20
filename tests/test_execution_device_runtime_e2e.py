from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.execution.device_runtime import DeviceRuntimeService, DeviceRuntimeStore
from app.execution.device_runtime_api import build_device_runtime_router
from app.execution.runtime_adapters.base import (
    ApprovalDecision,
    RuntimeAdapter,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeProbe,
    RuntimeSession,
    RuntimeSessionSpec,
    RuntimeThreadSnapshot,
    RuntimeTurn,
    RuntimeTurnInput,
)
from app.execution.runtime_host import DeviceRuntimeHost
from app.execution.store import ExecutionStore


_END = object()


class ScriptedRuntimeAdapter(RuntimeAdapter):
    provider = "fixture"
    capabilities = RuntimeCapabilities(
        interrupt=True,
        approvals=True,
        user_input=True,
    )

    def __init__(self) -> None:
        self.session_id = ""
        self.queue: asyncio.Queue[RuntimeEvent | object] = asyncio.Queue()
        self.turns: list[str] = []
        self.decisions: list[tuple[str, str]] = []
        self.closed = False

    async def probe(self) -> RuntimeProbe:
        return RuntimeProbe(available=True, version="fixture/1")

    async def start_session(self, spec: RuntimeSessionSpec) -> RuntimeSession:
        self.session_id = spec.session_id
        await self.emit(
            "session.started",
            {"provider_session_id": f"provider-{spec.session_id}"},
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
        turn_id = f"turn-{len(self.turns) + 1}"
        self.turns.append(str(turn.text or ""))
        await self.emit("turn.started", {}, turn_id=turn_id)
        await self.emit(
            "interaction.opened",
            {"interaction_id": "approval-1", "kind": "command_execution_approval"},
            turn_id=turn_id,
            interaction_id="approval-1",
        )
        return RuntimeTurn(session_id=session_id, turn_id=turn_id)

    async def interrupt_turn(
        self, session_id: str, turn_id: str | None = None
    ) -> None:
        await self.emit("turn.completed", {"status": "interrupted"}, turn_id=turn_id)

    async def respond_to_approval(
        self,
        session_id: str,
        interaction_id: str,
        decision: ApprovalDecision | str,
    ) -> None:
        resolved = decision.value if isinstance(decision, ApprovalDecision) else str(decision)
        self.decisions.append((interaction_id, resolved))
        await self.emit(
            "interaction.resolved",
            {"interaction_id": interaction_id, "outcome": resolved},
            interaction_id=interaction_id,
        )
        await self.emit("turn.completed", {"status": "completed"}, turn_id="turn-1")

    async def respond_to_user_input(
        self,
        session_id: str,
        interaction_id: str,
        answers: Mapping[str, str | Sequence[str]],
    ) -> None:
        await self.emit(
            "interaction.resolved",
            {"interaction_id": interaction_id, "outcome": "answered"},
            interaction_id=interaction_id,
        )

    async def read_thread(self, session_id: str) -> RuntimeThreadSnapshot:
        return RuntimeThreadSnapshot(thread_id=f"provider-{session_id}")

    async def rollback_thread(
        self, session_id: str, num_turns: int
    ) -> RuntimeThreadSnapshot:
        return await self.read_thread(session_id)

    async def stop_session(self, session_id: str) -> None:
        await self.emit("session.stopped", {})

    async def list_sessions(self) -> tuple[RuntimeSession, ...]:
        return ()

    async def events(
        self, session_id: str | None = None
    ) -> AsyncIterator[RuntimeEvent]:
        while True:
            value = await self.queue.get()
            if value is _END:
                return
            assert isinstance(value, RuntimeEvent)
            yield value

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.queue.put(_END)

    async def emit(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        turn_id: str | None = None,
        interaction_id: str | None = None,
    ) -> None:
        await self.queue.put(
            RuntimeEvent(
                event_id=uuid.uuid4().hex,
                provider=self.provider,
                session_id=self.session_id,
                type=event_type,
                payload=dict(payload),
                turn_id=turn_id,
                interaction_id=interaction_id,
                occurred_at=time.time(),
            )
        )


class DeviceRuntimeEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        database = root / "server.db"
        execution = ExecutionStore(database)
        self.service = DeviceRuntimeService(
            DeviceRuntimeStore(database),
            execution,
            device_exists=lambda owner, device: owner == "alice" and device == "device-1",
            offline_after=30,
        )
        app = FastAPI()

        def browser_user() -> str:
            return "alice"

        app.include_router(build_device_runtime_router(browser_user))
        app.state.device_runtime = self.service
        self.transport = httpx.ASGITransport(app=app)
        enrollment = self.service.issue_enrollment(
            owner_id="alice", device_id="device-1"
        )
        self.token_path = root / "enrollment-token"
        self.token_path.write_text(enrollment.token + "\n", encoding="utf-8")
        os.chmod(self.token_path, 0o600)
        self.adapters: list[ScriptedRuntimeAdapter] = []

        def factory() -> ScriptedRuntimeAdapter:
            adapter = ScriptedRuntimeAdapter()
            self.adapters.append(adapter)
            return adapter

        factory.capabilities = ScriptedRuntimeAdapter.capabilities  # type: ignore[attr-defined]
        factory.transport = "fixture"  # type: ignore[attr-defined]
        self.host = DeviceRuntimeHost(
            device_id="device-1",
            base_url="http://localhost",
            state_dir=root / "host",
            adapter_registry={"fixture": factory},
            http_transport=self.transport,
        )
        await self.host.enroll_from_file(self.token_path)
        await self.host.heartbeat()

    async def asyncTearDown(self) -> None:
        await self.host.close()
        await self.transport.aclose()
        self.directory.cleanup()

    async def drain_adapter_events(self) -> None:
        for _ in range(100):
            if len(self.host.event_spool):
                break
            await asyncio.sleep(0)
        self.assertGreater(len(self.host.event_spool), 0)
        await self.host.flush_events()

    async def test_server_to_host_to_typed_adapter_and_events_round_trip(self) -> None:
        session = self.service.create_session(
            owner_id="alice",
            device_id="device-1",
            provider="fixture",
            workspace=str(Path(self.directory.name)),
            options={"permission_mode": "workspace-write"},
        )
        await self.host.poll_commands()
        self.assertEqual(1, len(self.adapters))
        await self.drain_adapter_events()
        ready = self.service.get_session(owner_id="alice", session_id=session.session_id)
        self.assertEqual("ready", ready.lifecycle)
        self.assertEqual(f"provider-{session.session_id}", ready.provider_session_id)

        self.service.send_turn(
            owner_id="alice",
            session_id=session.session_id,
            input="make a deterministic change",
        )
        await self.host.poll_commands()
        await self.drain_adapter_events()
        waiting = self.service.get_session(owner_id="alice", session_id=session.session_id)
        self.assertEqual("waiting", waiting.lifecycle)
        self.assertEqual("approval-1", waiting.active_request_id)
        self.assertEqual(["make a deterministic change"], self.adapters[0].turns)

        self.service.respond_to_request(
            owner_id="alice",
            session_id=session.session_id,
            request_id="approval-1",
            response={"decision": "approve_once"},
        )
        await self.host.poll_commands()
        await self.drain_adapter_events()
        completed = self.service.get_session(owner_id="alice", session_id=session.session_id)
        self.assertEqual("ready", completed.lifecycle)
        self.assertEqual(
            [("approval-1", "approve_once")], self.adapters[0].decisions
        )
        events = self.service.session_events(
            owner_id="alice", session_id=session.session_id
        )
        self.assertEqual(
            [
                "session.started",
                "turn.started",
                "interaction.opened",
                "interaction.resolved",
                "turn.completed",
            ],
            [event.type for event in events],
        )
        self.assertEqual(0, len(self.host.event_spool))
        self.assertFalse(self.host.command_journal.pending_acks())

    async def test_rejected_start_and_missing_handle_stop_reach_terminal_states(
        self,
    ) -> None:
        rejected = self.service.create_session(
            owner_id="alice",
            device_id="device-1",
            provider="not-installed",
            workspace=str(Path(self.directory.name)),
        )
        await self.host.poll_commands()
        failed = self.service.get_session(
            owner_id="alice", session_id=rejected.session_id
        )
        self.assertEqual("failed", failed.lifecycle)
        self.assertIn("not installed", failed.last_error)

        session = self.service.create_session(
            owner_id="alice",
            device_id="device-1",
            provider="fixture",
            workspace=str(Path(self.directory.name)),
        )
        await self.host.poll_commands()
        await self.drain_adapter_events()
        self.assertEqual(
            "ready",
            self.service.get_session(
                owner_id="alice", session_id=session.session_id
            ).lifecycle,
        )

        handle = self.host._sessions.pop(session.session_id)
        await self.host._close_session_handle(handle)
        self.service.stop_session(owner_id="alice", session_id=session.session_id)
        await self.host.poll_commands()
        self.assertEqual(
            "stopping",
            self.service.get_session(
                owner_id="alice", session_id=session.session_id
            ).lifecycle,
        )
        await self.drain_adapter_events()
        self.assertEqual(
            "stopped",
            self.service.get_session(
                owner_id="alice", session_id=session.session_id
            ).lifecycle,
        )


if __name__ == "__main__":
    unittest.main()
