from __future__ import annotations

from .base import (
    ApprovalDecision,
    JsonValue,
    PermissionMode,
    RuntimeAdapter,
    RuntimeAdapterError,
    RuntimeAdapterFactory,
    RuntimeAdapterRegistry,
    RuntimeAttachment,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeInteractionNotFoundError,
    RuntimeInteractionResolvedError,
    RuntimeInvalidDecisionError,
    RuntimeOperationTimeoutError,
    RuntimeProbe,
    RuntimeProtocolError,
    RuntimeRequestError,
    RuntimeSession,
    RuntimeSessionClosedError,
    RuntimeSessionNotFoundError,
    RuntimeSessionSpec,
    RuntimeSpawnError,
    RuntimeThreadSnapshot,
    RuntimeThreadTurnSnapshot,
    RuntimeTransportError,
    RuntimeTurn,
    RuntimeTurnInput,
    RuntimeTurnNotFoundError,
    RuntimeUnavailableError,
)
from .codex import (
    PROVIDER as CODEX_PROVIDER,
    CodexRuntimeAdapter,
    codex_permission_config,
    is_recoverable_thread_resume_error,
)


def create_default_runtime_adapter_registry() -> RuntimeAdapterRegistry:
    """Return an isolated registry containing the built-in native runtimes."""

    registry = RuntimeAdapterRegistry()
    registry.register(CODEX_PROVIDER, CodexRuntimeAdapter)
    return registry


DEFAULT_RUNTIME_ADAPTER_REGISTRY = create_default_runtime_adapter_registry()


__all__ = [
    "ApprovalDecision",
    "CODEX_PROVIDER",
    "CodexRuntimeAdapter",
    "DEFAULT_RUNTIME_ADAPTER_REGISTRY",
    "JsonValue",
    "PermissionMode",
    "RuntimeAdapter",
    "RuntimeAdapterError",
    "RuntimeAdapterFactory",
    "RuntimeAdapterRegistry",
    "RuntimeAttachment",
    "RuntimeCapabilities",
    "RuntimeEvent",
    "RuntimeInteractionNotFoundError",
    "RuntimeInteractionResolvedError",
    "RuntimeInvalidDecisionError",
    "RuntimeOperationTimeoutError",
    "RuntimeProbe",
    "RuntimeProtocolError",
    "RuntimeRequestError",
    "RuntimeSession",
    "RuntimeSessionClosedError",
    "RuntimeSessionNotFoundError",
    "RuntimeSessionSpec",
    "RuntimeSpawnError",
    "RuntimeThreadSnapshot",
    "RuntimeThreadTurnSnapshot",
    "RuntimeTransportError",
    "RuntimeTurn",
    "RuntimeTurnInput",
    "RuntimeTurnNotFoundError",
    "RuntimeUnavailableError",
    "codex_permission_config",
    "create_default_runtime_adapter_registry",
    "is_recoverable_thread_resume_error",
]
