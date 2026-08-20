from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class AgentEvent:
    sequence: int
    id: str
    session_id: str
    type: str
    payload: dict[str, Any]
    occurred_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_id": self.id,
            "session_id": self.session_id,
            "type": self.type,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
        }


def event(session_id: str, type: str, payload: dict[str, Any] | None = None) -> AgentEvent:
    return AgentEvent(0, new_id(), session_id, type, dict(payload or {}), time.time())
