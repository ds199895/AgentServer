from __future__ import annotations

from typing import Any


class ExecutionError(Exception):
    """Base error for the execution event core."""


class ValidationError(ExecutionError, ValueError):
    """An event or command does not satisfy the protocol contract."""


class MissingExpectedRevision(ValidationError):
    """A state-changing event omitted its compare-and-set revision."""


class RevisionConflict(ExecutionError):
    """The aggregate changed after the producer read it."""

    def __init__(self, expected: int, actual: int, state: dict[str, Any] | None = None):
        super().__init__(f"expected revision {expected}, current revision is {actual}")
        self.expected = expected
        self.actual = actual
        self.state = state


class InvalidTransition(ExecutionError, ValueError):
    """An event would violate an aggregate lifecycle state machine."""

    def __init__(self, aggregate_kind: str, current: str | None, target: str):
        current_label = current if current is not None else "<missing>"
        super().__init__(
            f"invalid {aggregate_kind} transition: {current_label} -> {target}"
        )
        self.aggregate_kind = aggregate_kind
        self.current = current
        self.target = target


class IdempotencyConflict(ExecutionError):
    """An idempotency key was reused for different event contents."""


class EntityNotFound(ExecutionError, LookupError):
    """A requested execution entity does not exist in the owner scope."""


class LeaseConflict(ExecutionError):
    """A resource has an incompatible active lease."""


class RelationConstraintError(ExecutionError, ValueError):
    """An entity relation violates ownership or graph constraints."""


class CommandConflict(ExecutionError):
    """A command acknowledgement or idempotency key is incompatible."""
