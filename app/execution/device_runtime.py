from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .errors import CommandConflict, ExecutionError, ValidationError
from .events import new_id
from .models import Command, CommandStatus
from .store import ExecutionStore


ENROLLMENT_TOKEN_PREFIX = "asde1"
CREDENTIAL_TOKEN_PREFIX = "asdc1"
DEVICE_COMMAND_TARGET = "device"

MAX_TOKEN_BYTES = 4096
MAX_IDENTIFIER_LENGTH = 255
MAX_CAPABILITY_BYTES = 64 * 1024
MAX_COMMAND_PAYLOAD_BYTES = 64 * 1024
MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
MAX_EVENT_BATCH_BYTES = 256 * 1024
MAX_EVENT_BATCH_SIZE = 100
MAX_PROVIDERS = 32
MAX_FEATURES = 64
IDEMPOTENCY_REPLAY_TTL = 5 * 60
MAX_SQLITE_INTEGER = 2**63 - 1
DEFAULT_MAX_ACTIVE_SESSIONS = 8
MAX_SESSION_EVENTS = 100_000
MAX_SESSION_EVENT_BYTES = 64 * 1024 * 1024
MAX_DEVICE_EVENTS = 500_000
MAX_DEVICE_EVENT_BYTES = 256 * 1024 * 1024


class DeviceRuntimeError(ExecutionError):
    """Base error for the device-runtime control plane."""


class DeviceRuntimeAuthenticationError(DeviceRuntimeError):
    """A device credential is missing, expired, revoked, or malformed."""


class DeviceRuntimeConflict(DeviceRuntimeError):
    """A replay or state transition conflicts with durable runtime state."""


class DeviceRuntimeNotFound(DeviceRuntimeError, LookupError):
    """A device-runtime resource does not exist in the requested owner scope."""


class DeviceRuntimeFenceError(DeviceRuntimeConflict):
    """A host or provider session no longer owns the active generation."""


