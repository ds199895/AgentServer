from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeAlias


JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class ApprovalDecision(str, Enum):
    APPROVE_ONCE = "approve_once"
    APPROVE_SESSION = "approve_session"
    DENY = "deny"
    CANCEL_TURN = "cancel_turn"


class PermissionMode(str, Enum):
    APPROVAL_REQUIRED = "approval-required"
    WORKSPACE_WRITE = "workspace-write"
    AUTO = "auto"
    FULL_ACCESS = "full-access"


@dataclass(frozen=True)
class RuntimeCapabilities:
    resume: bool = False
    interrupt: bool = False
    approvals: bool = False
    user_input: bool = False
    read_thread: bool = False
    rollback: bool = False
    model_switch: bool = False


@dataclass(frozen=True)
class RuntimeProbe:
    available: bool
    version: str | None = None
    detail_code: str | None = None


@dataclass(frozen=True)
class RuntimeAttachment:
    type: str
    url: str


@dataclass(frozen=True)
class RuntimeSessionSpec:
    session_id: str
    cwd: Path | str
    permission_mode: PermissionMode | str = PermissionMode.WORKSPACE_WRITE
    model: str | None = None
    service_tier: str | None = None
    resume_cursor: Mapping[str, JsonValue] | None = None
    environment: Mapping[str, str] | None = None


@dataclass(frozen=True)
class RuntimeSession:
    session_id: str
    provider: str
    state: str
    cwd: str
    model: str | None = None
    active_turn_id: str | None = None
    resume_cursor: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True)
class RuntimeTurnInput:
    text: str | None = None
    attachments: tuple[RuntimeAttachment, ...] = ()
    model: str | None = None
    service_tier: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class RuntimeTurn:
    session_id: str
    turn_id: str
    resume_cursor: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True)
class RuntimeThreadTurnSnapshot:
    id: str
    items: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class RuntimeThreadSnapshot:
    thread_id: str
    turns: tuple[RuntimeThreadTurnSnapshot, ...] = ()


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    provider: str
    session_id: str
    type: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    turn_id: str | None = None
    item_id: str | None = None
    interaction_id: str | None = None
    occurred_at: float = 0.0


class RuntimeAdapterError(RuntimeError):
    code = "runtime_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        operation: str = "",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.operation = operation
        self.cause = cause


class RuntimeUnavailableError(RuntimeAdapterError):
    code = "runtime_unavailable"


class RuntimeSpawnError(RuntimeAdapterError):
    code = "runtime_spawn_error"


class RuntimeTransportError(RuntimeAdapterError):
    code = "runtime_transport_error"
    retryable = True


class RuntimeProtocolError(RuntimeAdapterError):
    code = "runtime_protocol_error"


class RuntimeRequestError(RuntimeAdapterError):
    code = "runtime_request_error"

    def __init__(
        self,
        message: str,
        *,
        request_code: int | None = None,
        retryable: bool = False,
        **values: Any,
    ) -> None:
        super().__init__(message, **values)
        self.request_code = request_code
        self.retryable = bool(retryable)


class RuntimeSessionNotFoundError(RuntimeAdapterError):
    code = "runtime_session_not_found"


class RuntimeSessionClosedError(RuntimeAdapterError):
    code = "runtime_session_closed"


class RuntimeTurnNotFoundError(RuntimeAdapterError):
    code = "runtime_turn_not_found"


class RuntimeInteractionNotFoundError(RuntimeAdapterError):
    code = "runtime_interaction_not_found"


class RuntimeInteractionResolvedError(RuntimeAdapterError):
    code = "runtime_interaction_resolved"


class RuntimeInvalidDecisionError(RuntimeAdapterError):
    code = "runtime_invalid_decision"


class RuntimeOperationTimeoutError(RuntimeAdapterError):
    code = "runtime_operation_timeout"
    retryable = True


class RuntimeAdapter(ABC):
    provider: str
    capabilities: RuntimeCapabilities

    @abstractmethod
    async def probe(self) -> RuntimeProbe:
        raise NotImplementedError

    @abstractmethod
    async def start_session(self, spec: RuntimeSessionSpec) -> RuntimeSession:
        raise NotImplementedError

    @abstractmethod
    async def send_turn(
        self, session_id: str, turn: RuntimeTurnInput
    ) -> RuntimeTurn:
        raise NotImplementedError

    @abstractmethod
    async def interrupt_turn(
        self, session_id: str, turn_id: str | None = None
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def respond_to_approval(
        self,
        session_id: str,
        interaction_id: str,
        decision: ApprovalDecision | str,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def respond_to_user_input(
        self,
        session_id: str,
        interaction_id: str,
        answers: Mapping[str, str | Sequence[str]],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def read_thread(self, session_id: str) -> RuntimeThreadSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def rollback_thread(
        self, session_id: str, num_turns: int
    ) -> RuntimeThreadSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def stop_session(self, session_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_sessions(self) -> tuple[RuntimeSession, ...]:
        raise NotImplementedError

    @abstractmethod
    def events(self, session_id: str | None = None) -> AsyncIterator[RuntimeEvent]:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


RuntimeAdapterFactory = Callable[..., RuntimeAdapter]


class RuntimeAdapterRegistry:
    """Small provider-to-factory registry kept outside orchestration code."""

    def __init__(self) -> None:
        self._factories: dict[str, RuntimeAdapterFactory] = {}

    def register(
        self,
        provider: str,
        factory: RuntimeAdapterFactory,
        *,
        replace: bool = False,
    ) -> None:
        name = str(provider or "").strip().lower()
        if not name:
            raise ValueError("runtime adapter provider is required")
        if name in self._factories and not replace:
            raise ValueError(f"runtime adapter is already registered: {name}")
        self._factories[name] = factory

    def create(self, provider: str, **options: Any) -> RuntimeAdapter:
        name = str(provider or "").strip().lower()
        try:
            factory = self._factories[name]
        except KeyError as error:
            raise RuntimeUnavailableError(
                "runtime adapter is not registered", provider=name, operation="create"
            ) from error
        return factory(**options)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
