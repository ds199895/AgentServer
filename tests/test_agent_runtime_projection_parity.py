from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.agent_runtime.events import AgentEvent
from app.agent_runtime.service import AgentSessionService
from app.agent_runtime.store import AgentEventStore

FIXTURE = Path(__file__).parent / "fixtures" / "agent_projection_parity.json"

# One representative turn covering every event type the timeline renders. The
# client reducer (`frontend/src/agent/reducer.ts`) is checked against the same
# fixture by `frontend/test/agent-projection-parity.test.mjs`, so the two
# projections cannot drift apart without a test failing on one side or both.
EVENTS: list[tuple[str, dict]] = [
    ("session.ready", {"provider_thread_id": "thread-9"}),
    ("turn.queued", {"turn_id": "turn-1", "input": "list the files"}),
    ("turn.started", {"turn_id": "turn-1"}),
    ("activity.started", {"activity_id": "item-1", "kind": "command", "title": "Command", "detail": "ls -la", "turn_id": "turn-1", "item_id": "item-1"}),
    ("activity.updated", {"activity_id": "item-1", "output_delta": "a.txt\n"}),
    ("activity.updated", {"activity_id": "item-1", "output_delta": "b.txt\n"}),
    ("activity.completed", {"activity_id": "item-1", "status": "succeeded", "item_id": "item-1"}),
    ("message.delta", {"message_id": "reasoning-1", "role": "reasoning", "text": "checking the tree", "item_id": "item-1", "turn_id": "turn-1"}),
    ("message.delta", {"message_id": "msg-1", "role": "assistant", "text": "Two ", "turn_id": "turn-1"}),
    ("message.delta", {"message_id": "msg-1", "role": "assistant", "text": "files.", "turn_id": "turn-1"}),
    ("message.created", {"message_id": "msg-1", "role": "assistant", "text": "Two files.", "turn_id": "turn-1"}),
    ("plan.updated", {"activity_id": "plan-1", "plan": [{"step": "look", "status": "in_progress"}], "turn_id": "turn-1"}),
    # A turn re-plans as it works; the second revision must replace the first
    # rather than appending a second "Plan updated" card.
    ("plan.updated", {"activity_id": "plan-1", "plan": [{"step": "look", "status": "completed"}, {"step": "report", "status": "pending"}], "turn_id": "turn-1"}),
    ("request.created", {"request_id": "req-1", "kind": "approval", "title": "Run command?", "options": [], "turn_id": "turn-1"}),
    ("request.resolved", {"request_id": "req-1", "resolution": {"decision": "approve_once"}}),
    ("turn.completed", {"turn_id": "turn-1"}),
]


async def build_projection() -> dict:
    """Drive the events through the real service and return the fixture body."""
    with tempfile.TemporaryDirectory() as directory:
        service = AgentSessionService(AgentEventStore(Path(directory) / "agent.db"))
        try:
            session = await service.create(
                owner_id="alice",
                provider="generic",
                device_id="device-a",
                cwd="/workspace",
                session_id="session-fixture",
            )
            committed = []
            for index, (typ, payload) in enumerate(EVENTS, start=1):
                value = await service._dispatch(
                    session,
                    AgentEvent(0, f"event-{index}", session.id, typ, payload, 1000.0 + index),
                )
                committed.append(value.as_dict())
            current = await service.get("alice", session.id)
            assert current is not None
            snapshot = current.as_dict()
            # `created_at` is wall-clock; pin it so regeneration is deterministic.
            snapshot["created_at"] = 1000.0
            return {"events": committed, "expected": snapshot}
        finally:
            await service.close()


class AgentProjectionParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_matches_the_current_server_projection(self) -> None:
        expected = json.loads(FIXTURE.read_text())
        actual = await build_projection()
        self.assertEqual(
            actual,
            expected,
            "The server projection changed. Regenerate tests/fixtures/"
            "agent_projection_parity.json and update frontend/src/agent/reducer.ts "
            "so the client stays in step.",
        )


if __name__ == "__main__":
    print(json.dumps(asyncio.run(build_projection()), indent=2, sort_keys=True))