def _identifier(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not 1 <= len(result) <= MAX_IDENTIFIER_LENGTH:
        raise ValidationError(f"{label} must contain 1..255 characters")
    return result


def _text(value: object, label: str, *, maximum: int, optional: bool = False) -> str:
    result = str(value or "").strip()
    if not result and optional:
        return ""
    if not result or len(result) > maximum:
        qualifier = f"1..{maximum}" if not optional else f"at most {maximum}"
        raise ValidationError(f"{label} must contain {qualifier} characters")
    return result


def _finite_timestamp(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be a finite timestamp")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be a finite timestamp")
    return result


def _json_object(
    value: Mapping[str, Any] | None,
    label: str,
    *,
    maximum_bytes: int,
) -> tuple[dict[str, Any], str]:
    if value is None:
        result: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        result = dict(value)
    else:
        raise ValidationError(f"{label} must be an object")
    try:
        encoded = json.dumps(
            result,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{label} must be a JSON object") from error
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValidationError(f"{label} exceeds {maximum_bytes} bytes")
    return json.loads(encoded), encoded


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token(prefix: str, identifier: str) -> str:
    return f"{prefix}.{identifier}.{secrets.token_urlsafe(32)}"


def _derived_token(
    prefix: str,
    identifier: str,
    *,
    key_material: str,
    purpose: str,
) -> str:
    digest = hmac.new(
        key_material.encode("utf-8"),
        f"agentserver/{purpose}/{identifier}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    secret = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{prefix}.{identifier}.{secret}"


def _enrollment_credential(token: str) -> tuple[str, str]:
    identifier = hashlib.sha256(
        b"agentserver/enrollment-credential-id\0" + token.encode("utf-8")
    ).hexdigest()[:32]
    return identifier, _derived_token(
        CREDENTIAL_TOKEN_PREFIX,
        identifier,
        key_material=token,
        purpose="enrollment-credential",
    )


def _rotation_credential(token: str, request_id: str) -> tuple[str, str]:
    identifier = hashlib.sha256(
        b"agentserver/rotation-credential-id\0"
        + token.encode("utf-8")
        + b"\0"
        + request_id.encode("utf-8")
    ).hexdigest()[:32]
    return identifier, _derived_token(
        CREDENTIAL_TOKEN_PREFIX,
        identifier,
        key_material=token,
        purpose=f"credential-rotation/{request_id}",
    )


def _parse_token(value: object, *, prefix: str, bearer: bool = False) -> tuple[str, str]:
    token = str(value or "").strip()
    if bearer:
        scheme, separator, credential = token.partition(" ")
        if not separator or scheme.lower() != "bearer":
            raise DeviceRuntimeAuthenticationError("missing device Bearer credential")
        token = credential.strip()
    if not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES or any(
        character.isspace() for character in token
    ):
        raise DeviceRuntimeAuthenticationError("device credential is malformed")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != prefix:
        raise DeviceRuntimeAuthenticationError("device credential is malformed")
    identifier = parts[1]
    if not identifier or len(identifier) > MAX_IDENTIFIER_LENGTH or not parts[2]:
        raise DeviceRuntimeAuthenticationError("device credential is malformed")
    return identifier, token


def _normalize_string_list(
    value: object,
    label: str,
    *,
    maximum_items: int = MAX_FEATURES,
    maximum_length: int = 100,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValidationError(f"{label} must contain at most {maximum_items} strings")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _text(item, label, maximum=maximum_length)
        if normalized in seen:
            raise ValidationError(f"{label} must not contain duplicates")
        seen.add(normalized)
        result.append(normalized)
    return result


def normalize_capabilities(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the bounded, versionable capability advertisement."""

    source = dict(value or {})
    unknown = set(source) - {"providers", "features"}
    if unknown:
        raise ValidationError(f"unknown capability fields: {sorted(unknown)}")
    providers_value = source.get("providers", [])
    if not isinstance(providers_value, list) or len(providers_value) > MAX_PROVIDERS:
        raise ValidationError(f"capabilities.providers must contain at most {MAX_PROVIDERS} providers")
    providers: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    allowed_provider_fields = {
        "id",
        "transport",
        "available",
        "version",
        "features",
        "reason",
    }
    for index, raw in enumerate(providers_value):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"capabilities.providers[{index}] must be an object")
        unknown_provider = set(raw) - allowed_provider_fields
        if unknown_provider:
            raise ValidationError(
                f"unknown provider capability fields: {sorted(unknown_provider)}"
            )
        provider_id = _text(raw.get("id"), "provider id", maximum=64)
        if provider_id in provider_ids:
            raise ValidationError("provider ids must be unique")
        provider_ids.add(provider_id)
        available = raw.get("available", False)
        if not isinstance(available, bool):
            raise ValidationError("provider available must be boolean")
        providers.append(
            {
                "id": provider_id,
                "transport": _text(
                    raw.get("transport"),
                    "provider transport",
                    maximum=64,
                    optional=True,
                ),
                "available": available,
                "version": _text(
                    raw.get("version"),
                    "provider version",
                    maximum=100,
                    optional=True,
                ),
                "features": _normalize_string_list(
                    raw.get("features"), "provider features"
                ),
                "reason": _text(
                    raw.get("reason"),
                    "provider reason",
                    maximum=500,
                    optional=True,
                ),
            }
        )
    result = {
        "providers": providers,
        "features": _normalize_string_list(source.get("features"), "runtime features"),
    }
    normalized, _encoded = _json_object(
        result, "capabilities", maximum_bytes=MAX_CAPABILITY_BYTES
    )
    return normalized


def normalize_platform(value: Mapping[str, Any] | None) -> dict[str, str]:
    source = dict(value or {})
    unknown = set(source) - {"os", "arch", "hostname"}
    if unknown:
        raise ValidationError(f"unknown platform fields: {sorted(unknown)}")
    return {
        key: _text(source.get(key), f"platform {key}", maximum=120, optional=True)
        for key in ("os", "arch", "hostname")
    }


@dataclass(frozen=True)
class EnrollmentGrant:
    enrollment_id: str
    owner_id: str
    device_id: str
    token: str
    created_at: float
    expires_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "enrollment_id": self.enrollment_id,
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "enrollment_token": self.token,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class DeviceCredentialClaims:
    credential_id: str
    owner_id: str
    device_id: str
    issued_at: float
    expires_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class CredentialGrant:
    token: str
    claims: DeviceCredentialClaims

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "token_type": "Bearer",
            **self.claims.as_dict(),
        }


@dataclass(frozen=True)
class DeviceRuntimeHost:
    owner_id: str
    device_id: str
    credential_id: str
    instance_id: str
    boot_id: str
    runtime_session_id: str
    generation: int
    protocol_version: int
    runtime_version: str
    health: str
    last_error: str
    capabilities: Mapping[str, Any]
    platform: Mapping[str, str]
    revision: int
    connected_at: float
    last_seen_at: float
    online_until: float

    def online(self, now: float) -> bool:
        return self.health != "revoked" and self.online_until > now

    def as_dict(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        revoked = self.health == "revoked"
        online = not revoked and self.online(current)
        if revoked:
            state = "revoked"
        elif not online:
            state = "offline"
        elif self.health == "degraded":
            state = "degraded"
        else:
            state = "online"
        return {
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "credential_id": self.credential_id,
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "runtime_session_id": self.runtime_session_id,
            "generation": self.generation,
            "protocol_version": self.protocol_version,
            "runtime_version": self.runtime_version,
            "health": self.health,
            "state": state,
            "online": online,
            "last_error": self.last_error,
            "capabilities": dict(self.capabilities),
            "platform": dict(self.platform),
            "revision": self.revision,
            "connected_at": self.connected_at,
            "last_seen_at": self.last_seen_at,
            "online_until": self.online_until,
        }


@dataclass(frozen=True)
class DeviceCommandPage:
    commands: tuple[Command, ...]
    next_sequence: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "commands": [command.as_dict() for command in self.commands],
            "next_sequence": self.next_sequence,
        }


@dataclass(frozen=True)
class RuntimeSession:
    session_id: str
    owner_id: str
    device_id: str
    provider: str
    workspace: str
    runtime_session_id: str
    runtime_generation: int
    lifecycle: str
    revision: int
    start_command_id: str
    provider_session_id: str
    active_request_id: str
    last_error: str
    attributes: Mapping[str, Any]
    created_at: float
    updated_at: float
    last_event_sequence: int

    def as_dict(self, *, stale: bool = False) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "provider": self.provider,
            "workspace": self.workspace,
            "runtime_session_id": self.runtime_session_id,
            "runtime_generation": self.runtime_generation,
            "lifecycle": self.lifecycle,
            "revision": self.revision,
            "start_command_id": self.start_command_id,
            "provider_session_id": self.provider_session_id,
            "active_request_id": self.active_request_id,
            "last_error": self.last_error,
            "attributes": dict(self.attributes),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_event_sequence": self.last_event_sequence,
            "stale": stale,
        }


@dataclass(frozen=True)
class RuntimeSessionEvent:
    sequence: int
    event_id: str
    owner_id: str
    device_id: str
    session_id: str
    runtime_session_id: str
    runtime_generation: int
    producer_seq: int
    type: str
    payload: Mapping[str, Any]
    occurred_at: float | None
    recorded_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "owner_id": self.owner_id,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "runtime_session_id": self.runtime_session_id,
            "runtime_generation": self.runtime_generation,
            "producer_seq": self.producer_seq,
            "type": self.type,
            "payload": dict(self.payload),
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class EventIngestResult:
    event_id: str
    producer_seq: int
    status: str
    sequence: int | None
    session_revision: int
    permanent: bool = False
    error_code: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "producer_seq": self.producer_seq,
            "status": self.status,
            "sequence": self.sequence,
            "session_revision": self.session_revision,
            "permanent": self.permanent,
            "error_code": self.error_code,
            "reason": self.reason,
        }


class DeviceRuntimeStore:
    """Durable device credentials, host generations, sessions and event streams."""

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS device_runtime_enrollments (
                    enrollment_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    invalidated_at REAL
                );
                CREATE INDEX IF NOT EXISTS device_runtime_enrollment_device
                ON device_runtime_enrollments(owner_id, device_id, created_at);

                CREATE TABLE IF NOT EXISTS device_runtime_credentials (
                    credential_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL,
                    replaced_by TEXT
                );
                CREATE INDEX IF NOT EXISTS device_runtime_credential_device
                ON device_runtime_credentials(owner_id, device_id, issued_at);

                CREATE TABLE IF NOT EXISTS device_runtime_hosts (
                    owner_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    credential_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL DEFAULT '',
                    boot_id TEXT NOT NULL DEFAULT '',
                    runtime_session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    protocol_version INTEGER NOT NULL,
                    runtime_version TEXT NOT NULL,
                    health TEXT NOT NULL,
                    last_error TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    platform_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    connected_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    online_until REAL NOT NULL,
                    PRIMARY KEY(owner_id, device_id)
                );

                CREATE TABLE IF NOT EXISTS device_runtime_sessions (
                    session_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    runtime_session_id TEXT NOT NULL,
                    runtime_generation INTEGER NOT NULL,
                    lifecycle TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    start_command_id TEXT NOT NULL,
                    active_turn_command_id TEXT NOT NULL DEFAULT '',
                    active_turn_revision INTEGER NOT NULL DEFAULT 0,
                    provider_session_id TEXT NOT NULL,
                    active_request_id TEXT NOT NULL,
                    last_error TEXT NOT NULL,
                    attributes_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_event_sequence INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS device_runtime_sessions_device
                ON device_runtime_sessions(owner_id, device_id, created_at);

                CREATE TABLE IF NOT EXISTS device_runtime_session_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    runtime_session_id TEXT NOT NULL,
                    runtime_generation INTEGER NOT NULL,
                    producer_seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at REAL,
                    recorded_at REAL NOT NULL,
                    UNIQUE(owner_id, event_id),
                    UNIQUE(
                        owner_id, session_id, runtime_session_id,
                        runtime_generation, producer_seq
                    ),
                    FOREIGN KEY(session_id) REFERENCES device_runtime_sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS device_runtime_events_session
                ON device_runtime_session_events(owner_id, session_id, sequence);
                """
            )
            host_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(device_runtime_hosts)"
                ).fetchall()
            }
            for column in ("instance_id", "boot_id"):
                if column not in host_columns:
                    connection.execute(
                        f"ALTER TABLE device_runtime_hosts "
                        f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            session_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(device_runtime_sessions)"
                ).fetchall()
            }
            if "active_turn_command_id" not in session_columns:
                connection.execute(
                    "ALTER TABLE device_runtime_sessions "
                    "ADD COLUMN active_turn_command_id TEXT NOT NULL DEFAULT ''"
                )
            if "active_turn_revision" not in session_columns:
                connection.execute(
                    "ALTER TABLE device_runtime_sessions "
                    "ADD COLUMN active_turn_revision INTEGER NOT NULL DEFAULT 0"
                )

    def issue_enrollment(
        self,
        *,
        owner_id: str,
        device_id: str,
        ttl: float = 300,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> EnrollmentGrant:
        owner_id = _identifier(owner_id, "owner_id")
        device_id = _identifier(device_id, "device_id")
        lifetime = float(ttl)
        if not math.isfinite(lifetime) or not 1 <= lifetime <= 24 * 60 * 60:
            raise ValidationError("enrollment ttl must be between 1 second and 24 hours")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        enrollment_id = new_id()
        token = _token(ENROLLMENT_TOKEN_PREFIX, enrollment_id)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            expires_at = timestamp + lifetime
            connection.execute(
                """
                UPDATE device_runtime_enrollments SET invalidated_at = ?
                WHERE owner_id = ? AND device_id = ?
                  AND consumed_at IS NULL AND invalidated_at IS NULL
                """,
                (timestamp, owner_id, device_id),
            )
            connection.execute(
                """
                INSERT INTO device_runtime_enrollments(
                    enrollment_id, token_hash, owner_id, device_id,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    enrollment_id,
                    _token_hash(token),
                    owner_id,
                    device_id,
                    timestamp,
                    expires_at,
                ),
            )
        return EnrollmentGrant(
            enrollment_id=enrollment_id,
            owner_id=owner_id,
            device_id=device_id,
            token=token,
            created_at=timestamp,
            expires_at=expires_at,
        )

    def enrollment_scope(
        self, token: str, *, now: float | None = None
    ) -> tuple[str, str]:
        enrollment_id, normalized = _parse_token(
            token, prefix=ENROLLMENT_TOKEN_PREFIX
        )
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM device_runtime_enrollments WHERE enrollment_id = ?",
                (enrollment_id,),
            ).fetchone()
        if (
            row is None
            or not hmac.compare_digest(str(row["token_hash"]), _token_hash(normalized))
            or row["invalidated_at"] is not None
            or (
                row["consumed_at"] is None
                and float(row["expires_at"]) <= timestamp
            )
            or (
                row["consumed_at"] is not None
                and timestamp
                >= float(row["consumed_at"]) + IDEMPOTENCY_REPLAY_TTL
            )
        ):
            raise DeviceRuntimeAuthenticationError("enrollment token is invalid or expired")
        return str(row["owner_id"]), str(row["device_id"])

    def consume_enrollment(
        self,
        token: str,
        *,
        credential_ttl: float = 90 * 24 * 60 * 60,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> CredentialGrant:
        enrollment_id, normalized = _parse_token(
            token, prefix=ENROLLMENT_TOKEN_PREFIX
        )
        lifetime = float(credential_ttl)
        if not math.isfinite(lifetime) or not 60 <= lifetime <= 366 * 24 * 60 * 60:
            raise ValidationError("credential ttl must be between 60 seconds and 366 days")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        credential_id, credential = _enrollment_credential(normalized)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            row = connection.execute(
                "SELECT * FROM device_runtime_enrollments WHERE enrollment_id = ?",
                (enrollment_id,),
            ).fetchone()
            if (
                row is None
                or not hmac.compare_digest(
                    str(row["token_hash"]), _token_hash(normalized)
                )
                or row["invalidated_at"] is not None
            ):
                raise DeviceRuntimeAuthenticationError(
                    "enrollment token is invalid or expired"
                )
            owner_id = str(row["owner_id"])
            device_id = str(row["device_id"])
            if row["consumed_at"] is not None:
                replay_deadline = (
                    float(row["consumed_at"]) + IDEMPOTENCY_REPLAY_TTL
                )
                existing = connection.execute(
                    "SELECT * FROM device_runtime_credentials "
                    "WHERE credential_id = ?",
                    (credential_id,),
                ).fetchone()
                if (
                    timestamp >= replay_deadline
                    or existing is None
                    or existing["revoked_at"] is not None
                    or float(existing["expires_at"]) <= timestamp
                    or not hmac.compare_digest(
                        str(existing["token_hash"]), _token_hash(credential)
                    )
                    or str(existing["owner_id"]) != owner_id
                    or str(existing["device_id"]) != device_id
                ):
                    raise DeviceRuntimeAuthenticationError(
                        "enrollment token is invalid or expired"
                    )
                claims = self._claims_from_row(existing)
                return CredentialGrant(token=credential, claims=claims)
            if float(row["expires_at"]) <= timestamp:
                raise DeviceRuntimeAuthenticationError(
                    "enrollment token is invalid or expired"
                )
            connection.execute(
                """
                UPDATE device_runtime_credentials
                SET revoked_at = ?, replaced_by = ?
                WHERE owner_id = ? AND device_id = ? AND revoked_at IS NULL
                """,
                (timestamp, credential_id, owner_id, device_id),
            )
            connection.execute(
                """
                UPDATE device_runtime_hosts
                SET health = 'revoked', online_until = ?,
                    last_error = 'device runtime credential replaced by enrollment',
                    revision = revision + 1
                WHERE owner_id = ? AND device_id = ?
                """,
                (timestamp, owner_id, device_id),
            )
            connection.execute(
                """
                UPDATE device_runtime_sessions
                SET lifecycle = 'lost', revision = revision + 1, updated_at = ?,
                    active_turn_command_id = '', active_turn_revision = 0,
                    last_error = 'device runtime credential replaced by enrollment'
                WHERE owner_id = ? AND device_id = ?
                  AND lifecycle NOT IN ('stopped', 'failed', 'lost')
                """,
                (timestamp, owner_id, device_id),
            )
            expires_at = timestamp + lifetime
            connection.execute(
                """
                INSERT INTO device_runtime_credentials(
                    credential_id, token_hash, owner_id, device_id,
                    issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    _token_hash(credential),
                    owner_id,
                    device_id,
                    timestamp,
                    expires_at,
                ),
            )
            updated = connection.execute(
                """
                UPDATE device_runtime_enrollments SET consumed_at = ?
                WHERE enrollment_id = ? AND consumed_at IS NULL
                  AND invalidated_at IS NULL
                """,
                (timestamp, enrollment_id),
            )
            if updated.rowcount != 1:
                raise DeviceRuntimeConflict("enrollment token was consumed concurrently")
        claims = DeviceCredentialClaims(
            credential_id=credential_id,
            owner_id=owner_id,
            device_id=device_id,
            issued_at=timestamp,
            expires_at=expires_at,
        )
        return CredentialGrant(token=credential, claims=claims)

    @staticmethod
    def _claims_from_row(row: sqlite3.Row) -> DeviceCredentialClaims:
        return DeviceCredentialClaims(
            credential_id=str(row["credential_id"]),
            owner_id=str(row["owner_id"]),
            device_id=str(row["device_id"]),
            issued_at=float(row["issued_at"]),
            expires_at=float(row["expires_at"]),
        )

    def authenticate_credential(
        self,
        value: str,
        *,
        bearer: bool = False,
        now: float | None = None,
    ) -> DeviceCredentialClaims:
        credential_id, token = _parse_token(
            value, prefix=CREDENTIAL_TOKEN_PREFIX, bearer=bearer
        )
        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM device_runtime_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        if (
            row is None
            or not hmac.compare_digest(str(row["token_hash"]), _token_hash(token))
            or row["revoked_at"] is not None
            or float(row["expires_at"]) <= timestamp
        ):
            raise DeviceRuntimeAuthenticationError(
                "device credential is invalid, expired, or revoked"
            )
        return self._claims_from_row(row)

    def validate_claims(
        self,
        claims: DeviceCredentialClaims,
        *,
        now: float | None = None,
    ) -> DeviceCredentialClaims:
        """Recheck a previously authenticated claims object at a side-effect boundary."""

        timestamp = self.clock() if now is None else float(now)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM device_runtime_credentials WHERE credential_id = ?",
                (claims.credential_id,),
            ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or float(row["expires_at"]) <= timestamp
            or str(row["owner_id"]) != claims.owner_id
            or str(row["device_id"]) != claims.device_id
            or float(row["issued_at"]) != claims.issued_at
            or float(row["expires_at"]) != claims.expires_at
        ):
            raise DeviceRuntimeAuthenticationError(
                "device credential claims are expired, revoked, or stale"
            )
        return self._claims_from_row(row)

    @classmethod
    def require_authenticated_host_on(
        cls,
        connection: sqlite3.Connection,
        claims: DeviceCredentialClaims,
        *,
        runtime_session_id: str,
        generation: int,
        require_online: bool = True,
        now: float,
    ) -> DeviceRuntimeHost:
        """Validate credential and Host fencing on an existing write transaction."""

        if not connection.in_transaction:
            raise RuntimeError("device runtime authorization requires a transaction")
        runtime_session_id = _identifier(
            runtime_session_id, "runtime_session_id"
        )
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or not 1 <= generation <= MAX_SQLITE_INTEGER
        ):
            raise ValidationError("runtime generation must be a positive int64")
        timestamp = float(now)
        credential = connection.execute(
            "SELECT * FROM device_runtime_credentials WHERE credential_id = ?",
            (claims.credential_id,),
        ).fetchone()
        if (
            credential is None
            or credential["revoked_at"] is not None
            or float(credential["expires_at"]) <= timestamp
            or str(credential["owner_id"]) != claims.owner_id
            or str(credential["device_id"]) != claims.device_id
            or float(credential["issued_at"]) != claims.issued_at
            or float(credential["expires_at"]) != claims.expires_at
        ):
            raise DeviceRuntimeAuthenticationError(
                "device credential claims are expired, revoked, or stale"
            )
        host_row = connection.execute(
            """
            SELECT * FROM device_runtime_hosts
            WHERE owner_id = ? AND device_id = ?
            """,
            (claims.owner_id, claims.device_id),
        ).fetchone()
        if host_row is None:
            raise DeviceRuntimeFenceError("device has no active runtime session")
        host = cls._host_from_row(host_row)
        if (
            host.credential_id != claims.credential_id
            or host.runtime_session_id != runtime_session_id
            or host.generation != generation
        ):
            raise DeviceRuntimeFenceError("runtime session generation fence is stale")
        if require_online and (
            host.health == "revoked" or not host.online(timestamp)
        ):
            raise DeviceRuntimeFenceError("device runtime session is offline")
        return host

    def rotate_credential(
        self,
        value: str,
        *,
        bearer: bool = False,
        credential_ttl: float = 90 * 24 * 60 * 60,
        request_id: str | None = None,
        runtime_session_id: str | None = None,
        generation: int | None = None,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> CredentialGrant:
        old_id, old_token = _parse_token(
            value, prefix=CREDENTIAL_TOKEN_PREFIX, bearer=bearer
        )
        rotation_id = _identifier(request_id or new_id(), "rotation request id")
        replacement_id, replacement_token = _rotation_credential(
            old_token, rotation_id
        )
        fenced = runtime_session_id is not None or generation is not None
        if fenced:
            runtime_session_id = _identifier(
                runtime_session_id, "runtime_session_id"
            )
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or not 1 <= generation <= MAX_SQLITE_INTEGER
            ):
                raise ValidationError(
                    "runtime generation must be a positive int64"
                )
        lifetime = float(credential_ttl)
        if not math.isfinite(lifetime) or not 60 <= lifetime <= 366 * 24 * 60 * 60:
            raise ValidationError("credential ttl must be between 60 seconds and 366 days")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            row = connection.execute(
                "SELECT * FROM device_runtime_credentials WHERE credential_id = ?",
                (old_id,),
            ).fetchone()
            if (
                row is None
                or not hmac.compare_digest(
                    str(row["token_hash"]), _token_hash(old_token)
                )
            ):
                raise DeviceRuntimeAuthenticationError(
                    "device credential is invalid, expired, or revoked"
                )
            if row["revoked_at"] is not None:
                replacement = connection.execute(
                    "SELECT * FROM device_runtime_credentials "
                    "WHERE credential_id = ?",
                    (replacement_id,),
                ).fetchone()
                if (
                    str(row["replaced_by"] or "") != replacement_id
                    or replacement is None
                    or replacement["revoked_at"] is not None
                    or float(replacement["expires_at"]) <= timestamp
                    or not hmac.compare_digest(
                        str(replacement["token_hash"]),
                        _token_hash(replacement_token),
                    )
                ):
                    raise DeviceRuntimeAuthenticationError(
                        "device credential is invalid, expired, or revoked"
                    )
                # A Host persists the high-entropy rotation request id before
                # sending it.  Replaying that exact id may be necessary long
                # after a response was lost or the device rebooted.  A
                # different request id derives a different replacement id and
                # is rejected above; explicit device revocation also revokes
                # the replacement, so an unbounded exact replay does not
                # resurrect an administratively revoked device.
                if fenced:
                    host = connection.execute(
                        "SELECT * FROM device_runtime_hosts "
                        "WHERE owner_id = ? AND device_id = ?",
                        (row["owner_id"], row["device_id"]),
                    ).fetchone()
                    if (
                        host is None
                        or str(host["credential_id"]) != replacement_id
                        or str(host["runtime_session_id"]) != runtime_session_id
                        or int(host["generation"]) != generation
                    ):
                        raise DeviceRuntimeFenceError(
                            "runtime session generation fence is stale"
                        )
                return CredentialGrant(
                    token=replacement_token,
                    claims=self._claims_from_row(replacement),
                )
            if float(row["expires_at"]) <= timestamp:
                raise DeviceRuntimeAuthenticationError(
                    "device credential is invalid, expired, or revoked"
                )
            if fenced:
                host = connection.execute(
                    "SELECT * FROM device_runtime_hosts "
                    "WHERE owner_id = ? AND device_id = ?",
                    (row["owner_id"], row["device_id"]),
                ).fetchone()
                if (
                    host is None
                    or str(host["credential_id"]) != old_id
                    or str(host["runtime_session_id"]) != runtime_session_id
                    or int(host["generation"]) != generation
                ):
                    raise DeviceRuntimeFenceError(
                        "runtime session generation fence is stale"
                    )
            expires_at = timestamp + lifetime
            try:
                connection.execute(
                    """
                    INSERT INTO device_runtime_credentials(
                        credential_id, token_hash, owner_id, device_id,
                        issued_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        replacement_id,
                        _token_hash(replacement_token),
                        row["owner_id"],
                        row["device_id"],
                        timestamp,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DeviceRuntimeConflict(
                    "credential rotation request id was reused"
                ) from error
            updated = connection.execute(
                """
                UPDATE device_runtime_credentials
                SET revoked_at = ?, replaced_by = ?
                WHERE credential_id = ? AND revoked_at IS NULL
                """,
                (timestamp, replacement_id, old_id),
            )
            if updated.rowcount != 1:
                raise DeviceRuntimeConflict("device credential was rotated concurrently")
            # Rotation is one atomic identity hand-off.  Leaving the live Host
            # row bound to the now-revoked credential creates a dead zone where
            # the replacement authenticates but cannot pass the Host fence
            # until another heartbeat happens to repair the binding.
            host_parameters: list[Any] = [
                replacement_id,
                row["owner_id"],
                row["device_id"],
                old_id,
            ]
            host_where = ""
            if fenced:
                host_where = " AND runtime_session_id = ? AND generation = ?"
                host_parameters.extend([runtime_session_id, generation])
            updated_host = connection.execute(
                """
                UPDATE device_runtime_hosts
                SET credential_id = ?, revision = revision + 1
                WHERE owner_id = ? AND device_id = ? AND credential_id = ?
                """
                + host_where,
                host_parameters,
            )
            if fenced and updated_host.rowcount != 1:
                raise DeviceRuntimeFenceError(
                    "runtime session generation fence changed during rotation"
                )
        claims = DeviceCredentialClaims(
            credential_id=replacement_id,
            owner_id=str(row["owner_id"]),
            device_id=str(row["device_id"]),
            issued_at=timestamp,
            expires_at=expires_at,
        )
        return CredentialGrant(token=replacement_token, claims=claims)

    def revoke_device(
        self,
        *,
        owner_id: str,
        device_id: str,
        credential_id: str | None = None,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> int:
        owner_id = _identifier(owner_id, "owner_id")
        device_id = _identifier(device_id, "device_id")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            host_row = connection.execute(
                """
                SELECT credential_id FROM device_runtime_hosts
                WHERE owner_id = ? AND device_id = ?
                """,
                (owner_id, device_id),
            ).fetchone()
            affects_host = credential_id is None or (
                host_row is not None
                and str(host_row["credential_id"]) == credential_id
            )
            query = (
                "UPDATE device_runtime_credentials SET revoked_at = ? "
                "WHERE owner_id = ? AND device_id = ? AND revoked_at IS NULL"
            )
            parameters: list[Any] = [timestamp, owner_id, device_id]
            if credential_id is not None:
                query += " AND credential_id = ?"
                parameters.append(_identifier(credential_id, "credential_id"))
            cursor = connection.execute(query, parameters)
            connection.execute(
                """
                UPDATE device_runtime_enrollments SET invalidated_at = ?
                WHERE owner_id = ? AND device_id = ?
                  AND consumed_at IS NULL AND invalidated_at IS NULL
                """,
                (timestamp, owner_id, device_id),
            )
            if affects_host:
                connection.execute(
                    """
                    UPDATE device_runtime_hosts
                    SET health = 'revoked', online_until = ?, revision = revision + 1
                    WHERE owner_id = ? AND device_id = ?
                    """,
                    (timestamp, owner_id, device_id),
                )
                connection.execute(
                    """
                    UPDATE device_runtime_sessions
                    SET lifecycle = 'lost', revision = revision + 1, updated_at = ?,
                        active_turn_command_id = '', active_turn_revision = 0,
                        last_error = 'device runtime credential revoked'
                    WHERE owner_id = ? AND device_id = ?
                      AND lifecycle NOT IN ('stopped', 'failed', 'lost')
                    """,
                    (timestamp, owner_id, device_id),
                )
        return int(cursor.rowcount)

    @staticmethod
    def _host_from_row(row: sqlite3.Row) -> DeviceRuntimeHost:
        return DeviceRuntimeHost(
            owner_id=str(row["owner_id"]),
            device_id=str(row["device_id"]),
            credential_id=str(row["credential_id"]),
            instance_id=str(row["instance_id"]),
            boot_id=str(row["boot_id"]),
            runtime_session_id=str(row["runtime_session_id"]),
            generation=int(row["generation"]),
            protocol_version=int(row["protocol_version"]),
            runtime_version=str(row["runtime_version"]),
            health=str(row["health"]),
            last_error=str(row["last_error"]),
            capabilities=json.loads(str(row["capabilities_json"])),
            platform=json.loads(str(row["platform_json"])),
            revision=int(row["revision"]),
            connected_at=float(row["connected_at"]),
            last_seen_at=float(row["last_seen_at"]),
            online_until=float(row["online_until"]),
        )

    def host(self, *, owner_id: str, device_id: str) -> DeviceRuntimeHost | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM device_runtime_hosts
                WHERE owner_id = ? AND device_id = ?
                """,
                (owner_id, device_id),
            ).fetchone()
        return self._host_from_row(row) if row is not None else None

    def heartbeat(
        self,
        claims: DeviceCredentialClaims,
        *,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
        protocol_version: int,
        runtime_version: str,
        health: str,
        last_error: str,
        capabilities: Mapping[str, Any],
        platform: Mapping[str, str],
        offline_after: float,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> DeviceRuntimeHost:
        instance_id = _identifier(instance_id, "instance_id")
        boot_id = _identifier(boot_id, "boot_id")
        runtime_session_id = _identifier(runtime_session_id, "runtime_session_id")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or not 1 <= generation <= MAX_SQLITE_INTEGER
        ):
            raise ValidationError("runtime generation must be a positive int64")
        if (
            not isinstance(protocol_version, int)
            or isinstance(protocol_version, bool)
            or not 1 <= protocol_version <= 1_000_000
        ):
            raise ValidationError("protocol_version must be a positive integer")
        runtime_version = _text(
            runtime_version, "runtime_version", maximum=100, optional=True
        )
        if health not in {"healthy", "degraded"}:
            raise ValidationError("runtime health must be healthy or degraded")
        last_error = _text(last_error, "last_error", maximum=1000, optional=True)
        delay = float(offline_after)
        if not math.isfinite(delay) or not 2 <= delay <= 10 * 60:
            raise ValidationError("offline_after must be between 2 and 600 seconds")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        capabilities_json = json.dumps(
            capabilities, separators=(",", ":"), sort_keys=True, allow_nan=False
        )
        platform_json = json.dumps(
            platform, separators=(",", ":"), sort_keys=True, allow_nan=False
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            credential = connection.execute(
                "SELECT * FROM device_runtime_credentials "
                "WHERE credential_id = ?",
                (claims.credential_id,),
            ).fetchone()
            if (
                credential is None
                or credential["revoked_at"] is not None
                or float(credential["expires_at"]) <= timestamp
                or str(credential["owner_id"]) != claims.owner_id
                or str(credential["device_id"]) != claims.device_id
                or float(credential["issued_at"]) != claims.issued_at
                or float(credential["expires_at"]) != claims.expires_at
            ):
                raise DeviceRuntimeAuthenticationError(
                    "device credential claims are expired, revoked, or stale"
                )
            current = connection.execute(
                """
                SELECT * FROM device_runtime_hosts
                WHERE owner_id = ? AND device_id = ?
                """,
                (claims.owner_id, claims.device_id),
            ).fetchone()
            connected_at = timestamp
            revision = 1
            replacing = False
            if current is not None:
                same_credential = str(current["credential_id"]) == claims.credential_id
                same_fence = (
                    str(current["runtime_session_id"]) == runtime_session_id
                    and int(current["generation"]) == generation
                )
                if same_credential:
                    if generation < int(current["generation"]):
                        raise DeviceRuntimeFenceError("runtime generation is stale")
                    if generation == int(current["generation"]) and not same_fence:
                        raise DeviceRuntimeFenceError(
                            "runtime session id changed without a new generation"
                        )
                    replacing = generation > int(current["generation"])
                else:
                    credential = connection.execute(
                        """
                        SELECT revoked_at FROM device_runtime_credentials
                        WHERE credential_id = ?
                        """,
                        (current["credential_id"],),
                    ).fetchone()
                    if credential is not None and credential["revoked_at"] is None:
                        raise DeviceRuntimeFenceError(
                            "another active credential owns the runtime session"
                        )
                    replacing = not same_fence
                connected_at = (
                    float(current["connected_at"]) if same_fence else timestamp
                )
                revision = int(current["revision"]) + 1
                if replacing:
                    connection.execute(
                        """
                        UPDATE device_runtime_sessions
                        SET lifecycle = 'lost', revision = revision + 1,
                            active_turn_command_id = '', active_turn_revision = 0,
                            updated_at = ?,
                            last_error = 'device runtime generation replaced'
                        WHERE owner_id = ? AND device_id = ?
                          AND lifecycle NOT IN ('stopped', 'failed', 'lost')
                        """,
                        (timestamp, claims.owner_id, claims.device_id),
                    )
            connection.execute(
                """
                INSERT INTO device_runtime_hosts(
                    owner_id, device_id, credential_id, instance_id, boot_id,
                    runtime_session_id,
                    generation, protocol_version, runtime_version, health,
                    last_error, capabilities_json, platform_json, revision,
                    connected_at, last_seen_at, online_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, device_id) DO UPDATE SET
                    credential_id = excluded.credential_id,
                    instance_id = excluded.instance_id,
                    boot_id = excluded.boot_id,
                    runtime_session_id = excluded.runtime_session_id,
                    generation = excluded.generation,
                    protocol_version = excluded.protocol_version,
                    runtime_version = excluded.runtime_version,
                    health = excluded.health,
                    last_error = excluded.last_error,
                    capabilities_json = excluded.capabilities_json,
                    platform_json = excluded.platform_json,
                    revision = excluded.revision,
                    connected_at = excluded.connected_at,
                    last_seen_at = excluded.last_seen_at,
                    online_until = excluded.online_until
                """,
                (
                    claims.owner_id,
                    claims.device_id,
                    claims.credential_id,
                    instance_id,
                    boot_id,
                    runtime_session_id,
                    generation,
                    protocol_version,
                    runtime_version,
                    health,
                    last_error,
                    capabilities_json,
                    platform_json,
                    revision,
                    connected_at,
                    timestamp,
                    min(timestamp + delay, claims.expires_at),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM device_runtime_hosts
                WHERE owner_id = ? AND device_id = ?
                """,
                (claims.owner_id, claims.device_id),
            ).fetchone()
        assert row is not None
        return self._host_from_row(row)

    def require_host_fence(
        self,
        claims: DeviceCredentialClaims,
        *,
        runtime_session_id: str,
        generation: int,
        require_online: bool = True,
        now: float | None = None,
    ) -> DeviceRuntimeHost:
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or not 1 <= generation <= MAX_SQLITE_INTEGER
        ):
            raise ValidationError("runtime generation must be a positive int64")
        host = self.host(owner_id=claims.owner_id, device_id=claims.device_id)
        timestamp = self.clock() if now is None else float(now)
        if host is None:
            raise DeviceRuntimeFenceError("device has no active runtime session")
        if (
            host.credential_id != claims.credential_id
            or host.runtime_session_id != runtime_session_id
            or host.generation != generation
        ):
            raise DeviceRuntimeFenceError("runtime session generation fence is stale")
        if require_online and not host.online(timestamp):
            raise DeviceRuntimeFenceError("device runtime session is offline")
        return host

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> RuntimeSession:
        return RuntimeSession(
            session_id=str(row["session_id"]),
            owner_id=str(row["owner_id"]),
            device_id=str(row["device_id"]),
            provider=str(row["provider"]),
            workspace=str(row["workspace"]),
            runtime_session_id=str(row["runtime_session_id"]),
            runtime_generation=int(row["runtime_generation"]),
            lifecycle=str(row["lifecycle"]),
            revision=int(row["revision"]),
            start_command_id=str(row["start_command_id"]),
            provider_session_id=str(row["provider_session_id"]),
            active_request_id=str(row["active_request_id"]),
            last_error=str(row["last_error"]),
            attributes=json.loads(str(row["attributes_json"])),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_event_sequence=int(row["last_event_sequence"]),
        )

    def create_session(
        self,
        *,
        owner_id: str,
        device_id: str,
        provider: str,
        workspace: str,
        runtime_session_id: str,
        runtime_generation: int,
        attributes: Mapping[str, Any],
        session_id: str,
        start_command_id: str,
        max_active_sessions: int = DEFAULT_MAX_ACTIVE_SESSIONS,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
        on_created: Callable[
            [sqlite3.Connection, RuntimeSession, float], None
        ]
        | None = None,
    ) -> tuple[RuntimeSession, bool]:
        values = {
            "owner_id": owner_id,
            "device_id": device_id,
            "provider": provider,
            "workspace": workspace,
            "runtime_session_id": runtime_session_id,
            "runtime_generation": runtime_generation,
            "attributes": dict(attributes),
            "session_id": session_id,
            "start_command_id": start_command_id,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                values, separators=(",", ":"), sort_keys=True, allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
        attributes_json = json.dumps(
            attributes, separators=(",", ":"), sort_keys=True, allow_nan=False
        )
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            existing = connection.execute(
                "SELECT * FROM device_runtime_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["fingerprint"]) != fingerprint:
                    raise DeviceRuntimeConflict(
                        "runtime session id was reused for different contents"
                    )
                return self._session_from_row(existing), False
            active_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM device_runtime_sessions
                    WHERE owner_id = ? AND device_id = ?
                      AND lifecycle NOT IN ('stopped', 'failed', 'lost')
                    """,
                    (owner_id, device_id),
                ).fetchone()[0]
            )
            if active_count >= max_active_sessions:
                raise DeviceRuntimeConflict(
                    "device runtime active session limit was reached"
                )
            connection.execute(
                """
                INSERT INTO device_runtime_sessions(
                    session_id, fingerprint, owner_id, device_id, provider,
                    workspace, runtime_session_id, runtime_generation,
                    lifecycle, revision, start_command_id, provider_session_id,
                    active_request_id, last_error, attributes_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'starting', 1, ?, '', '', '', ?, ?, ?)
                """,
                (
                    session_id,
                    fingerprint,
                    owner_id,
                    device_id,
                    provider,
                    workspace,
                    runtime_session_id,
                    runtime_generation,
                    start_command_id,
                    attributes_json,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM device_runtime_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            assert row is not None
            session = self._session_from_row(row)
            if on_created is not None:
                on_created(connection, session, timestamp)
        return session, True

    def session(self, *, owner_id: str, session_id: str) -> RuntimeSession | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM device_runtime_sessions
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner_id, session_id),
            ).fetchone()
        return self._session_from_row(row) if row is not None else None

    def sessions(
        self,
        *,
        owner_id: str,
        device_id: str | None = None,
        limit: int = 200,
    ) -> list[RuntimeSession]:
        if not 1 <= limit <= 1000:
            raise ValidationError("session limit must be between 1 and 1000")
        query = "SELECT * FROM device_runtime_sessions WHERE owner_id = ?"
        parameters: list[Any] = [owner_id]
        if device_id is not None:
            query += " AND device_id = ?"
            parameters.append(device_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._session_from_row(row) for row in rows]

    def mark_session_stopping(
        self,
        *,
        owner_id: str,
        session_id: str,
        expected_revision: int | None = None,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
        on_stopping: Callable[
            [sqlite3.Connection, RuntimeSession, RuntimeSession, float], None
        ]
        | None = None,
    ) -> tuple[RuntimeSession, bool]:
        if expected_revision is not None and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValidationError("expected session revision must be non-negative")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            row = connection.execute(
                """
                SELECT * FROM device_runtime_sessions
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner_id, session_id),
            ).fetchone()
            if row is None:
                raise DeviceRuntimeNotFound("runtime session does not exist")
            lifecycle = str(row["lifecycle"])
            if lifecycle == "stopping":
                return self._session_from_row(row), False
            if lifecycle in {"stopped", "failed", "lost"}:
                raise DeviceRuntimeConflict("runtime session is already terminal")
            if (
                expected_revision is not None
                and int(row["revision"]) != expected_revision
            ):
                raise DeviceRuntimeConflict(
                    "runtime session changed while queuing stop"
                )
            previous = self._session_from_row(row)
            updated = connection.execute(
                """
                UPDATE device_runtime_sessions
                SET lifecycle = 'stopping', revision = revision + 1, updated_at = ?
                WHERE owner_id = ? AND session_id = ? AND revision = ?
                  AND lifecycle = ?
                """,
                (
                    timestamp,
                    owner_id,
                    session_id,
                    previous.revision,
                    previous.lifecycle,
                ),
            )
            if updated.rowcount != 1:
                raise DeviceRuntimeConflict(
                    "runtime session changed while queuing stop"
                )
            result = connection.execute(
                "SELECT * FROM device_runtime_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            assert result is not None
            stopping = self._session_from_row(result)
            if on_stopping is not None:
                on_stopping(connection, previous, stopping, timestamp)
        return stopping, True

    def reserve_session_turn(
        self,
        *,
        owner_id: str,
        session_id: str,
        command_id: str,
        expected_revision: int,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
        on_reserved: Callable[
            [sqlite3.Connection, RuntimeSession, float], None
        ]
        | None = None,
    ) -> RuntimeSession:
        command_id = _identifier(command_id, "command_id")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            updated = connection.execute(
                """
                UPDATE device_runtime_sessions
                SET lifecycle = 'running', revision = revision + 1,
                    active_turn_command_id = ?, active_turn_revision = ?,
                    updated_at = ?, last_error = ''
                WHERE owner_id = ? AND session_id = ?
                  AND lifecycle = 'ready' AND revision = ?
                """,
                (
                    command_id,
                    expected_revision + 1,
                    timestamp,
                    owner_id,
                    session_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise DeviceRuntimeConflict(
                    "runtime session cannot accept a concurrent turn"
                )
            row = connection.execute(
                "SELECT * FROM device_runtime_sessions "
                "WHERE owner_id = ? AND session_id = ?",
                (owner_id, session_id),
            ).fetchone()
            assert row is not None
            session = self._session_from_row(row)
            if on_reserved is not None:
                on_reserved(connection, session, timestamp)
        return session

    def release_session_turn_reservation(
        self,
        *,
        owner_id: str,
        session_id: str,
        reserved_revision: int,
        command_id: str,
        message: str = "",
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        command_id = _identifier(command_id, "command_id")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            connection.execute(
                """
                UPDATE device_runtime_sessions
                SET lifecycle = 'ready', revision = revision + 1,
                    active_turn_command_id = '', active_turn_revision = 0,
                    updated_at = ?, last_error = ?
                WHERE owner_id = ? AND session_id = ?
                  AND lifecycle = 'running' AND revision = ?
                  AND active_turn_command_id = ?
                  AND active_turn_revision = ?
                """,
                (
                    timestamp,
                    _text(message, "turn error", maximum=1000, optional=True),
                    owner_id,
                    session_id,
                    reserved_revision,
                    command_id,
                    reserved_revision,
                ),
            )

    @staticmethod
    def reconcile_session_commands_on(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        now: float,
        device_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Repair session projections whose durable launch command cannot run."""

        if not connection.in_transaction:
            raise RuntimeError("session reconciliation requires a transaction")
        timestamp = float(now)
        connection.execute(
            """
            UPDATE execution_commands
            SET status = ?, acked_at = ?
            WHERE expires_at IS NOT NULL AND expires_at <= ?
              AND status IN (?, ?, ?)
            """,
            (
                CommandStatus.EXPIRED.value,
                timestamp,
                timestamp,
                CommandStatus.QUEUED.value,
                CommandStatus.DELIVERED.value,
                CommandStatus.ACCEPTED.value,
            ),
        )
        query = """
            SELECT * FROM device_runtime_sessions
            WHERE owner_id = ?
              AND (
                    lifecycle = 'starting'
                    OR lifecycle = 'stopping'
                    OR (
                        lifecycle = 'running'
                        AND active_turn_command_id != ''
                        AND active_turn_revision > 0
                    )
              )
        """
        parameters: list[Any] = [owner_id]
        if device_id is not None:
            query += " AND device_id = ?"
            parameters.append(device_id)
        if session_id is not None:
            query += " AND session_id = ?"
            parameters.append(session_id)
        rows = connection.execute(query, parameters).fetchall()
        reconciled = 0

        def command_can_still_produce_an_event(command: sqlite3.Row) -> bool:
            status = CommandStatus(str(command["status"]))
            if status in {
                CommandStatus.QUEUED,
                CommandStatus.DELIVERED,
                CommandStatus.ACCEPTED,
            }:
                return True
            if status is not CommandStatus.COMPLETED:
                return False
            expires_at = command["expires_at"]
            return expires_at is not None and float(expires_at) > timestamp

        def matching_command(
            *,
            command_id: str,
            command_type: str,
            session: sqlite3.Row,
            revision: int | None = None,
        ) -> sqlite3.Row | None:
            command = connection.execute(
                """
                SELECT * FROM execution_commands
                WHERE owner_id = ? AND command_id = ?
                """,
                (owner_id, command_id),
            ).fetchone()
            if command is None:
                return None
            try:
                payload = json.loads(str(command["payload_json"]))
            except (TypeError, ValueError):
                return None
            if not isinstance(payload, Mapping):
                return None
            payload_generation = payload.get("runtime_generation")
            if (
                str(command["target_kind"]) != DEVICE_COMMAND_TARGET
                or str(command["target_id"]) != str(session["device_id"])
                or str(command["command_type"]) != command_type
                or str(payload.get("session_id") or "")
                != str(session["session_id"])
                or str(payload.get("device_id") or "")
                != str(session["device_id"])
                or str(payload.get("runtime_session_id") or "")
                != str(session["runtime_session_id"])
                or not isinstance(payload_generation, int)
                or isinstance(payload_generation, bool)
                or payload_generation != int(session["runtime_generation"])
                or (
                    revision is not None
                    and payload.get("session_revision") != revision
                )
            ):
                return None
            return command

        for row in rows:
            lifecycle = str(row["lifecycle"])
            if lifecycle == "starting":
                command_id = str(row["start_command_id"])
                command = matching_command(
                    command_id=command_id,
                    command_type="session.start",
                    session=row,
                    revision=int(row["revision"]),
                )
                if command is not None and command_can_still_produce_an_event(command):
                    continue
                if command is None:
                    reason = "session start command is missing"
                elif str(command["status"]) == CommandStatus.COMPLETED.value:
                    reason = "session start completed without a lifecycle event"
                elif str(command["status"]) == CommandStatus.REJECTED.value:
                    reason = "session start command was rejected"
                else:
                    reason = "session start command expired"
                updated = connection.execute(
                    """
                    UPDATE device_runtime_sessions
                    SET lifecycle = 'failed', revision = revision + 1,
                        active_turn_command_id = '', active_turn_revision = 0,
                        updated_at = ?, last_error = ?
                    WHERE owner_id = ? AND session_id = ?
                      AND lifecycle = 'starting' AND start_command_id = ?
                    """,
                    (
                        timestamp,
                        reason,
                        owner_id,
                        row["session_id"],
                        command_id,
                    ),
                )
                reconciled += int(updated.rowcount)
                continue

            if lifecycle == "stopping":
                command_id = hashlib.sha256(
                    (
                        f"session.stop\0{owner_id}\0"
                        f"{str(row['session_id'])}"
                    ).encode("utf-8")
                ).hexdigest()[:32]
                command = matching_command(
                    command_id=command_id,
                    command_type="session.stop",
                    session=row,
                )
                if command is not None and command_can_still_produce_an_event(command):
                    continue
                if command is None:
                    reason = "session stop command is missing"
                elif str(command["status"]) == CommandStatus.COMPLETED.value:
                    reason = "session stop completed without a lifecycle event"
                elif str(command["status"]) == CommandStatus.REJECTED.value:
                    reason = "session stop command was rejected"
                else:
                    reason = "session stop command expired"
                updated = connection.execute(
                    """
                    UPDATE device_runtime_sessions
                    SET lifecycle = 'failed', revision = revision + 1,
                        active_request_id = '', active_turn_command_id = '',
                        active_turn_revision = 0, updated_at = ?, last_error = ?
                    WHERE owner_id = ? AND session_id = ?
                      AND lifecycle = 'stopping'
                    """,
                    (
                        timestamp,
                        reason,
                        owner_id,
                        row["session_id"],
                    ),
                )
                reconciled += int(updated.rowcount)
                continue

            command_id = str(row["active_turn_command_id"])
            reserved_revision = int(row["active_turn_revision"])
            if int(row["revision"]) != reserved_revision:
                continue
            command = matching_command(
                command_id=command_id,
                command_type="session.turn",
                session=row,
                revision=reserved_revision,
            )
            if command is not None and command_can_still_produce_an_event(command):
                continue
            completed_without_event = (
                command is not None
                and str(command["status"]) == CommandStatus.COMPLETED.value
            )
            if command is None:
                reason = "turn command is missing"
            elif completed_without_event:
                reason = "turn command completed without a lifecycle event"
            elif str(command["status"]) == CommandStatus.REJECTED.value:
                reason = "turn command was rejected"
            else:
                reason = "turn command expired"
            reconciled_lifecycle = (
                "failed" if completed_without_event else "ready"
            )
            updated = connection.execute(
                """
                UPDATE device_runtime_sessions
                SET lifecycle = ?, revision = revision + 1,
                    active_turn_command_id = '', active_turn_revision = 0,
                    updated_at = ?, last_error = ?
                WHERE owner_id = ? AND session_id = ?
                  AND lifecycle = 'running' AND revision = ?
                  AND active_turn_command_id = ?
                  AND active_turn_revision = ?
                """,
                (
                    reconciled_lifecycle,
                    timestamp,
                    reason,
                    owner_id,
                    row["session_id"],
                    reserved_revision,
                    command_id,
                    reserved_revision,
                ),
            )
            reconciled += int(updated.rowcount)
        return reconciled

    def reconcile_session_commands(
        self,
        *,
        owner_id: str,
        device_id: str | None = None,
        session_id: str | None = None,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> int:
        owner_id = _identifier(owner_id, "owner_id")
        if device_id is not None:
            device_id = _identifier(device_id, "device_id")
        if session_id is not None:
            session_id = _identifier(session_id, "session_id")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            return self.reconcile_session_commands_on(
                connection,
                owner_id=owner_id,
                device_id=device_id,
                session_id=session_id,
                now=timestamp,
            )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RuntimeSessionEvent:
        return RuntimeSessionEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            owner_id=str(row["owner_id"]),
            device_id=str(row["device_id"]),
            session_id=str(row["session_id"]),
            runtime_session_id=str(row["runtime_session_id"]),
            runtime_generation=int(row["runtime_generation"]),
            producer_seq=int(row["producer_seq"]),
            type=str(row["event_type"]),
            payload=json.loads(str(row["payload_json"])),
            occurred_at=(
                float(row["occurred_at"])
                if row["occurred_at"] is not None
                else None
            ),
            recorded_at=float(row["recorded_at"]),
        )

    @staticmethod
    def _project_session_event(
        lifecycle: str,
        active_request_id: str,
        provider_session_id: str,
        last_error: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, str, str, str]:
        terminal = {"stopped", "failed", "lost"}
        if event_type == "session.exited":
            if lifecycle not in terminal:
                raise DeviceRuntimeConflict(
                    "session.exited requires a terminal runtime session"
                )
            return lifecycle, active_request_id, provider_session_id, last_error
        if lifecycle in terminal:
            raise DeviceRuntimeConflict("runtime session is terminal")
        if event_type == "session.started":
            was_stopping = lifecycle == "stopping"
            if lifecycle not in {"starting", "stopping"}:
                raise DeviceRuntimeConflict("session.started requires a starting session")
            lifecycle = "stopping" if was_stopping else "ready"
            provider_session_id = _text(
                payload.get("provider_session_id"),
                "provider_session_id",
                maximum=255,
                optional=True,
            )
            last_error = ""
        elif event_type == "turn.started":
            was_stopping = lifecycle == "stopping"
            if lifecycle not in {"running", "stopping"}:
                raise DeviceRuntimeConflict("turn.started requires a reserved turn")
            lifecycle = "stopping" if was_stopping else "running"
            active_request_id = ""
        elif event_type in {
            "interaction.opened",
            "request.opened",
            "user-input.requested",
        }:
            was_stopping = lifecycle == "stopping"
            if lifecycle not in {"running", "stopping"}:
                raise DeviceRuntimeConflict(
                    f"{event_type} requires a running turn"
                )
            if active_request_id:
                raise DeviceRuntimeConflict(
                    f"{event_type} requires no active interaction"
                )
            interaction_id = payload.get("interaction_id") or payload.get("request_id")
            active_request_id = _identifier(interaction_id, "interaction_id")
            lifecycle = "stopping" if was_stopping else "waiting"
        elif event_type in {
            "interaction.resolved",
            "request.closed",
            "request.resolved",
            "user-input.resolved",
        }:
            interaction_id = _identifier(
                payload.get("interaction_id") or payload.get("request_id"),
                "interaction_id",
            )
            was_stopping = lifecycle == "stopping"
            if (
                lifecycle not in {"waiting", "stopping"}
                or active_request_id != interaction_id
            ):
                raise DeviceRuntimeConflict(
                    f"{event_type} does not match the active interaction"
                )
            active_request_id = ""
            lifecycle = "stopping" if was_stopping else "running"
        elif event_type == "turn.completed":
            was_stopping = lifecycle == "stopping"
            if lifecycle not in {"running", "waiting", "stopping"}:
                raise DeviceRuntimeConflict("turn.completed requires an active turn")
            lifecycle = "stopping" if was_stopping else "ready"
            active_request_id = ""
            last_error = ""
        elif event_type == "turn.failed":
            was_stopping = lifecycle == "stopping"
            if lifecycle not in {"running", "waiting", "stopping"}:
                raise DeviceRuntimeConflict("turn.failed requires an active turn")
            lifecycle = "stopping" if was_stopping else "ready"
            active_request_id = ""
            last_error = _text(
                payload.get("error"), "turn error", maximum=1000, optional=True
            )
        elif event_type == "session.stopped":
            lifecycle = "stopped"
            active_request_id = ""
        elif event_type == "session.failed":
            lifecycle = "failed"
            active_request_id = ""
            last_error = _text(
                payload.get("error"), "session error", maximum=1000, optional=True
            )
        return lifecycle, active_request_id, provider_session_id, last_error

    def append_session_events(
        self,
        *,
        claims: DeviceCredentialClaims,
        session_id: str,
        runtime_session_id: str,
        runtime_generation: int,
        events: Iterable[Mapping[str, Any]],
        now: float | None = None,
        clock: Callable[[], float] | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> list[EventIngestResult]:
        owner_id = claims.owner_id
        device_id = claims.device_id
        session_id = _identifier(session_id, "session_id")
        values = list(events)
        if not 1 <= len(values) <= MAX_EVENT_BATCH_SIZE:
            raise ValidationError("runtime event batch must contain 1..100 events")
        normalized: list[dict[str, Any]] = []
        batch_size = 0
        for raw in values:
            if not isinstance(raw, Mapping):
                raise ValidationError("runtime session events must be objects")
            event_id = _identifier(raw.get("event_id"), "event_id")
            event_type = _text(raw.get("type"), "event type", maximum=120)
            producer_seq = raw.get("producer_seq")
            if (
                not isinstance(producer_seq, int)
                or isinstance(producer_seq, bool)
                or not 0 <= producer_seq <= MAX_SQLITE_INTEGER
            ):
                raise ValidationError("producer_seq must be a non-negative int64")
            payload, payload_json = _json_object(
                raw.get("payload"),
                "runtime event payload",
                maximum_bytes=MAX_EVENT_PAYLOAD_BYTES,
            )
            occurred_at_value = raw.get("occurred_at")
            occurred_at = (
                None
                if occurred_at_value is None
                else _finite_timestamp(occurred_at_value, "occurred_at")
            )
            canonical = {
                "event_id": event_id,
                "type": event_type,
                "producer_seq": producer_seq,
                "payload": payload,
                "occurred_at": occurred_at,
                "session_id": session_id,
                "runtime_session_id": runtime_session_id,
                "runtime_generation": runtime_generation,
            }
            encoded = json.dumps(
                canonical, separators=(",", ":"), sort_keys=True, allow_nan=False
            )
            batch_size += len(encoded.encode("utf-8"))
            normalized.append(
                {
                    **canonical,
                    "payload_json": payload_json,
                    "fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                    "encoded_bytes": len(encoded.encode("utf-8")),
                    "storage_bytes": (
                        len(payload_json.encode("utf-8"))
                        + len(event_type.encode("utf-8"))
                        + 256
                    ),
                }
            )
        if batch_size > MAX_EVENT_BATCH_BYTES:
            raise ValidationError("runtime event batch exceeds 256 KiB")
        if now is not None and clock is not None:
            raise ValueError("now and clock are mutually exclusive")
        results: list[EventIngestResult] = []
        connection_context = (
            self._connect() if _connection is None else nullcontext(_connection)
        )
        with self._lock, connection_context as connection:
            if _connection is None:
                connection.execute("BEGIN IMMEDIATE")
            elif not connection.in_transaction:
                raise RuntimeError(
                    "shared runtime event connection requires a transaction"
                )
            timestamp = (
                float(clock())
                if clock is not None
                else self.clock() if now is None else float(now)
            )
            self.require_authenticated_host_on(
                connection,
                claims,
                runtime_session_id=runtime_session_id,
                generation=runtime_generation,
                now=timestamp,
            )
            self.reconcile_session_commands_on(
                connection,
                owner_id=owner_id,
                device_id=device_id,
                session_id=session_id,
                now=timestamp,
            )
            session_row = connection.execute(
                """
                SELECT * FROM device_runtime_sessions
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner_id, session_id),
            ).fetchone()
            if session_row is None:
                return [
                    EventIngestResult(
                        event_id=str(event["event_id"]),
                        producer_seq=int(event["producer_seq"]),
                        status="rejected",
                        sequence=None,
                        session_revision=0,
                        permanent=True,
                        error_code="session_not_found",
                        reason="server_session_not_found",
                    )
                    for event in normalized
                ]
            if (
                str(session_row["device_id"]) != device_id
                or str(session_row["runtime_session_id"]) != runtime_session_id
                or int(session_row["runtime_generation"]) != runtime_generation
            ):
                raise DeviceRuntimeFenceError("runtime provider session fence is stale")
            lifecycle = str(session_row["lifecycle"])
            active_request_id = str(session_row["active_request_id"])
            active_turn_command_id = str(session_row["active_turn_command_id"])
            active_turn_revision = int(session_row["active_turn_revision"])
            provider_session_id = str(session_row["provider_session_id"])
            last_error = str(session_row["last_error"])
            revision = int(session_row["revision"])
            last_sequence = int(session_row["last_event_sequence"])

            rejection_reasons = {
                "retention_quota": "server_retention_quota",
                "session_terminal": "server_session_terminal",
                "invalid_transition": "server_invalid_transition",
                "identity_conflict": "server_event_identity_conflict",
            }

            def reject_event(
                event: Mapping[str, Any], error_code: str
            ) -> EventIngestResult:
                nonlocal lifecycle
                nonlocal active_request_id
                nonlocal active_turn_command_id
                nonlocal active_turn_revision
                nonlocal last_error
                nonlocal revision
                reason = rejection_reasons[error_code]
                if lifecycle not in {"stopped", "failed", "lost"}:
                    lifecycle = "failed"
                    active_request_id = ""
                    active_turn_command_id = ""
                    active_turn_revision = 0
                    last_error = reason
                    revision += 1
                return EventIngestResult(
                    event_id=str(event["event_id"]),
                    producer_seq=int(event["producer_seq"]),
                    status="rejected",
                    sequence=None,
                    session_revision=revision,
                    permanent=True,
                    error_code=error_code,
                    reason=reason,
                )

            def turn_command(
                *, reservation_revision: int | None
            ) -> tuple[sqlite3.Row, Mapping[str, Any], Mapping[str, Any]]:
                if not active_turn_command_id:
                    raise DeviceRuntimeConflict(
                        "turn event has no active command reservation"
                    )
                command = connection.execute(
                    """
                    SELECT * FROM execution_commands
                    WHERE owner_id = ? AND command_id = ?
                    """,
                    (owner_id, active_turn_command_id),
                ).fetchone()
                if command is None:
                    raise DeviceRuntimeConflict(
                        "turn event command reservation is missing"
                    )
                try:
                    command_payload = json.loads(str(command["payload_json"]))
                    ack_payload = json.loads(str(command["ack_payload_json"]))
                except (TypeError, ValueError) as error:
                    raise DeviceRuntimeConflict(
                        "turn event command reservation is invalid"
                    ) from error
                if not isinstance(command_payload, Mapping) or not isinstance(
                    ack_payload, Mapping
                ):
                    raise DeviceRuntimeConflict(
                        "turn event command reservation is invalid"
                    )
                generation_value = command_payload.get("runtime_generation")
                if (
                    str(command["target_kind"]) != DEVICE_COMMAND_TARGET
                    or str(command["target_id"]) != device_id
                    or str(command["command_type"])
                    not in {"session.turn", "turn.start"}
                    or str(command_payload.get("session_id") or "") != session_id
                    or str(command_payload.get("device_id") or "") != device_id
                    or str(command_payload.get("runtime_session_id") or "")
                    != runtime_session_id
                    or not isinstance(generation_value, int)
                    or isinstance(generation_value, bool)
                    or generation_value != runtime_generation
                    or (
                        reservation_revision is not None
                        and command_payload.get("session_revision")
                        != reservation_revision
                    )
                ):
                    raise DeviceRuntimeConflict(
                        "turn event does not match the active command reservation"
                    )
                return command, command_payload, ack_payload

            def latest_started_turn_id() -> str:
                row = connection.execute(
                    """
                    SELECT payload_json FROM device_runtime_session_events
                    WHERE owner_id = ? AND session_id = ?
                      AND event_type = 'turn.started'
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (owner_id, session_id),
                ).fetchone()
                if row is None:
                    return ""
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (TypeError, ValueError):
                    return ""
                return (
                    str(payload.get("turn_id") or "")
                    if isinstance(payload, Mapping)
                    else ""
                )
            session_usage = connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                       COALESCE(SUM(
                           length(CAST(payload_json AS BLOB))
                           + length(CAST(event_type AS BLOB)) + 256
                       ), 0) AS event_bytes
                FROM device_runtime_session_events
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner_id, session_id),
            ).fetchone()
            device_usage = connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                       COALESCE(SUM(
                           length(CAST(payload_json AS BLOB))
                           + length(CAST(event_type AS BLOB)) + 256
                       ), 0) AS event_bytes
                FROM device_runtime_session_events
                WHERE owner_id = ? AND device_id = ?
                """,
                (owner_id, device_id),
            ).fetchone()
            assert session_usage is not None and device_usage is not None
            session_event_count = int(session_usage["event_count"])
            session_event_bytes = int(session_usage["event_bytes"])
            device_event_count = int(device_usage["event_count"])
            device_event_bytes = int(device_usage["event_bytes"])
            for event in normalized:
                by_id = connection.execute(
                    """
                    SELECT * FROM device_runtime_session_events
                    WHERE owner_id = ? AND event_id = ?
                    """,
                    (owner_id, event["event_id"]),
                ).fetchone()
                by_position = connection.execute(
                    """
                    SELECT * FROM device_runtime_session_events
                    WHERE owner_id = ? AND session_id = ?
                      AND runtime_session_id = ? AND runtime_generation = ?
                      AND producer_seq = ?
                    """,
                    (
                        owner_id,
                        session_id,
                        runtime_session_id,
                        runtime_generation,
                        event["producer_seq"],
                    ),
                ).fetchone()
                existing = by_id or by_position
                if existing is not None:
                    if (
                        str(existing["event_id"]) != event["event_id"]
                        or str(existing["fingerprint"]) != event["fingerprint"]
                    ):
                        results.append(reject_event(event, "identity_conflict"))
                        continue
                    results.append(
                        EventIngestResult(
                            event_id=event["event_id"],
                            producer_seq=event["producer_seq"],
                            status="duplicate",
                            sequence=int(existing["sequence"]),
                            session_revision=revision,
                        )
                    )
                    continue
                if (
                    lifecycle in {"stopped", "failed", "lost"}
                    and event["type"] != "session.exited"
                ):
                    results.append(reject_event(event, "session_terminal"))
                    continue
                event_bytes = int(event["storage_bytes"])
                if (
                    session_event_count + 1 > MAX_SESSION_EVENTS
                    or session_event_bytes + event_bytes > MAX_SESSION_EVENT_BYTES
                    or device_event_count + 1 > MAX_DEVICE_EVENTS
                    or device_event_bytes + event_bytes > MAX_DEVICE_EVENT_BYTES
                ):
                    results.append(reject_event(event, "retention_quota"))
                    continue
                event_type = str(event["type"])
                event_payload = event["payload"]
                projection_before = (
                    lifecycle,
                    active_request_id,
                    active_turn_command_id,
                    active_turn_revision,
                    provider_session_id,
                    last_error,
                )
                try:
                    if event_type == "session.started" and lifecycle == "stopping":
                        already_started = connection.execute(
                            """
                            SELECT 1 FROM device_runtime_session_events
                            WHERE owner_id = ? AND session_id = ?
                              AND event_type = 'session.started'
                            LIMIT 1
                            """,
                            (owner_id, session_id),
                        ).fetchone()
                        if already_started is not None:
                            raise DeviceRuntimeConflict(
                                "session.started was already projected"
                            )
                    elif event_type == "turn.started":
                        reservation_matches = (
                            lifecycle == "running"
                            and active_turn_revision == revision
                        ) or (
                            lifecycle == "stopping"
                            and active_turn_revision == revision - 1
                        )
                        if not reservation_matches or active_turn_revision <= 0:
                            raise DeviceRuntimeConflict(
                                "turn.started does not match an active reservation"
                            )
                        command, command_payload, ack_payload = turn_command(
                            reservation_revision=active_turn_revision
                        )
                        event_turn_id = _identifier(
                            event_payload.get("turn_id"), "turn_id"
                        )
                        acknowledged_turn_id = str(ack_payload.get("turn_id") or "")
                        client_turn_id = _identifier(
                            command_payload.get("turn_id"), "turn_id"
                        )
                        expected_turn_id = acknowledged_turn_id or client_turn_id
                        if event_turn_id != expected_turn_id:
                            raise DeviceRuntimeConflict(
                                "turn.started does not match the reserved turn"
                            )
                    elif event_type in {
                        "interaction.opened",
                        "request.opened",
                        "user-input.requested",
                        "interaction.resolved",
                        "request.closed",
                        "request.resolved",
                        "user-input.resolved",
                    } and lifecycle == "stopping":
                        if active_turn_revision != 0 or not active_turn_command_id:
                            raise DeviceRuntimeConflict(
                                f"{event_type} has no started turn reservation"
                            )
                        turn_command(reservation_revision=None)
                        started_turn_id = latest_started_turn_id()
                        if not started_turn_id:
                            raise DeviceRuntimeConflict(
                                f"{event_type} has no active turn"
                            )
                        interaction_turn_id = event_payload.get("turn_id")
                        if (
                            interaction_turn_id not in {None, ""}
                            and _identifier(interaction_turn_id, "turn_id")
                            != started_turn_id
                        ):
                            raise DeviceRuntimeConflict(
                                f"{event_type} does not match the active turn"
                            )
                    elif event_type in {"turn.completed", "turn.failed"}:
                        event_turn_id = _identifier(
                            event_payload.get("turn_id"), "turn_id"
                        )
                        if active_turn_revision != 0 or not active_turn_command_id:
                            raise DeviceRuntimeConflict(
                                f"{event_type} has no started turn reservation"
                            )
                        turn_command(reservation_revision=None)
                        if latest_started_turn_id() != event_turn_id:
                            raise DeviceRuntimeConflict(
                                f"{event_type} does not match the active turn"
                            )
                    (
                        lifecycle,
                        active_request_id,
                        provider_session_id,
                        last_error,
                    ) = self._project_session_event(
                        lifecycle,
                        active_request_id,
                        provider_session_id,
                        last_error,
                        event_type,
                        event_payload,
                    )
                except (DeviceRuntimeConflict, ValidationError):
                    results.append(reject_event(event, "invalid_transition"))
                    continue
                if event_type == "turn.started":
                    # Keep the command id as the durable turn identity, while
                    # zero marks that provider execution has actually started.
                    active_turn_revision = 0
                elif event_type in {
                    "turn.completed",
                    "turn.failed",
                    "session.stopped",
                    "session.failed",
                }:
                    active_turn_command_id = ""
                    active_turn_revision = 0
                cursor = connection.execute(
                    """
                    INSERT INTO device_runtime_session_events(
                        event_id, fingerprint, owner_id, device_id, session_id,
                        runtime_session_id, runtime_generation, producer_seq,
                        event_type, payload_json, occurred_at, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["fingerprint"],
                        owner_id,
                        device_id,
                        session_id,
                        runtime_session_id,
                        runtime_generation,
                        event["producer_seq"],
                        event["type"],
                        event["payload_json"],
                        event["occurred_at"],
                        timestamp,
                    ),
                )
                last_sequence = int(cursor.lastrowid)
                projection_after = (
                    lifecycle,
                    active_request_id,
                    active_turn_command_id,
                    active_turn_revision,
                    provider_session_id,
                    last_error,
                )
                # Observation events advance the durable event cursor without
                # invalidating a pending start/turn command reservation.  The
                # revision is the session projection CAS, not an event count.
                if projection_after != projection_before or event_type == "session.exited":
                    revision += 1
                session_event_count += 1
                session_event_bytes += event_bytes
                device_event_count += 1
                device_event_bytes += event_bytes
                results.append(
                    EventIngestResult(
                        event_id=event["event_id"],
                        producer_seq=event["producer_seq"],
                        status="accepted",
                        sequence=last_sequence,
                        session_revision=revision,
                    )
                )
            connection.execute(
                """
                UPDATE device_runtime_sessions
                SET lifecycle = ?, active_request_id = ?,
                    active_turn_command_id = ?, active_turn_revision = ?,
                    provider_session_id = ?, last_error = ?, revision = ?,
                    last_event_sequence = ?, updated_at = ?
                WHERE owner_id = ? AND session_id = ?
                """,
                (
                    lifecycle,
                    active_request_id,
                    active_turn_command_id,
                    active_turn_revision,
                    provider_session_id,
                    last_error,
                    revision,
                    last_sequence,
                    timestamp,
                    owner_id,
                    session_id,
                ),
            )
        return results

    def session_events(
        self,
        *,
        owner_id: str,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[RuntimeSessionEvent]:
        if after_sequence < 0:
            raise ValidationError("after_sequence must be non-negative")
        if not 1 <= limit <= 1000:
            raise ValidationError("event limit must be between 1 and 1000")
        if self.session(owner_id=owner_id, session_id=session_id) is None:
            raise DeviceRuntimeNotFound("runtime session does not exist")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM device_runtime_session_events
                WHERE owner_id = ? AND session_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (owner_id, session_id, after_sequence, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]


class DeviceRuntimeService:
    """Owner/device-fenced orchestration over DeviceRuntimeStore and CommandQueue."""

    def __init__(
        self,
        store: DeviceRuntimeStore,
        execution_store: ExecutionStore,
        *,
        device_exists: Callable[[str, str], bool],
        clock: Callable[[], float] | None = None,
        offline_after: float = 30,
        max_active_sessions: int = DEFAULT_MAX_ACTIVE_SESSIONS,
    ) -> None:
        if store.database_path.resolve() != execution_store.database_path.resolve():
            raise ValueError(
                "device runtime and execution stores must share one SQLite database"
            )
        self.store = store
        self.execution_store = execution_store
        self.device_exists = device_exists
        self.clock = clock or store.clock
        self.offline_after = float(offline_after)
        if not math.isfinite(self.offline_after) or not 2 <= self.offline_after <= 600:
            raise ValueError("offline_after must be between 2 and 600 seconds")
        if (
            not isinstance(max_active_sessions, int)
            or isinstance(max_active_sessions, bool)
            or not 1 <= max_active_sessions <= 64
        ):
            raise ValueError("max_active_sessions must be between 1 and 64")
        self.max_active_sessions = max_active_sessions

    def _require_device(self, owner_id: str, device_id: str) -> None:
        if not self.device_exists(owner_id, device_id):
            raise DeviceRuntimeNotFound("device does not exist in owner scope")

    def _require_claims(
        self, claims: DeviceCredentialClaims
    ) -> DeviceCredentialClaims:
        current = self.store.validate_claims(claims, now=self.clock())
        self._require_device(current.owner_id, current.device_id)
        return current

    def issue_enrollment(
        self,
        *,
        owner_id: str,
        device_id: str,
        ttl: float = 300,
    ) -> EnrollmentGrant:
        self._require_device(owner_id, device_id)
        return self.store.issue_enrollment(
            owner_id=owner_id, device_id=device_id, ttl=ttl, clock=self.clock
        )

    def consume_enrollment(
        self,
        token: str,
        *,
        credential_ttl: float = 90 * 24 * 60 * 60,
    ) -> CredentialGrant:
        owner_id, device_id = self.store.enrollment_scope(token, now=self.clock())
        self._require_device(owner_id, device_id)
        grant = self.store.consume_enrollment(
            token, credential_ttl=credential_ttl, clock=self.clock
        )
        try:
            self._require_device(grant.claims.owner_id, grant.claims.device_id)
        except DeviceRuntimeNotFound:
            self.store.revoke_device(
                owner_id=grant.claims.owner_id,
                device_id=grant.claims.device_id,
                credential_id=grant.claims.credential_id,
                clock=self.clock,
            )
            raise
        return grant

    def authenticate(
        self, value: str, *, bearer: bool = False
    ) -> DeviceCredentialClaims:
        claims = self.store.authenticate_credential(
            value, bearer=bearer, now=self.clock()
        )
        self._require_device(claims.owner_id, claims.device_id)
        return claims

    def rotate_credential(
        self,
        value: str,
        *,
        bearer: bool = False,
        credential_ttl: float = 90 * 24 * 60 * 60,
        request_id: str | None = None,
        runtime_session_id: str | None = None,
        generation: int | None = None,
    ) -> CredentialGrant:
        grant = self.store.rotate_credential(
            value,
            bearer=bearer,
            credential_ttl=credential_ttl,
            request_id=request_id,
            runtime_session_id=runtime_session_id,
            generation=generation,
            clock=self.clock,
        )
        try:
            self._require_device(grant.claims.owner_id, grant.claims.device_id)
        except DeviceRuntimeNotFound:
            self.store.revoke_device(
                owner_id=grant.claims.owner_id,
                device_id=grant.claims.device_id,
                credential_id=grant.claims.credential_id,
                clock=self.clock,
            )
            raise
        return grant

    def revoke_device(
        self, *, owner_id: str, device_id: str, credential_id: str | None = None
    ) -> int:
        return self.store.revoke_device(
            owner_id=owner_id,
            device_id=device_id,
            credential_id=credential_id,
            clock=self.clock,
        )

    def heartbeat(
        self,
        claims: DeviceCredentialClaims,
        *,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
        capabilities: Mapping[str, Any] | None = None,
        protocol_version: int = 1,
        runtime_version: str = "",
        platform: Mapping[str, Any] | None = None,
        health: str = "healthy",
        last_error: str = "",
    ) -> DeviceRuntimeHost:
        claims = self._require_claims(claims)
        return self.store.heartbeat(
            claims,
            instance_id=instance_id,
            boot_id=boot_id,
            runtime_session_id=runtime_session_id,
            generation=generation,
            protocol_version=protocol_version,
            runtime_version=runtime_version,
            health=health,
            last_error=last_error,
            capabilities=normalize_capabilities(capabilities),
            platform=normalize_platform(platform),
            offline_after=self.offline_after,
            clock=self.clock,
        )

    def runtime_status(self, *, owner_id: str, device_id: str) -> dict[str, Any]:
        self._require_device(owner_id, device_id)
        host = self.store.host(owner_id=owner_id, device_id=device_id)
        if host is None:
            return {
                "owner_id": owner_id,
                "device_id": device_id,
                "state": "unregistered",
                "online": False,
                "instance_id": "",
                "boot_id": "",
                "runtime_session_id": "",
                "generation": 0,
                "protocol_version": 0,
                "runtime_version": "",
                "health": "unregistered",
                "last_error": "",
                "capabilities": {"providers": [], "features": []},
                "platform": {"os": "", "arch": "", "hostname": ""},
                "revision": 0,
                "connected_at": None,
                "last_seen_at": None,
                "online_until": None,
            }
        return host.as_dict(now=self.clock())

    @staticmethod
    def _command_fence(host: DeviceRuntimeHost) -> dict[str, Any]:
        return {
            "device_id": host.device_id,
            "runtime_session_id": host.runtime_session_id,
            "runtime_generation": host.generation,
        }

    def _active_host(
        self, *, owner_id: str, device_id: str, require_online: bool = True
    ) -> DeviceRuntimeHost:
        self._require_device(owner_id, device_id)
        host = self.store.host(owner_id=owner_id, device_id=device_id)
        if host is None:
            raise DeviceRuntimeFenceError("device has no active runtime session")
        if require_online and not host.online(self.clock()):
            raise DeviceRuntimeFenceError("device runtime session is offline")
        return host

    def enqueue_device_command(
        self,
        *,
        owner_id: str,
        device_id: str,
        command_type: str,
        payload: Mapping[str, Any] | None = None,
        command_id: str | None = None,
        ttl: float = 60,
        require_online: bool = True,
        _expected_host: DeviceRuntimeHost | None = None,
    ) -> Command:
        self._require_device(owner_id, device_id)
        body = dict(payload or {})
        reserved = {"device_id", "runtime_session_id", "runtime_generation"}
        if reserved.intersection(body):
            raise ValidationError("device command payload contains reserved fence fields")
        lifetime = float(ttl)
        if not math.isfinite(lifetime) or not 1 <= lifetime <= 24 * 60 * 60:
            raise ValidationError("command ttl must be between 1 second and 24 hours")
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = float(self.clock())
            row = connection.execute(
                """
                SELECT * FROM device_runtime_hosts
                WHERE owner_id = ? AND device_id = ?
                """,
                (owner_id, device_id),
            ).fetchone()
            if row is None:
                raise DeviceRuntimeFenceError(
                    "device has no active runtime session"
                )
            host = self.store._host_from_row(row)
            if _expected_host is not None and (
                host.owner_id != _expected_host.owner_id
                or host.device_id != _expected_host.device_id
                or host.runtime_session_id != _expected_host.runtime_session_id
                or host.generation != _expected_host.generation
            ):
                raise DeviceRuntimeFenceError(
                    "device runtime session changed while queuing the command"
                )
            if require_online and not host.online(timestamp):
                raise DeviceRuntimeFenceError("device runtime session is offline")
            fenced_body, _encoded = _json_object(
                {**body, **self._command_fence(host)},
                "device command payload",
                maximum_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            )
            return self.execution_store.command_queue.enqueue(
                owner_id=owner_id,
                target_kind=DEVICE_COMMAND_TARGET,
                target_id=device_id,
                command_type=command_type,
                payload=fenced_body,
                command_id=command_id,
                expires_at=timestamp + lifetime,
                created_at=timestamp,
                _connection=connection,
            )

    @staticmethod
    def _command_matches_host(command: Command, host: DeviceRuntimeHost) -> bool:
        runtime_generation = command.payload.get("runtime_generation")
        return (
            command.owner_id == host.owner_id
            and command.target_kind == DEVICE_COMMAND_TARGET
            and command.target_id == host.device_id
            and str(command.payload.get("device_id") or "") == host.device_id
            and str(command.payload.get("runtime_session_id") or "")
            == host.runtime_session_id
            and isinstance(runtime_generation, int)
            and not isinstance(runtime_generation, bool)
            and runtime_generation == host.generation
        )

    def poll_commands(
        self,
        claims: DeviceCredentialClaims,
        *,
        runtime_session_id: str,
        generation: int,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> DeviceCommandPage:
        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or not 0 <= after_sequence <= MAX_SQLITE_INTEGER
        ):
            raise ValidationError("after_sequence must be a non-negative int64")
        claims = self._require_claims(claims)
        host = self.store.require_host_fence(
            claims,
            runtime_session_id=runtime_session_id,
            generation=generation,
            now=self.clock(),
        )

        def authorize(
            connection: sqlite3.Connection, timestamp: float
        ) -> None:
            self.store.require_authenticated_host_on(
                connection,
                claims,
                runtime_session_id=runtime_session_id,
                generation=generation,
                now=timestamp,
            )

        def reconcile_expired_commands(
            connection: sqlite3.Connection, timestamp: float
        ) -> None:
            self.store.reconcile_session_commands_on(
                connection,
                owner_id=claims.owner_id,
                device_id=claims.device_id,
                now=timestamp,
            )

        commands, next_sequence = (
            self.execution_store.command_queue.poll_and_mark_delivered(
                owner_id=claims.owner_id,
                target_kind=DEVICE_COMMAND_TARGET,
                target_id=claims.device_id,
                after_sequence=after_sequence,
                limit=limit,
                clock=self.clock,
                transaction_guard=authorize,
                after_expire=reconcile_expired_commands,
                command_filter=lambda command: self._command_matches_host(
                    command, host
                ),
            )
        )
        return DeviceCommandPage(tuple(commands), next_sequence)

    def ack_command(
        self,
        claims: DeviceCredentialClaims,
        *,
        runtime_session_id: str,
        generation: int,
        command_id: str,
        status: CommandStatus | str,
        ack_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Command:
        claims = self._require_claims(claims)
        host = self.store.require_host_fence(
            claims,
            runtime_session_id=runtime_session_id,
            generation=generation,
            now=self.clock(),
        )
        body = dict(payload or {})
        reserved = {"device_id", "runtime_session_id", "runtime_generation"}
        if reserved.intersection(body):
            raise ValidationError("command acknowledgement contains reserved fence fields")
        body = {**body, **self._command_fence(host)}
        body, _encoded = _json_object(
            body,
            "device command acknowledgement",
            maximum_bytes=MAX_COMMAND_PAYLOAD_BYTES,
        )
        rejection_error = str(body.get("error") or "")[:1000]

        def authorize(
            connection: sqlite3.Connection, timestamp: float
        ) -> None:
            self.store.require_authenticated_host_on(
                connection,
                claims,
                runtime_session_id=runtime_session_id,
                generation=generation,
                now=timestamp,
            )

        def reconcile_expired_commands(
            connection: sqlite3.Connection, timestamp: float
        ) -> None:
            self.store.reconcile_session_commands_on(
                connection,
                owner_id=claims.owner_id,
                device_id=claims.device_id,
                now=timestamp,
            )

        def authorize_command(command: Command) -> None:
            if not self._command_matches_host(command, host):
                raise DeviceRuntimeFenceError(
                    "command is outside the active device session"
                )

        def project_rejection(
            connection: sqlite3.Connection,
            command: Command,
            _acknowledged: Command,
            target: CommandStatus,
            timestamp: float,
        ) -> None:
            if target is not CommandStatus.REJECTED:
                return
            session_id = str(command.payload.get("session_id") or "")
            if session_id and command.type == "session.stop":
                expected_stop_id = hashlib.sha256(
                    f"session.stop\0{claims.owner_id}\0{session_id}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:32]
                if command.id != expected_stop_id:
                    return
                connection.execute(
                    """
                    UPDATE device_runtime_sessions
                    SET lifecycle = 'failed', last_error = ?,
                        active_request_id = '', active_turn_command_id = '',
                        active_turn_revision = 0,
                        revision = revision + 1, updated_at = ?
                    WHERE owner_id = ? AND session_id = ?
                      AND lifecycle = 'stopping'
                    """,
                    (
                        rejection_error or "session stop rejected",
                        timestamp,
                        claims.owner_id,
                        session_id,
                    ),
                )
                return
            if session_id and command.type == "session.start":
                reserved_revision = command.payload.get("session_revision")
                if not isinstance(reserved_revision, int) or isinstance(
                    reserved_revision, bool
                ):
                    return
                connection.execute(
                    """
                    UPDATE device_runtime_sessions
                    SET lifecycle = 'failed', last_error = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE owner_id = ? AND session_id = ?
                      AND lifecycle = 'starting'
                      AND revision = ? AND start_command_id = ?
                    """,
                    (
                        rejection_error or "session start rejected",
                        timestamp,
                        claims.owner_id,
                        session_id,
                        reserved_revision,
                        command.id,
                    ),
                )
                return
            if not session_id or command.type not in {"session.turn", "turn.start"}:
                return
            reserved_revision = command.payload.get("session_revision")
            if not isinstance(reserved_revision, int) or isinstance(
                reserved_revision, bool
            ):
                return
            connection.execute(
                """
                UPDATE device_runtime_sessions
                SET lifecycle = 'ready', revision = revision + 1,
                    active_turn_command_id = '', active_turn_revision = 0,
                    updated_at = ?, last_error = ?
                WHERE owner_id = ? AND session_id = ?
                  AND lifecycle = 'running' AND revision = ?
                  AND active_turn_command_id = ?
                  AND active_turn_revision = ?
                """,
                (
                    timestamp,
                    rejection_error or "turn start rejected",
                    claims.owner_id,
                    session_id,
                    reserved_revision,
                    command.id,
                    reserved_revision,
                ),
            )

        return self.execution_store.command_queue.ack(
            owner_id=claims.owner_id,
            command_id=command_id,
            status=status,
            ack_id=ack_id,
            payload=body,
            clock=self.clock,
            transaction_guard=authorize,
            after_expire=reconcile_expired_commands,
            command_guard=authorize_command,
            after_ack=project_rejection,
        )

    def _controlled_session(
        self, *, owner_id: str, session_id: str
    ) -> tuple[RuntimeSession, DeviceRuntimeHost]:
        self.store.reconcile_session_commands(
            owner_id=owner_id,
            session_id=session_id,
            clock=self.clock,
        )
        session = self.store.session(owner_id=owner_id, session_id=session_id)
        if session is None:
            raise DeviceRuntimeNotFound("runtime session does not exist")
        host = self._active_host(owner_id=owner_id, device_id=session.device_id)
        if (
            session.runtime_session_id != host.runtime_session_id
            or session.runtime_generation != host.generation
        ):
            raise DeviceRuntimeFenceError("runtime provider session fence is stale")
        return session, host

    def create_session(
        self,
        *,
        owner_id: str,
        device_id: str,
        provider: str,
        workspace: str,
        options: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        ttl: float = 5 * 60,
    ) -> RuntimeSession:
        provider = _text(provider, "provider", maximum=64)
        workspace = _text(workspace, "workspace", maximum=4096)
        options_body, _encoded = _json_object(
            options, "session options", maximum_bytes=MAX_COMMAND_PAYLOAD_BYTES // 2
        )
        lifetime = float(ttl)
        if not math.isfinite(lifetime) or not 1 <= lifetime <= 24 * 60 * 60:
            raise ValidationError("command ttl must be between 1 second and 24 hours")
        resolved_session_id = _identifier(session_id or new_id(), "session_id")
        self.store.reconcile_session_commands(
            owner_id=owner_id,
            device_id=device_id,
            clock=self.clock,
        )
        host = self._active_host(owner_id=owner_id, device_id=device_id)
        start_command_id = hashlib.sha256(
            f"session.start\0{owner_id}\0{resolved_session_id}".encode("utf-8")
        ).hexdigest()[:32]

        def enqueue_start(
            connection: sqlite3.Connection,
            session: RuntimeSession,
            timestamp: float,
        ) -> None:
            current = connection.execute(
                """
                SELECT * FROM device_runtime_hosts
                WHERE owner_id = ? AND device_id = ?
                """,
                (owner_id, device_id),
            ).fetchone()
            if current is None:
                raise DeviceRuntimeFenceError("device has no active runtime session")
            current_host = self.store._host_from_row(current)
            if (
                current_host.runtime_session_id != host.runtime_session_id
                or current_host.generation != host.generation
                or not current_host.online(timestamp)
            ):
                raise DeviceRuntimeFenceError(
                    "device runtime session changed while creating the session"
                )
            payload, _payload_json = _json_object(
                {
                    "session_id": resolved_session_id,
                    "provider": provider,
                    "workspace": workspace,
                    "options": options_body,
                    "session_revision": session.revision,
                    **self._command_fence(current_host),
                },
                "device command payload",
                maximum_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            )
            self.execution_store.command_queue.enqueue(
                owner_id=owner_id,
                target_kind=DEVICE_COMMAND_TARGET,
                target_id=device_id,
                command_type="session.start",
                command_id=start_command_id,
                payload=payload,
                created_at=timestamp,
                expires_at=timestamp + lifetime,
                _connection=connection,
            )

        session, created = self.store.create_session(
            owner_id=owner_id,
            device_id=device_id,
            provider=provider,
            workspace=workspace,
            runtime_session_id=host.runtime_session_id,
            runtime_generation=host.generation,
            attributes={"options": options_body},
            session_id=resolved_session_id,
            start_command_id=start_command_id,
            max_active_sessions=self.max_active_sessions,
            clock=self.clock,
            on_created=enqueue_start,
        )
        del created
        return session

    def get_session(self, *, owner_id: str, session_id: str) -> RuntimeSession:
        self.store.reconcile_session_commands(
            owner_id=owner_id,
            session_id=session_id,
            clock=self.clock,
        )
        session = self.store.session(owner_id=owner_id, session_id=session_id)
        if session is None:
            raise DeviceRuntimeNotFound("runtime session does not exist")
        return session

    def list_sessions(
        self,
        *,
        owner_id: str,
        device_id: str | None = None,
        limit: int = 200,
    ) -> list[RuntimeSession]:
        self.store.reconcile_session_commands(
            owner_id=owner_id,
            device_id=device_id,
            clock=self.clock,
        )
        return self.store.sessions(
            owner_id=owner_id, device_id=device_id, limit=limit
        )

    def send_turn(
        self,
        *,
        owner_id: str,
        session_id: str,
        input: str,
        turn_id: str | None = None,
        options: Mapping[str, Any] | None = None,
        ttl: float = 5 * 60,
    ) -> Command:
        session, host = self._controlled_session(
            owner_id=owner_id, session_id=session_id
        )
        value = str(input)
        if not value or len(value.encode("utf-8")) > 48 * 1024:
            raise ValidationError("turn input must contain 1..49152 UTF-8 bytes")
        lifetime = float(ttl)
        if not math.isfinite(lifetime) or not 1 <= lifetime <= 24 * 60 * 60:
            raise ValidationError("command ttl must be between 1 second and 24 hours")
        # Per-turn provider options (model, effort). Bounded and whitelisted so a
        # browser cannot smuggle arbitrary fields into the device command.
        turn_options: dict[str, str] = {}
        for name in ("model", "service_tier", "effort"):
            raw = (options or {}).get(name)
            if raw is None or raw == "":
                continue
            turn_options[name] = _require_text(raw, f"turn {name}", limit=128)
        resolved_turn_id = _identifier(turn_id or new_id(), "turn_id")
        command_id = hashlib.sha256(
            f"session.turn\0{owner_id}\0{session_id}\0{resolved_turn_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        existing = self.execution_store.command_queue.get(
            owner_id=owner_id,
            command_id=command_id,
            now=self.clock(),
        )
        if existing is not None:
            if (
                existing.type != "session.turn"
                or existing.target_kind != DEVICE_COMMAND_TARGET
                or existing.target_id != session.device_id
                or str(existing.payload.get("session_id") or "") != session_id
                or str(existing.payload.get("turn_id") or "") != resolved_turn_id
                or existing.payload.get("input") != value
                or dict(existing.payload.get("options") or {}) != turn_options
                or not self._command_matches_host(existing, host)
            ):
                raise CommandConflict(
                    "runtime turn command id is bound to different contents"
                )
            return existing
        if session.lifecycle != "ready":
            raise DeviceRuntimeConflict("runtime session cannot accept a new turn")

        queued_command: Command | None = None

        def enqueue_turn(
            connection: sqlite3.Connection,
            reserved: RuntimeSession,
            timestamp: float,
        ) -> None:
            nonlocal queued_command
            current = connection.execute(
                """
                SELECT * FROM device_runtime_hosts
                WHERE owner_id = ? AND device_id = ?
                """,
                (owner_id, session.device_id),
            ).fetchone()
            if current is None:
                raise DeviceRuntimeFenceError("device has no active runtime session")
            current_host = self.store._host_from_row(current)
            if (
                current_host.runtime_session_id != host.runtime_session_id
                or current_host.generation != host.generation
                or not current_host.online(timestamp)
            ):
                raise DeviceRuntimeFenceError(
                    "device runtime session changed while queuing the turn"
                )
            payload, _payload_json = _json_object(
                {
                    "session_id": session_id,
                    "turn_id": resolved_turn_id,
                    "input": value,
                    "session_revision": reserved.revision,
                    **({"options": turn_options} if turn_options else {}),
                    **self._command_fence(current_host),
                },
                "device command payload",
                maximum_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            )
            queued_command = self.execution_store.command_queue.enqueue(
                owner_id=owner_id,
                target_kind=DEVICE_COMMAND_TARGET,
                target_id=session.device_id,
                command_type="session.turn",
                command_id=command_id,
                payload=payload,
                created_at=timestamp,
                expires_at=timestamp + lifetime,
                _connection=connection,
            )

        _reserved = self.store.reserve_session_turn(
            owner_id=owner_id,
            session_id=session_id,
            command_id=command_id,
            expected_revision=session.revision,
            clock=self.clock,
            on_reserved=enqueue_turn,
        )
        assert queued_command is not None
        return queued_command

    def interrupt_session(
        self,
        *,
        owner_id: str,
        session_id: str,
        turn_id: str | None = None,
        ttl: float = 60,
    ) -> Command:
        session, host = self._controlled_session(
            owner_id=owner_id, session_id=session_id
        )
        if session.lifecycle not in {"ready", "running", "waiting"}:
            raise DeviceRuntimeConflict("runtime session cannot be interrupted")
        resolved_turn_id = _text(
            turn_id, "turn_id", maximum=255, optional=True
        )
        command_id = hashlib.sha256(
            f"session.interrupt\0{owner_id}\0{session_id}\0{resolved_turn_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        return self.enqueue_device_command(
            owner_id=owner_id,
            device_id=session.device_id,
            command_type="session.interrupt",
            command_id=command_id,
            ttl=ttl,
            payload={
                "session_id": session_id,
                "turn_id": resolved_turn_id,
                "session_revision": session.revision,
            },
            _expected_host=host,
        )

    def respond_to_request(
        self,
        *,
        owner_id: str,
        session_id: str,
        request_id: str,
        response: Mapping[str, Any],
        ttl: float = 60,
    ) -> Command:
        session, host = self._controlled_session(
            owner_id=owner_id, session_id=session_id
        )
        request_id = _identifier(request_id, "request_id")
        if session.lifecycle != "waiting" or session.active_request_id != request_id:
            raise DeviceRuntimeConflict("request is not active for this runtime session")
        response_body, _encoded = _json_object(
            response, "request response", maximum_bytes=48 * 1024
        )
        command_id = hashlib.sha256(
            f"session.respond\0{owner_id}\0{session_id}\0{request_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        return self.enqueue_device_command(
            owner_id=owner_id,
            device_id=session.device_id,
            command_type="session.respond",
            command_id=command_id,
            ttl=ttl,
            payload={
                "session_id": session_id,
                "request_id": request_id,
                "response": response_body,
                "session_revision": session.revision,
            },
            _expected_host=host,
        )

    def stop_session(
        self,
        *,
        owner_id: str,
        session_id: str,
        ttl: float = 60,
    ) -> Command:
        lifetime = float(ttl)
        if not math.isfinite(lifetime) or not 1 <= lifetime <= 24 * 60 * 60:
            raise ValidationError("command ttl must be between 1 second and 24 hours")
        self.store.reconcile_session_commands(
            owner_id=owner_id,
            session_id=session_id,
            clock=self.clock,
        )
        command_id = hashlib.sha256(
            f"session.stop\0{owner_id}\0{session_id}".encode("utf-8")
        ).hexdigest()[:32]

        def validate_existing(command: Command, session: RuntimeSession) -> Command:
            generation_value = command.payload.get("runtime_generation")
            revision_value = command.payload.get("session_revision")
            if (
                command.owner_id != owner_id
                or command.type != "session.stop"
                or str(command.payload.get("session_id") or "") != session_id
                or command.target_kind != DEVICE_COMMAND_TARGET
                or command.target_id != session.device_id
                or str(command.payload.get("device_id") or "")
                != session.device_id
                or str(command.payload.get("runtime_session_id") or "")
                != session.runtime_session_id
                or not isinstance(generation_value, int)
                or isinstance(generation_value, bool)
                or generation_value != session.runtime_generation
                or not isinstance(revision_value, int)
                or isinstance(revision_value, bool)
                or revision_value < 0
            ):
                raise DeviceRuntimeConflict(
                    "runtime stop command id is bound to different contents"
                )
            return command

        session = self.store.session(owner_id=owner_id, session_id=session_id)
        if session is None:
            raise DeviceRuntimeNotFound("runtime session does not exist")
        existing = self.execution_store.command_queue.get(
            owner_id=owner_id,
            command_id=command_id,
            now=self.clock(),
        )
        if existing is not None:
            validate_existing(existing, session)
            if session.lifecycle in {"stopping", "stopped", "failed", "lost"}:
                return existing
        elif session.lifecycle in {"stopping", "stopped", "failed", "lost"}:
            raise DeviceRuntimeConflict(
                "runtime session is terminal without a recoverable stop command"
            )

        session, host = self._controlled_session(
            owner_id=owner_id, session_id=session_id
        )
        queued_command: Command | None = None

        def enqueue_stop(
            connection: sqlite3.Connection,
            previous: RuntimeSession,
            _stopping: RuntimeSession,
            timestamp: float,
        ) -> None:
            nonlocal queued_command
            current = connection.execute(
                """
                SELECT * FROM device_runtime_hosts
                WHERE owner_id = ? AND device_id = ?
                """,
                (owner_id, previous.device_id),
            ).fetchone()
            if current is None:
                raise DeviceRuntimeFenceError(
                    "device has no active runtime session"
                )
            current_host = self.store._host_from_row(current)
            if (
                current_host.runtime_session_id != host.runtime_session_id
                or current_host.generation != host.generation
                or previous.runtime_session_id != current_host.runtime_session_id
                or previous.runtime_generation != current_host.generation
                or not current_host.online(timestamp)
            ):
                raise DeviceRuntimeFenceError(
                    "device runtime session changed while queuing the stop"
                )
            existing_row = connection.execute(
                """
                SELECT * FROM execution_commands
                WHERE owner_id = ? AND command_id = ?
                """,
                (owner_id, command_id),
            ).fetchone()
            if existing_row is not None:
                queued_command = validate_existing(
                    self.execution_store.command_queue._from_row(existing_row),
                    previous,
                )
                return
            payload, _payload_json = _json_object(
                {
                    "session_id": session_id,
                    "session_revision": previous.revision,
                    **self._command_fence(current_host),
                },
                "device command payload",
                maximum_bytes=MAX_COMMAND_PAYLOAD_BYTES,
            )
            queued_command = self.execution_store.command_queue.enqueue(
                owner_id=owner_id,
                target_kind=DEVICE_COMMAND_TARGET,
                target_id=previous.device_id,
                command_type="session.stop",
                command_id=command_id,
                payload=payload,
                created_at=timestamp,
                expires_at=timestamp + lifetime,
                _connection=connection,
            )

        try:
            _stopping, transitioned = self.store.mark_session_stopping(
                owner_id=owner_id,
                session_id=session_id,
                expected_revision=session.revision,
                clock=self.clock,
                on_stopping=enqueue_stop,
            )
        except DeviceRuntimeConflict:
            latest = self.store.session(owner_id=owner_id, session_id=session_id)
            concurrent = self.execution_store.command_queue.get(
                owner_id=owner_id,
                command_id=command_id,
                now=self.clock(),
            )
            if (
                latest is not None
                and concurrent is not None
                and latest.lifecycle in {"stopping", "stopped", "failed", "lost"}
            ):
                return validate_existing(concurrent, latest)
            raise
        if not transitioned:
            queued_command = self.execution_store.command_queue.get(
                owner_id=owner_id,
                command_id=command_id,
                now=self.clock(),
            )
            if queued_command is None:
                raise DeviceRuntimeConflict(
                    "runtime session is stopping without a recoverable stop command"
                )
            validate_existing(queued_command, _stopping)
        assert queued_command is not None
        return queued_command

    def ingest_session_events(
        self,
        claims: DeviceCredentialClaims,
        *,
        runtime_session_id: str,
        generation: int,
        session_id: str,
        events: Iterable[Mapping[str, Any]],
    ) -> list[EventIngestResult]:
        claims = self._require_claims(claims)
        return self.store.append_session_events(
            claims=claims,
            session_id=session_id,
            runtime_session_id=runtime_session_id,
            runtime_generation=generation,
            events=events,
            clock=self.clock,
        )

    def ingest_event_batch(
        self,
        claims: DeviceCredentialClaims,
        *,
        runtime_session_id: str,
        generation: int,
        groups: Mapping[str, Iterable[Mapping[str, Any]]],
    ) -> list[EventIngestResult]:
        """Classify a multi-session device delivery in one SQLite transaction."""

        claims = self._require_claims(claims)
        prepared = [
            (_identifier(session_id, "session_id"), list(events))
            for session_id, events in groups.items()
        ]
        event_count = sum(len(events) for _session_id, events in prepared)
        if not 1 <= event_count <= MAX_EVENT_BATCH_SIZE:
            raise ValidationError("runtime event batch must contain 1..100 events")
        timestamp = float(self.clock())
        results: list[EventIngestResult] = []
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for session_id, events in prepared:
                results.extend(
                    self.store.append_session_events(
                        claims=claims,
                        session_id=session_id,
                        runtime_session_id=runtime_session_id,
                        runtime_generation=generation,
                        events=events,
                        now=timestamp,
                        _connection=connection,
                    )
                )
        return results

    def session_events(
        self,
        *,
        owner_id: str,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[RuntimeSessionEvent]:
        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or not 0 <= after_sequence <= MAX_SQLITE_INTEGER
        ):
            raise ValidationError("after_sequence must be a non-negative int64")
        return self.store.session_events(
            owner_id=owner_id,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )


__all__ = [
    "CredentialGrant",
    "DeviceCommandPage",
    "DeviceCredentialClaims",
    "DeviceRuntimeAuthenticationError",
    "DeviceRuntimeConflict",
    "DeviceRuntimeError",
    "DeviceRuntimeFenceError",
    "DeviceRuntimeHost",
    "DeviceRuntimeNotFound",
    "DeviceRuntimeService",
    "DeviceRuntimeStore",
    "EnrollmentGrant",
    "EventIngestResult",
    "RuntimeSession",
    "RuntimeSessionEvent",
    "normalize_capabilities",
]
