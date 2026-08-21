from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SessionState = Literal[
    "starting", "ready", "running", "waiting", "disconnected", "stopping", "stopped", "failed"
]
TurnState = Literal["queued", "running", "completed", "failed", "interrupted"]
MessageRole = Literal["user", "assistant", "system", "reasoning"]
ActivityKind = Literal["plan", "tool", "file", "command", "output", "status"]
RequestKind = Literal["approval", "user_input"]


def _json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


@dataclass
class AgentMessage:
    id: str
    session_id: str
    role: MessageRole
    text: str
    turn_id: str | None = None
    item_id: str | None = None
    created_at: float = 0.0
    streaming: bool = False
    sequence: int = 0

    def as_dict(self) -> dict[str, Any]:
        return _json(self.__dict__)


@dataclass
class AgentActivity:
    id: str
    session_id: str
    kind: ActivityKind
    title: str
    status: str = "running"
    detail: str = ""
    input: Any = None
    output: Any = None
    turn_id: str | None = None
    item_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    collapsed: bool = True
    sequence: int = 0

    def as_dict(self) -> dict[str, Any]:
        return _json(self.__dict__)


@dataclass
class AgentRequest:
    id: str
    session_id: str
    kind: RequestKind
    title: str
    detail: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"
    turn_id: str | None = None
    created_at: float = 0.0
    input: Any = None
    response: Any = None
    resolved_at: float | None = None
    sequence: int = 0

    def as_dict(self) -> dict[str, Any]:
        return _json(self.__dict__)


@dataclass
class AgentTurn:
    id: str
    session_id: str
    input: str
    state: TurnState = "queued"
    created_at: float = 0.0
    completed_at: float | None = None
    error: str | None = None
    # Model this turn actually ran with, when it differs from the session
    # default; lets the transcript show what produced each answer.
    model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return _json(self.__dict__)


@dataclass
class AgentSession:
    id: str
    owner_id: str
    device_id: str | None
    provider: str
    cwd: str
    permission_mode: str = "workspace-write"
    model: str | None = None
    state: SessionState = "starting"
    created_at: float = 0.0
    updated_at: float = 0.0
    active_turn_id: str | None = None
    last_error: str | None = None
    resume_cursor: dict[str, Any] | None = None
    sequence: int = 0
    executor_id: str = ""
    bridge_instance_id: str = ""
    transport: str = ""
    device_generation: int = 0
    platform: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    connector_sequence: int = 0
    messages: list[AgentMessage] = field(default_factory=list)
    activities: list[AgentActivity] = field(default_factory=list)
    requests: list[AgentRequest] = field(default_factory=list)
    turns: list[AgentTurn] = field(default_factory=list)

    @property
    def session_kind(self) -> str:
        return "agent"

    def as_dict(self, *, include_history: bool = True) -> dict[str, Any]:
        result = {
            "id": self.id,
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "provider": self.provider,
            "cwd": self.cwd,
            "permission_mode": self.permission_mode,
            "model": self.model,
            "state": self.state,
            "session_kind": self.session_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_turn_id": self.active_turn_id,
            "last_error": self.last_error,
            "resume_cursor": self.resume_cursor,
            "sequence": self.sequence,
            "executor_id": self.executor_id,
            "bridge_instance_id": self.bridge_instance_id,
            "transport": self.transport,
            "device_generation": self.device_generation,
            "platform": self.platform,
            "capabilities": self.capabilities,
            "connector_sequence": self.connector_sequence,
        }
        if include_history:
            result.update(
                messages=[item.as_dict() for item in self.messages],
                activities=[item.as_dict() for item in self.activities],
                requests=[item.as_dict() for item in self.requests],
                turns=[item.as_dict() for item in self.turns],
            )
        return _json(result)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentSession":
        session = cls(
            id=str(value["id"]), owner_id=str(value.get("owner_id") or ""),
            device_id=value.get("device_id"), provider=str(value.get("provider") or "generic"),
            cwd=str(value.get("cwd") or "."), permission_mode=str(value.get("permission_mode") or "workspace-write"),
            model=value.get("model"), state=value.get("state") or "starting",
            created_at=float(value.get("created_at") or 0), updated_at=float(value.get("updated_at") or 0),
            active_turn_id=value.get("active_turn_id"), last_error=value.get("last_error"), resume_cursor=value.get("resume_cursor"), sequence=int(value.get("sequence") or 0),
            executor_id=str(value.get("executor_id") or ""), bridge_instance_id=str(value.get("bridge_instance_id") or ""), transport=str(value.get("transport") or ""), device_generation=int(value.get("device_generation") or 0), platform=dict(value.get("platform") or {}), capabilities=dict(value.get("capabilities") or {}), connector_sequence=int(value.get("connector_sequence") or 0),
        )
        session.messages = [AgentMessage(**item) for item in value.get("messages", [])]
        session.activities = [AgentActivity(**item) for item in value.get("activities", [])]
        session.requests = [AgentRequest(**item) for item in value.get("requests", [])]
        session.turns = [AgentTurn(**item) for item in value.get("turns", [])]
        return session
