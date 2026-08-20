from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import math
import os
import platform
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from .bridge import _validated_base_url
from .bridge_commands import (
    BridgeCommandJournal,
    BridgeCommandJournalError,
)
from .runtime_lock import RuntimeInstanceLock
from .runtime_adapters.base import (
    ApprovalDecision,
    RuntimeAdapter as TypedRuntimeAdapter,
    RuntimeAttachment,
    RuntimeEvent as TypedRuntimeEvent,
    RuntimeAdapterRegistry as TypedRuntimeAdapterRegistry,
    RuntimeSessionSpec,
    RuntimeTurnInput,
)


DEVICE_EVENT_SCHEMA = "agentserver.device-runtime-event/1"
DEVICE_RUNTIME_VERSION = "1"
DEVICE_CREDENTIAL_PREFIX = "asdc1"
MAX_PRIVATE_VALUE_BYTES = 4096
MAX_DEVICE_EVENT_BYTES = 64 * 1024
MAX_DEVICE_EVENT_BATCH = 100
MAX_DEVICE_EVENT_BATCH_BYTES = 224 * 1024
MAX_DEVICE_EVENT_SPOOL = 10_000
MAX_DEVICE_EVENT_DEAD_LETTERS = 10_000
DEFAULT_CREDENTIAL_ROTATION_WINDOW = 7 * 24 * 60 * 60

_PUBLIC_SECRET_MARKER = re.compile(r"\b(?:LEAK|SECRET|TOKEN|PASSWORD|CREDENTIAL)_[A-Z0-9_]+\b")
_PUBLIC_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|credential)\b\s*[:=]\s*[^\s,;]+"
)


def _public_text(value: object, *, limit: int = 16_384) -> str:
    text = str(value or "")
    text = _PUBLIC_SECRET_MARKER.sub("[redacted]", text)
    text = _PUBLIC_SECRET_ASSIGNMENT.sub(r"\1=[redacted]", text)
    return text[:limit]

SUPPORTED_COMMAND_TYPES = frozenset(
    {
        "runtime.probe",
        "session.start",
        "session.turn",
        "session.interrupt",
        "session.respond",
        "session.stop",
        # Compatibility aliases for commands issued before the device-runtime
        # control-plane vocabulary was frozen.
        "turn.start",
        "turn.interrupt",
        "approval.respond",
        "user_input.respond",
    }
)
# Only a read-only probe is intrinsically safe to replay after the process died
# between entering the handler and recording its result. Provider operations are
# deliberately quarantined as ``uncertain`` unless the control plane explicitly
# decides how to recover them.
IDEMPOTENT_COMMAND_TYPES = frozenset({"runtime.probe"})


class DeviceRuntimeError(RuntimeError):
    pass


class DeviceRuntimeProtocolError(DeviceRuntimeError):
    pass


class DeviceEventSpoolFull(DeviceRuntimeError):
    pass


class DeviceRuntimeCycleError(DeviceRuntimeError):
    def __init__(self, errors: Mapping[str, BaseException]) -> None:
        self.components = tuple(errors)
        # Exception strings must never include request headers, bodies, or
        # credentials. Component/type is enough for health and retry decisions.
        detail = ", ".join(
            f"{name}:{type(error).__name__}" for name, error in errors.items()
        )
        super().__init__(f"device runtime cycle degraded ({detail})")


def _require_text(value: object, label: str, *, limit: int = 255) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit or any(character in result for character in "\0\r\n"):
        raise ValueError(f"{label} must contain 1..{limit} safe characters")
    return result


def _device_credential_id(value: object) -> str:
    credential = str(value or "").strip()
    if (
        not credential
        or len(credential.encode("utf-8")) > MAX_PRIVATE_VALUE_BYTES
        or any(character.isspace() for character in credential)
    ):
        raise DeviceRuntimeProtocolError("stored device credential is invalid")
    parts = credential.split(".")
    if (
        len(parts) != 3
        or parts[0] != DEVICE_CREDENTIAL_PREFIX
        or not parts[2]
    ):
        raise DeviceRuntimeProtocolError("stored device credential is invalid")
    try:
        return _require_text(parts[1], "device credential id")
    except ValueError as error:
        raise DeviceRuntimeProtocolError(
            "stored device credential is invalid"
        ) from error


def _json_object(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        encoded = json.dumps(
            dict(value), separators=(",", ":"), sort_keys=True, allow_nan=False
        )
        normalized = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must contain valid JSON values") from error
    return normalized


def _ensure_private_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = resolved.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("runtime state path must be a real directory")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            raise ValueError("runtime state directory is not owned by this uid")
        resolved.chmod(0o700)
    return resolved


def _private_file_info(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"private runtime file is unavailable: {path}") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("private runtime path must be a regular file")
    if os.name != "nt" and (
        info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("private runtime file mode must be exactly 0600")
    return info


def load_private_text_file(path: Path | str) -> str:
    """Read a bounded owner-only secret/identity without following symlinks."""

    resolved = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as error:
        raise ValueError(f"cannot open private runtime file: {resolved}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("private runtime path must be a regular file")
        if os.name != "nt" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("private runtime file mode must be exactly 0600")
        encoded = os.read(descriptor, MAX_PRIVATE_VALUE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(encoded) > MAX_PRIVATE_VALUE_BYTES:
        raise ValueError("private runtime file is too large")
    try:
        value = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("private runtime file is not UTF-8") from error
    if not value or any(character.isspace() for character in value):
        raise ValueError("private runtime file contains an invalid value")
    return value


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        offset += os.write(descriptor, value[offset:])


def _atomic_write_private(path: Path, value: str) -> None:
    if not value or any(character.isspace() for character in value):
        raise ValueError("private runtime value is invalid")
    encoded = (value + "\n").encode("utf-8")
    if len(encoded) > MAX_PRIVATE_VALUE_BYTES:
        raise ValueError("private runtime value is too large")
    parent = _ensure_private_directory(path.parent)
    temporary = parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        _private_file_info(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _load_private_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(load_private_text_file(path))
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} file is invalid") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} file must contain an object")
    return dict(value)


def _remove_private_file(path: Path) -> None:
    _private_file_info(path)
    path.unlink()
    if os.name != "nt":
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _load_or_create_identity(path: Path) -> str:
    try:
        return _require_text(load_private_text_file(path), "instance_id")
    except ValueError:
        if path.exists() or path.is_symlink():
            raise
    value = uuid.uuid4().hex
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        _write_all(descriptor, (value + "\n").encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        path.chmod(0o600)
        return value
    except FileExistsError:
        return _require_text(load_private_text_file(path), "instance_id")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _increment_generation(path: Path) -> int:
    """Advance the durable host generation while the instance lock is held."""

    try:
        encoded = load_private_text_file(path)
    except ValueError:
        if path.exists() or path.is_symlink():
            raise
        current = 0
    else:
        if not encoded.isascii() or not encoded.isdecimal():
            raise ValueError("runtime generation file is invalid")
        current = int(encoded)
        if current < 0 or current >= (2**63 - 1):
            raise ValueError("runtime generation is outside the supported range")
    generation = current + 1
    _atomic_write_private(path, str(generation))
    return generation


class PrivateCredentialFile:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    @property
    def exists(self) -> bool:
        return self.path.exists() and not self.path.is_symlink()

    def load(self) -> str:
        with self._lock:
            return load_private_text_file(self.path)

    def replace(self, credential: str) -> str:
        with self._lock:
            _atomic_write_private(self.path, credential)
            return self.load()


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    occurred_at: float | None = None


@dataclass(frozen=True)
class AdapterContext:
    device_id: str
    instance_id: str
    boot_id: str
    provider: str
    session_id: str | None
    workspace: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


AdapterEventSink = Callable[
    [RuntimeEvent | Mapping[str, Any]], Awaitable[dict[str, Any]]
]


@runtime_checkable
class RuntimeAdapter(Protocol):
    def probe(self, payload: Mapping[str, Any]) -> object | Awaitable[object]: ...

    def start_session(self, payload: Mapping[str, Any]) -> object | Awaitable[object]: ...

    def stop_session(self, payload: Mapping[str, Any]) -> object | Awaitable[object]: ...

    def start_turn(self, payload: Mapping[str, Any]) -> object | Awaitable[object]: ...

    def interrupt_turn(self, payload: Mapping[str, Any]) -> object | Awaitable[object]: ...

    def respond_to_approval(
        self, payload: Mapping[str, Any]
    ) -> object | Awaitable[object]: ...

    def respond_to_user_input(
        self, payload: Mapping[str, Any]
    ) -> object | Awaitable[object]: ...

    def close(self) -> object | Awaitable[object]: ...


class AdapterFactory(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> object | Awaitable[object]: ...


class DeviceRuntimeClient(Protocol):
    async def enroll(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def heartbeat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def commands(
        self,
        *,
        after_sequence: int,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> Mapping[str, Any]: ...

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
    ) -> Mapping[str, Any]: ...

    async def send_events(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> Mapping[str, Any]: ...

    async def rotate_credential(
        self,
        payload: Mapping[str, Any],
        *,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


class DeviceRuntimeHTTPClient:
    """Small HTTP boundary for the device-runtime protocol.

    Paths live here so the server contract can evolve without leaking URL
    construction throughout the Host. Secrets are supplied only as request
    headers/bodies and are never included in raised error text.
    """

    API_ROOT = "/api/device-runtime/v1"
    ENROLL_PATH = f"{API_ROOT}/enroll"
    HEARTBEAT_PATH = f"{API_ROOT}/heartbeat"
    COMMANDS_PATH = f"{API_ROOT}/commands"
    EVENTS_PATH = f"{API_ROOT}/events:batch"
    ROTATE_PATH = f"{API_ROOT}/credential:rotate"

    def __init__(
        self,
        base_url: str,
        credential_provider: Callable[[], str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = _validated_base_url(base_url)
        self.credential_provider = credential_provider
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=max(0.1, float(timeout)),
            trust_env=False,
            transport=transport,
        )

    @staticmethod
    def _response_payload(response: httpx.Response, operation: str) -> Mapping[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise DeviceRuntimeProtocolError(
                f"device runtime {operation} failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise DeviceRuntimeProtocolError(
                f"device runtime {operation} response is not JSON"
            ) from error
        if not isinstance(payload, Mapping):
            raise DeviceRuntimeProtocolError(
                f"device runtime {operation} response must be an object"
            )
        return payload

    def _authorization(self) -> dict[str, str]:
        credential = self.credential_provider()
        if (
            not credential
            or len(credential.encode("utf-8")) > MAX_PRIVATE_VALUE_BYTES
            or any(character.isspace() for character in credential)
        ):
            raise DeviceRuntimeProtocolError("stored device credential is invalid")
        return {"Authorization": f"Bearer {credential}"}

    @staticmethod
    def _runtime_identity(
        *,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or not 1 <= generation < 2**63
        ):
            raise ValueError("runtime generation must be a positive int64")
        return {
            "device_id": _require_text(device_id, "device_id"),
            "instance_id": _require_text(instance_id, "instance_id"),
            "boot_id": _require_text(boot_id, "boot_id"),
            "runtime_session_id": _require_text(
                runtime_session_id, "runtime_session_id"
            ),
            "generation": generation,
        }

    async def enroll(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = await self._client.post(self.ENROLL_PATH, json=dict(payload))
        return self._response_payload(response, "enroll")

    async def heartbeat(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = await self._client.post(
            self.HEARTBEAT_PATH,
            headers=self._authorization(),
            json=dict(payload),
        )
        return self._response_payload(response, "heartbeat")

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
        identity = self._runtime_identity(
            device_id=device_id,
            instance_id=instance_id,
            boot_id=boot_id,
            runtime_session_id=runtime_session_id,
            generation=generation,
        )
        response = await self._client.get(
            self.COMMANDS_PATH,
            headers=self._authorization(),
            params={
                "after_sequence": int(after_sequence),
                **identity,
            },
        )
        return self._response_payload(response, "commands")

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
        identifier = _require_text(command_id, "command_id")
        identity = self._runtime_identity(
            device_id=device_id,
            instance_id=instance_id,
            boot_id=boot_id,
            runtime_session_id=runtime_session_id,
            generation=generation,
        )
        response = await self._client.post(
            f"{self.COMMANDS_PATH}/{identifier}/ack",
            headers=self._authorization(),
            json={**dict(payload), **identity},
        )
        return self._response_payload(response, "command ACK")

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
        identity = self._runtime_identity(
            device_id=device_id,
            instance_id=instance_id,
            boot_id=boot_id,
            runtime_session_id=runtime_session_id,
            generation=generation,
        )
        response = await self._client.post(
            self.EVENTS_PATH,
            headers=self._authorization(),
            json={**identity, "events": [dict(event) for event in events]},
        )
        return self._response_payload(response, "events")

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
        identity = self._runtime_identity(
            device_id=device_id,
            instance_id=instance_id,
            boot_id=boot_id,
            runtime_session_id=runtime_session_id,
            generation=generation,
        )
        response = await self._client.post(
            self.ROTATE_PATH,
            headers=self._authorization(),
            json={**dict(payload), **identity},
        )
        return self._response_payload(response, "credential rotation")

    async def close(self) -> None:
        await self._client.aclose()


class DeviceEventSpool:
    """Durable at-least-once queue shared by every adapter on one device."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        max_events: int = MAX_DEVICE_EVENT_SPOOL,
        max_dead_letters: int = MAX_DEVICE_EVENT_DEAD_LETTERS,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        _ensure_private_directory(self.database_path.parent)
        descriptor = os.open(
            self.database_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        os.close(descriptor)
        if os.name != "nt":
            self.database_path.chmod(0o600)
        self.max_events = max(32, int(max_events))
        if (
            not isinstance(max_dead_letters, int)
            or isinstance(max_dead_letters, bool)
            or max_dead_letters < 1
        ):
            raise ValueError("max_dead_letters must be a positive integer")
        self.max_dead_letters = max_dead_letters
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS device_event_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_runtime_events (
                    producer_seq INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    envelope_json TEXT NOT NULL,
                    attempted INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS device_runtime_events_created
                ON device_runtime_events(created_at);
                CREATE TABLE IF NOT EXISTS device_runtime_event_dead_letters (
                    dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    producer_seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    quarantined_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS device_runtime_dead_letters_recorded
                ON device_runtime_event_dead_letters(quarantined_at, dead_letter_id);
                """
            )
            dead_letter_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(device_runtime_event_dead_letters)"
                ).fetchall()
            }
            if "error_code" not in dead_letter_columns:
                connection.execute(
                    "ALTER TABLE device_runtime_event_dead_letters "
                    "ADD COLUMN error_code TEXT NOT NULL DEFAULT ''"
                )
            if connection.execute(
                "SELECT 1 FROM device_event_metadata WHERE key = 'epoch'"
            ).fetchone() is None:
                connection.execute(
                    "INSERT INTO device_event_metadata(key, value) VALUES ('epoch', ?)",
                    (uuid.uuid4().hex,),
                )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            self._trim_dead_letters(connection)
        self._harden_files()

    def _harden_files(self) -> None:
        if os.name == "nt":
            return
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            with contextlib.suppress(FileNotFoundError):
                path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        self._harden_files()
        return connection

    @property
    def epoch(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM device_event_metadata WHERE key = 'epoch'"
            ).fetchone()
        if row is None:
            raise DeviceRuntimeError("device event epoch is unavailable")
        return str(row["value"])

    def enqueue(
        self,
        event: RuntimeEvent,
        *,
        device_id: str,
        instance_id: str,
        boot_id: str,
        runtime_session_id: str,
        generation: int,
    ) -> dict[str, Any]:
        event_type = _require_text(event.type, "runtime event type", limit=120)
        payload = _json_object(event.payload, "runtime event payload")
        occurred_at = time.time() if event.occurred_at is None else event.occurred_at
        if (
            isinstance(occurred_at, bool)
            or not isinstance(occurred_at, (int, float))
            or not math.isfinite(float(occurred_at))
        ):
            raise ValueError("runtime event occurred_at must be finite")
        session_id = (
            _require_text(event.session_id, "session_id")
            if event.session_id is not None
            else None
        )
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or not 1 <= generation < 2**63
        ):
            raise ValueError("runtime generation must be a positive int64")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM device_runtime_events"
                ).fetchone()[0]
            )
            if count >= self.max_events:
                raise DeviceEventSpoolFull(
                    "device event spool is full; no runtime event was discarded"
                )
            row = connection.execute(
                "SELECT value FROM device_event_metadata WHERE key = 'last_sequence'"
            ).fetchone()
            sequence = int(row["value"]) + 1 if row is not None else 1
            if sequence >= 2**63:
                raise DeviceEventSpoolFull(
                    "device event sequence exhausted its int64 range"
                )
            epoch_row = connection.execute(
                "SELECT value FROM device_event_metadata WHERE key = 'epoch'"
            ).fetchone()
            if epoch_row is None:
                raise DeviceRuntimeError("device event epoch is unavailable")
            envelope = {
                "schema": DEVICE_EVENT_SCHEMA,
                "event_id": uuid.uuid4().hex,
                "type": event_type,
                "device_id": _require_text(device_id, "device_id"),
                "instance_id": _require_text(instance_id, "instance_id"),
                "boot_id": _require_text(boot_id, "boot_id"),
                "runtime_session_id": _require_text(
                    runtime_session_id, "runtime_session_id"
                ),
                "generation": generation,
                "session_id": session_id,
                "producer": {
                    "epoch": str(epoch_row["value"]),
                    "seq": sequence,
                },
                "occurred_at": float(occurred_at),
                "payload": payload,
            }
            encoded = json.dumps(
                envelope, separators=(",", ":"), sort_keys=True, allow_nan=False
            )
            if len(encoded.encode("utf-8")) > MAX_DEVICE_EVENT_BYTES:
                raise ValueError("device runtime event exceeds 64 KiB")
            connection.execute(
                """
                INSERT INTO device_runtime_events(
                    producer_seq, event_id, envelope_json, attempted, created_at
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (sequence, envelope["event_id"], encoded, time.time()),
            )
            connection.execute(
                """
                INSERT INTO device_event_metadata(key, value)
                VALUES ('last_sequence', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(sequence),),
            )
        self._harden_files()
        return envelope

    def pending(self, *, limit: int = MAX_DEVICE_EVENT_BATCH) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), MAX_DEVICE_EVENT_BATCH))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM device_runtime_events "
                "ORDER BY producer_seq LIMIT ?",
                (bounded,),
            ).fetchall()
        return [json.loads(str(row["envelope_json"])) for row in rows]

    def delivery_batch(
        self,
        *,
        limit: int = MAX_DEVICE_EVENT_BATCH,
        maximum_bytes: int = MAX_DEVICE_EVENT_BATCH_BYTES,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), MAX_DEVICE_EVENT_BATCH))
        byte_limit = max(MAX_DEVICE_EVENT_BYTES, int(maximum_bytes))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT producer_seq, envelope_json FROM device_runtime_events "
                "ORDER BY producer_seq LIMIT ?",
                (bounded,),
            ).fetchall()
            selected: list[sqlite3.Row] = []
            encoded_size = 2
            for row in rows:
                item_size = len(str(row["envelope_json"]).encode("utf-8"))
                projected = encoded_size + item_size + (1 if selected else 0)
                if selected and projected > byte_limit:
                    break
                if projected > byte_limit:
                    raise DeviceRuntimeProtocolError(
                        "one queued runtime event exceeds the delivery batch limit"
                    )
                selected.append(row)
                encoded_size = projected
            if selected:
                values = [int(row["producer_seq"]) for row in selected]
                placeholders = ",".join("?" for _ in values)
                connection.execute(
                    f"UPDATE device_runtime_events SET attempted = 1 "
                    f"WHERE producer_seq IN ({placeholders})",
                    values,
                )
        return [json.loads(str(row["envelope_json"])) for row in selected]

    def _trim_dead_letters(self, connection: sqlite3.Connection) -> int:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM device_runtime_event_dead_letters"
            ).fetchone()[0]
        )
        excess = max(0, count - self.max_dead_letters)
        if excess:
            connection.execute(
                """
                DELETE FROM device_runtime_event_dead_letters
                WHERE dead_letter_id IN (
                    SELECT dead_letter_id
                    FROM device_runtime_event_dead_letters
                    ORDER BY dead_letter_id
                    LIMIT ?
                )
                """,
                (excess,),
            )
        return excess

    def quarantine_stale_generation(self, *, now: float | None = None) -> int:
        """Atomically move envelopes that cannot pass the new Host fence."""

        quarantined_at = time.time() if now is None else float(now)
        if not math.isfinite(quarantined_at):
            raise ValueError("quarantined_at must be finite")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM device_runtime_events"
                ).fetchone()[0]
            )
            if count:
                connection.execute(
                    """
                    INSERT INTO device_runtime_event_dead_letters(
                        producer_seq, event_id, envelope_json,
                        reason, quarantined_at
                    )
                    SELECT producer_seq, event_id, envelope_json,
                           'stale_generation', ?
                    FROM device_runtime_events
                    ORDER BY producer_seq
                    """,
                    (quarantined_at,),
                )
                connection.execute("DELETE FROM device_runtime_events")
            self._trim_dead_letters(connection)
        self._harden_files()
        return count

    @property
    def dead_letter_count(self) -> int:
        with self._lock, self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM device_runtime_event_dead_letters"
                ).fetchone()[0]
            )

    def dead_letters(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("dead-letter limit must be between 1 and 1000")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT dead_letter_id, producer_seq, event_id, envelope_json,
                       reason, error_code, quarantined_at
                FROM device_runtime_event_dead_letters
                ORDER BY dead_letter_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "dead_letter_id": int(row["dead_letter_id"]),
                "producer_seq": int(row["producer_seq"]),
                "event_id": str(row["event_id"]),
                "envelope": json.loads(str(row["envelope_json"])),
                "reason": str(row["reason"]),
                "error_code": str(row["error_code"]),
                "quarantined_at": float(row["quarantined_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _result_code(value: object) -> str:
        if not isinstance(value, str):
            raise DeviceRuntimeProtocolError(
                "device event results are malformed"
            )
        try:
            result = _require_text(value, "event result code", limit=120)
        except ValueError as error:
            raise DeviceRuntimeProtocolError(
                "device event results are malformed"
            ) from error
        if not all(
            character.isascii()
            and (character.isalnum() or character in "._-")
            for character in result
        ):
            raise DeviceRuntimeProtocolError(
                "device event results are malformed"
            )
        return result

    @staticmethod
    def _delivery_identity(event: Mapping[str, Any]) -> tuple[str, int]:
        event_id = event.get("event_id")
        producer = event.get("producer")
        producer_seq = producer.get("seq") if isinstance(producer, Mapping) else None
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 255
            or not isinstance(producer_seq, int)
            or isinstance(producer_seq, bool)
            or not 0 <= producer_seq < 2**63
        ):
            raise DeviceRuntimeProtocolError(
                "queued device event identity is invalid"
            )
        return event_id, producer_seq

    def settle_delivery(
        self,
        events: Sequence[Mapping[str, Any]],
        results: object,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically settle only events explicitly classified by the server."""

        expected: dict[tuple[str, int], Mapping[str, Any]] = {}
        for event in events:
            if not isinstance(event, Mapping):
                raise DeviceRuntimeProtocolError(
                    "queued device event batch is invalid"
                )
            identity = self._delivery_identity(event)
            if identity in expected:
                raise DeviceRuntimeProtocolError(
                    "queued device event batch contains duplicates"
                )
            expected[identity] = event
        if not expected:
            return {
                "accepted": 0,
                "duplicate": 0,
                "rejected": 0,
                "retryable": 0,
                "missing": 0,
                "permanent_rejections": [],
            }
        if not isinstance(results, list):
            raise DeviceRuntimeProtocolError(
                "device event response results must be an array"
            )

        actions: dict[tuple[str, int], tuple[str, str, str]] = {}
        counts = {
            "accepted": 0,
            "duplicate": 0,
            "rejected": 0,
            "retryable": 0,
            "missing": 0,
            "permanent_rejections": [],
        }
        for raw in results:
            if not isinstance(raw, Mapping):
                raise DeviceRuntimeProtocolError(
                    "device event results are malformed"
                )
            event_id = raw.get("event_id")
            producer_seq = raw.get("producer_seq")
            if (
                not isinstance(event_id, str)
                or not event_id
                or len(event_id) > 255
                or not isinstance(producer_seq, int)
                or isinstance(producer_seq, bool)
                or not 0 <= producer_seq < 2**63
            ):
                raise DeviceRuntimeProtocolError(
                    "device event results are malformed"
                )
            identity = (event_id, producer_seq)
            if identity not in expected or identity in actions:
                raise DeviceRuntimeProtocolError(
                    "device event results do not match the delivered batch"
                )
            status = raw.get("status")
            if status in {"accepted", "duplicate"}:
                if raw.get("permanent", False) is not False:
                    raise DeviceRuntimeProtocolError(
                        "device event results are malformed"
                    )
                actions[identity] = ("delete", "", "")
                counts[str(status)] += 1
                continue
            if status != "rejected" or not isinstance(raw.get("permanent"), bool):
                raise DeviceRuntimeProtocolError(
                    "device event results are malformed"
                )
            error_code = self._result_code(raw.get("error_code"))
            reason = self._result_code(raw.get("reason") or error_code)
            if raw["permanent"]:
                actions[identity] = ("dead_letter", error_code, reason)
                counts["rejected"] += 1
                counts["permanent_rejections"].append(
                    {
                        "event_id": event_id,
                        "producer_seq": producer_seq,
                        "session_id": expected[identity].get("session_id"),
                        "error_code": error_code,
                        "reason": reason,
                    }
                )
            else:
                actions[identity] = ("retain", error_code, reason)
                counts["retryable"] += 1
        counts["missing"] = len(expected) - len(actions)

        settled = {
            identity: action
            for identity, action in actions.items()
            if action[0] in {"delete", "dead_letter"}
        }
        if not settled:
            return counts
        quarantined_at = time.time() if now is None else float(now)
        if not math.isfinite(quarantined_at):
            raise ValueError("settled event timestamp must be finite")
        sequences = [identity[1] for identity in settled]
        placeholders = ",".join("?" for _ in sequences)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT producer_seq, event_id, envelope_json "
                f"FROM device_runtime_events WHERE producer_seq IN ({placeholders})",
                sequences,
            ).fetchall()
            stored = {
                (str(row["event_id"]), int(row["producer_seq"])): row
                for row in rows
            }
            if set(stored) != set(settled):
                raise DeviceRuntimeProtocolError(
                    "device event spool changed during settlement"
                )
            for identity, (_action, error_code, reason) in settled.items():
                if _action != "dead_letter":
                    continue
                row = stored[identity]
                connection.execute(
                    """
                    INSERT INTO device_runtime_event_dead_letters(
                        producer_seq, event_id, envelope_json,
                        reason, error_code, quarantined_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity[1],
                        identity[0],
                        str(row["envelope_json"]),
                        reason,
                        error_code,
                        quarantined_at,
                    ),
                )
            connection.execute(
                f"DELETE FROM device_runtime_events "
                f"WHERE producer_seq IN ({placeholders})",
                sequences,
            )
            self._trim_dead_letters(connection)
        self._harden_files()
        return counts

    def acknowledge(
        self,
        accepted_through_seq: int,
        *,
        missing_ranges: Sequence[Sequence[int]] = (),
    ) -> int:
        if (
            not isinstance(accepted_through_seq, int)
            or isinstance(accepted_through_seq, bool)
            or not 0 <= accepted_through_seq < 2**63
        ):
            raise ValueError("accepted event cursor must be a non-negative int64")
        missing: set[int] = set()
        for value in missing_ranges:
            if len(value) != 2:
                raise ValueError("missing event range must contain two integers")
            start, end = value
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end < start
                or end >= 2**63
                or end - start > MAX_DEVICE_EVENT_SPOOL
            ):
                raise ValueError("missing event range is invalid")
            missing.update(range(start, end + 1))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT producer_seq FROM device_runtime_events "
                "WHERE producer_seq <= ?",
                (accepted_through_seq,),
            ).fetchall()
            acknowledged = [
                int(row["producer_seq"])
                for row in rows
                if int(row["producer_seq"]) not in missing
            ]
            if acknowledged:
                placeholders = ",".join("?" for _ in acknowledged)
                connection.execute(
                    f"DELETE FROM device_runtime_events "
                    f"WHERE producer_seq IN ({placeholders})",
                    acknowledged,
                )
        return len(acknowledged)

    def __len__(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM device_runtime_events"
                ).fetchone()[0]
            )


@dataclass
class _SessionEventGate:
    accepting: bool = True
    terminal_event_spooled: bool = False


@dataclass
class _SessionHandle:
    session_id: str
    provider: str
    adapter: object
    started_at: float
    event_gate: _SessionEventGate
    typed: bool = False
    event_task: asyncio.Task[None] | None = None
    # The done callback consumes task.exception() immediately so a failed
    # provider stream can never become an un-retrieved background exception.
    # Keeping only the exception class (rather than its message/traceback)
    # also avoids retaining provider internals or leaking them into telemetry.
    event_task_outcome: str | None = None
    event_task_expected_shutdown: bool = False
    event_pump_failure_enqueued: bool = False


async def _await_result(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


class DeviceRuntimeHost:
    """One persistent, outbound-only runtime host for a managed device."""

    def __init__(
        self,
        *,
        device_id: str,
        base_url: str,
        state_dir: Path | str,
        adapter_registry: (
            Mapping[str, AdapterFactory] | TypedRuntimeAdapterRegistry | None
        ) = None,
        client: DeviceRuntimeClient | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        heartbeat_interval: float = 10.0,
        poll_interval: float = 1.0,
        initial_backoff: float = 0.25,
        max_backoff: float = 30.0,
        event_spool_limit: int = MAX_DEVICE_EVENT_SPOOL,
        event_dead_letter_limit: int = MAX_DEVICE_EVENT_DEAD_LETTERS,
        credential_rotation_window: float = DEFAULT_CREDENTIAL_ROTATION_WINDOW,
    ) -> None:
        self.device_id = _require_text(device_id, "device_id")
        self.base_url = _validated_base_url(base_url)
        self.state_dir = _ensure_private_directory(Path(state_dir))
        self._instance_lock = RuntimeInstanceLock(self.state_dir / "host.instance.lock")
        self._lock_acquired = False
        self._closed = False
        # Acquiring during construction makes generation allocation and every
        # subsequent side-effect owned by exactly one Host. Acquiring only in
        # run() would allow enroll/rotate/run_once users to create split brains.
        self._instance_lock.acquire()
        self._lock_acquired = True
        try:
            self.instance_path = self.state_dir / "instance_id"
            self.generation_path = self.state_dir / "generation"
            self.credential_file = PrivateCredentialFile(
                self.state_dir / "device.credential"
            )
            self.rotation_request_path = self.state_dir / "credential.rotation"
            self.instance_id = _load_or_create_identity(self.instance_path)
            self.generation = _increment_generation(self.generation_path)
            self.boot_id = uuid.uuid4().hex
            self.runtime_session_id = self.boot_id
            self.database_path = self.state_dir / "runtime.db"
            descriptor = os.open(
                self.database_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.close(descriptor)
            if os.name != "nt":
                self.database_path.chmod(0o600)
            self.command_journal = BridgeCommandJournal(self.database_path)
            stale_commands = (
                self.command_journal.quarantine_stale_runtime_fence(
                    device_id=self.device_id,
                    runtime_session_id=self.runtime_session_id,
                    generation=self.generation,
                )
            )
            self.stale_generation_commands = stale_commands["commands"]
            self.stale_generation_command_acks = stale_commands["acks"]
            self.event_spool = DeviceEventSpool(
                self.database_path,
                max_events=event_spool_limit,
                max_dead_letters=event_dead_letter_limit,
            )
            # Construction allocated a new generation and runtime_session_id.
            # The server deliberately rejects envelopes from the old fence;
            # retaining them in the live queue would poison the FIFO head
            # forever, so preserve them in a bounded diagnostic quarantine.
            self.stale_generation_events = (
                self.event_spool.quarantine_stale_generation()
            )
            self.adapter_registry = self._normalize_adapter_registry(adapter_registry)
            self.client: DeviceRuntimeClient = client or DeviceRuntimeHTTPClient(
                self.base_url,
                self.credential_file.load,
                transport=http_transport,
            )
            self.heartbeat_interval = max(1.0, float(heartbeat_interval))
            self.poll_interval = max(0.1, float(poll_interval))
            self.initial_backoff = max(0.05, float(initial_backoff))
            self.max_backoff = max(self.initial_backoff, float(max_backoff))
            self.credential_rotation_window = float(credential_rotation_window)
            if (
                not math.isfinite(self.credential_rotation_window)
                or not 60 <= self.credential_rotation_window <= 90 * 24 * 60 * 60
            ):
                raise ValueError(
                    "credential_rotation_window must be between 60 seconds and 90 days"
                )
            self._sessions: dict[str, _SessionHandle] = {}
            self._session_lock = asyncio.Lock()
            self._command_lock = asyncio.Lock()
            self._event_lock = asyncio.Lock()
            self._stop_event = asyncio.Event()
            self._server_clock_anchor: tuple[float, float] | None = None
            self._next_heartbeat_at = 0.0
        except BaseException:
            self._instance_lock.release()
            self._lock_acquired = False
            raise

    @staticmethod
    def _normalize_adapter_registry(
        registry: Mapping[str, AdapterFactory] | TypedRuntimeAdapterRegistry | None,
    ) -> dict[str, AdapterFactory]:
        if registry is None:
            return {}
        if isinstance(registry, Mapping):
            return {
                _require_text(provider, "provider", limit=80): factory
                for provider, factory in registry.items()
            }
        if not isinstance(registry, TypedRuntimeAdapterRegistry):
            raise TypeError("adapter_registry must be a mapping or RuntimeAdapterRegistry")
        providers = registry.providers
        create = registry.create
        normalized: dict[str, AdapterFactory] = {}
        for raw_provider in providers:
            provider = _require_text(raw_provider, "provider", limit=80)

            def factory(*, _provider: str = provider) -> object:
                return create(_provider)

            normalized[provider] = factory
        return normalized

    def _runtime_identity(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "runtime_session_id": self.runtime_session_id,
            "generation": self.generation,
        }

    @property
    def enrolled(self) -> bool:
        return self.credential_file.exists

    @property
    def sessions(self) -> dict[str, dict[str, Any]]:
        return {
            identifier: {
                "session_id": handle.session_id,
                "provider": handle.provider,
                "started_at": handle.started_at,
            }
            for identifier, handle in self._sessions.items()
        }

    async def enroll_from_file(
        self,
        enrollment_token_file: Path | str,
        *,
        metadata: Mapping[str, Any] | None = None,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        had_existing = self.enrolled
        if had_existing and not replace_existing:
            # Validate the existing credential rather than silently accepting a
            # damaged or permission-loosened file.
            self.credential_file.load()
            return {
                "enrolled": True,
                "already_enrolled": True,
                "device_id": self.device_id,
                "instance_id": self.instance_id,
            }
        enrollment_token = load_private_text_file(enrollment_token_file)
        payload = await self.client.enroll(
            {
                "schema": "agentserver.device-enrollment/1",
                "device_id": self.device_id,
                "instance_id": self.instance_id,
                "boot_id": self.boot_id,
                "enrollment_token": enrollment_token,
                "metadata": _json_object(metadata, "device metadata"),
            }
        )
        credential = payload.get("credential") or payload.get("device_credential")
        value = str(credential or "")
        if (
            not value
            or len(value.encode("utf-8")) > MAX_PRIVATE_VALUE_BYTES
            or any(character.isspace() for character in value)
        ):
            raise DeviceRuntimeProtocolError(
                "device enrollment response has no valid credential"
            )
        self.credential_file.replace(value)
        return {
            "enrolled": True,
            "already_enrolled": False,
            "replaced_existing": had_existing,
            "device_id": self.device_id,
            "instance_id": self.instance_id,
        }

    async def rotate_credential(
        self, *, claim_current_fence: bool = True
    ) -> dict[str, Any]:
        current_credential = self.credential_file.load()
        current_credential_id = _device_credential_id(current_credential)
        pending: dict[str, Any] | None = None
        if self.rotation_request_path.exists() or self.rotation_request_path.is_symlink():
            pending = _load_private_json(
                self.rotation_request_path,
                "credential rotation",
            )
        if pending is None:
            # Claim this generation before persisting the request fence. If the
            # HTTP response is lost, a later CLI process reuses the exact saved
            # request id and original fence instead of attempting a new rotate.
            if claim_current_fence:
                await self.heartbeat()
            pending = {
                "request_id": uuid.uuid4().hex,
                "old_credential_id": current_credential_id,
                "runtime_session_id": self.runtime_session_id,
                "generation": self.generation,
            }
            _atomic_write_private(
                self.rotation_request_path,
                json.dumps(pending, separators=(",", ":"), sort_keys=True),
            )
        pending_old_credential_id = pending.get("old_credential_id")
        if pending_old_credential_id is not None:
            pending_old_credential_id = _require_text(
                pending_old_credential_id, "rotation old credential id"
            )
            if current_credential_id != pending_old_credential_id:
                # The previous process durably installed the replacement and
                # crashed before deleting the marker. Reissuing the request
                # with the replacement token would create an unintended
                # second rotation because token derivation keys on the bearer.
                _remove_private_file(self.rotation_request_path)
                return {
                    "rotated": True,
                    "recovered_after_local_commit": True,
                    "device_id": self.device_id,
                    "instance_id": self.instance_id,
                }
        request_id = _require_text(
            pending.get("request_id"), "rotation request id"
        )
        fenced_session_id = _require_text(
            pending.get("runtime_session_id"), "rotation runtime_session_id"
        )
        fenced_generation = pending.get("generation")
        if (
            not isinstance(fenced_generation, int)
            or isinstance(fenced_generation, bool)
            or fenced_generation < 1
            or fenced_generation >= 2**63
        ):
            raise DeviceRuntimeProtocolError(
                "stored credential rotation generation is invalid"
            )
        identity = self._runtime_identity()
        identity["runtime_session_id"] = fenced_session_id
        identity["generation"] = fenced_generation
        payload = await self.client.rotate_credential(
            {"request_id": request_id},
            **identity,
        )
        replacement = payload.get("credential") or payload.get("device_credential")
        value = str(replacement or "")
        if (
            not value
            or len(value.encode("utf-8")) > MAX_PRIVATE_VALUE_BYTES
            or any(character.isspace() for character in value)
        ):
            raise DeviceRuntimeProtocolError(
                "credential rotation response has no valid credential"
            )
        _device_credential_id(value)
        self.credential_file.replace(value)
        _remove_private_file(self.rotation_request_path)
        return {
            "rotated": True,
            "device_id": self.device_id,
            "instance_id": self.instance_id,
        }

    def _credential_rotation_due(self, heartbeat: Mapping[str, Any]) -> bool:
        expires_at = heartbeat.get("credential_expires_at")
        if expires_at is None:
            # Rolling-upgrade compatibility: older control planes do not
            # advertise credential expiry. Manual rotation remains available.
            return False
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
        ):
            raise DeviceRuntimeProtocolError(
                "device heartbeat has an invalid credential_expires_at"
            )
        return float(expires_at) - self._journal_time() <= self.credential_rotation_window

    @staticmethod
    def _capability_features(factory: AdapterFactory) -> list[str]:
        capabilities = getattr(factory, "capabilities", None)
        if capabilities is None:
            return []
        if is_dataclass(capabilities):
            values = asdict(capabilities)
        elif isinstance(capabilities, Mapping):
            values = dict(capabilities)
        else:
            return []
        return sorted(
            str(name).replace("_", ".")
            for name, enabled in values.items()
            if enabled is True
        )

    def _capabilities(self) -> dict[str, Any]:
        providers: list[dict[str, Any]] = []
        for provider, factory in sorted(self.adapter_registry.items()):
            providers.append(
                {
                    "id": provider,
                    "transport": str(
                        getattr(factory, "transport", "native") or "native"
                    ),
                    "available": bool(getattr(factory, "available", True)),
                    "version": str(getattr(factory, "version", "") or ""),
                    "features": self._capability_features(factory),
                }
            )
        return {"providers": providers, "features": []}

    @staticmethod
    def _platform() -> dict[str, str]:
        system = platform.system().strip()
        release = platform.release().strip()
        return {
            "os": " ".join(value for value in (system, release) if value)[:120],
            "arch": platform.machine().strip()[:120],
            "hostname": platform.node().strip()[:120],
        }

    def _heartbeat_payload(self) -> dict[str, Any]:
        return {
            "schema": "agentserver.device-heartbeat/1",
            **self._runtime_identity(),
            "protocol_version": 1,
            "runtime_version": DEVICE_RUNTIME_VERSION,
            "capabilities": self._capabilities(),
            "platform": self._platform(),
            "health": "healthy",
            "last_error": "",
            "sessions": list(self.sessions.values()),
            "command_cursor": self.command_journal.cursor,
            "event_queue_depth": len(self.event_spool),
        }

    async def heartbeat(self) -> Mapping[str, Any]:
        self.credential_file.load()
        started_at = time.monotonic()
        payload = await self.client.heartbeat(self._heartbeat_payload())
        self._calibrate_server_time(
            payload.get("server_time"), elapsed=time.monotonic() - started_at
        )
        return payload

    def _calibrate_server_time(self, value: object, *, elapsed: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise DeviceRuntimeProtocolError(
                "device runtime response has no valid server_time"
            )
        sampled_at = time.monotonic()
        conservative = float(value) + max(0.0, float(elapsed))
        previous = self._server_clock_anchor
        if previous is not None:
            conservative = max(
                conservative,
                previous[0] + max(0.0, sampled_at - previous[1]),
            )
        self._server_clock_anchor = (conservative, sampled_at)
        return conservative

    def _journal_time(self) -> float:
        anchor = self._server_clock_anchor
        if anchor is None:
            return float("-inf")
        return anchor[0] + max(0.0, time.monotonic() - anchor[1])

    async def _emit_adapter_event(
        self,
        value: RuntimeEvent | TypedRuntimeEvent | Mapping[str, Any],
        *,
        default_session_id: str | None,
    ) -> dict[str, Any]:
        if isinstance(value, RuntimeEvent):
            event = value
        elif isinstance(value, TypedRuntimeEvent):
            payload = _json_object(value.payload, "runtime event payload")
            for key, extra in (
                ("turn_id", value.turn_id),
                ("item_id", value.item_id),
                ("interaction_id", value.interaction_id),
                ("provider", value.provider),
                ("provider_event_id", value.event_id),
            ):
                if extra is not None and key not in payload:
                    payload[key] = extra
            event = RuntimeEvent(
                type=value.type,
                payload=payload,
                session_id=value.session_id,
                occurred_at=value.occurred_at,
            )
        elif isinstance(value, Mapping):
            event = RuntimeEvent(
                type=str(value.get("type") or ""),
                payload=_json_object(value.get("payload"), "runtime event payload"),
                session_id=(
                    str(value["session_id"])
                    if value.get("session_id") is not None
                    else None
                ),
                occurred_at=(
                    float(value["occurred_at"])
                    if value.get("occurred_at") is not None
                    else None
                ),
            )
        else:
            raise ValueError("adapter event must be RuntimeEvent or object")
        session_id = event.session_id or default_session_id
        if (
            default_session_id is not None
            and session_id is not None
            and session_id != default_session_id
        ):
            raise ValueError("adapter event cannot cross its session boundary")
        normalized = RuntimeEvent(
            type=event.type,
            payload=event.payload,
            session_id=session_id,
            occurred_at=event.occurred_at,
        )
        return await asyncio.to_thread(
            self.event_spool.enqueue,
            normalized,
            device_id=self.device_id,
            instance_id=self.instance_id,
            boot_id=self.boot_id,
            runtime_session_id=self.runtime_session_id,
            generation=self.generation,
        )

    def _context(
        self,
        *,
        provider: str,
        session_id: str | None,
        payload: Mapping[str, Any],
    ) -> AdapterContext:
        workspace_value = payload.get("workspace") or payload.get("cwd")
        workspace = str(workspace_value) if workspace_value is not None else None
        return AdapterContext(
            device_id=self.device_id,
            instance_id=self.instance_id,
            boot_id=self.boot_id,
            provider=provider,
            session_id=session_id,
            workspace=workspace,
            metadata=dict(payload),
        )

    async def _create_adapter(
        self,
        *,
        provider: str,
        session_id: str | None,
        payload: Mapping[str, Any],
        event_gate: _SessionEventGate | None = None,
    ) -> object:
        factory = self.adapter_registry.get(provider)
        if factory is None:
            raise DeviceRuntimeProtocolError(
                f"runtime provider is not installed: {provider}"
            )

        async def emit(value: RuntimeEvent | Mapping[str, Any]) -> dict[str, Any]:
            if event_gate is not None and not event_gate.accepting:
                # Legacy callback adapters may emit their normal terminal
                # lifecycle while close() tears them down. A server-side
                # permanent rejection has already made that stream poison, so
                # acknowledge the local callback without creating another
                # event that would recurse into the same rejection.
                return {"suppressed": True, "session_id": session_id}
            return await self._emit_adapter_event(
                value, default_session_id=session_id
            )

        context = self._context(
            provider=provider,
            session_id=session_id,
            payload=payload,
        )
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            signature = None
        use_legacy_factory = True
        if signature is not None:
            try:
                signature.bind(context, emit)
            except TypeError:
                try:
                    signature.bind()
                except TypeError as error:
                    raise DeviceRuntimeProtocolError(
                        "runtime adapter factory has an unsupported signature"
                    ) from error
                use_legacy_factory = False
        created = factory(context, emit) if use_legacy_factory else factory()
        adapter = await _await_result(created)
        if adapter is None:
            raise DeviceRuntimeProtocolError("runtime adapter factory returned no adapter")
        return adapter

    @staticmethod
    async def _adapter_call(
        adapter: object, method_name: str, payload: Mapping[str, Any]
    ) -> object:
        method = getattr(adapter, method_name, None)
        if not callable(method):
            raise DeviceRuntimeProtocolError(
                f"runtime adapter does not implement {method_name}"
            )
        return await _await_result(method(dict(payload)))

    @staticmethod
    async def _adapter_close(adapter: object) -> None:
        close = getattr(adapter, "close", None)
        if callable(close):
            await _await_result(close())

    async def _pump_adapter_events(
        self,
        adapter: TypedRuntimeAdapter,
        session_id: str,
        event_gate: _SessionEventGate,
    ) -> None:
        stream = adapter.events(session_id)
        if not hasattr(stream, "__aiter__"):
            raise DeviceRuntimeProtocolError(
                "runtime adapter events() must return an async iterator"
            )
        async for event in stream:
            if not event_gate.accepting:
                return
            await self._emit_adapter_event(
                event,
                default_session_id=session_id,
            )
            event_type = (
                event.get("type") if isinstance(event, Mapping) else getattr(event, "type", None)
            )
            if event_type in {"session.stopped", "session.failed"}:
                # Mark terminal only after the envelope is durable. Some
                # adapters emit session.failed + session.exited and then end
                # their iterator while the Host handle is still registered.
                # Reaping that handle must not synthesize a second terminal
                # event that the server would permanently reject.
                event_gate.terminal_event_spooled = True

    @staticmethod
    def _record_event_pump_outcome(
        handle: _SessionHandle,
        task: asyncio.Task[None],
    ) -> None:
        """Consume and retain a safe summary of a completed pump task."""

        if handle.event_task_outcome is not None or not task.done():
            return
        if task.cancelled():
            handle.event_task_outcome = "cancelled"
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            handle.event_task_outcome = "cancelled"
            return
        handle.event_task_outcome = (
            "completed" if error is None else type(error).__name__[:120]
        )

    async def _enqueue_event_pump_failure(
        self,
        handle: _SessionHandle,
        failure_kind: str,
    ) -> None:
        enqueue_task = asyncio.create_task(
            self._emit_adapter_event(
                RuntimeEvent(
                    type="session.failed",
                    payload={
                        "error": "runtime_event_pump_failed",
                        "error_code": "event_pump_failed",
                        "cause": failure_kind,
                    },
                    session_id=handle.session_id,
                ),
                default_session_id=handle.session_id,
            ),
            name=f"device-runtime-pump-failure:{handle.session_id}",
        )
        try:
            await asyncio.shield(enqueue_task)
        except asyncio.CancelledError:
            # The SQLite write runs in a worker thread. Shield it through the
            # cancellation boundary so the next cycle knows whether it must
            # enqueue or only finish removing/closing the handle.
            [outcome] = await asyncio.gather(enqueue_task, return_exceptions=True)
            if not isinstance(outcome, BaseException):
                handle.event_pump_failure_enqueued = True
            raise
        handle.event_pump_failure_enqueued = True

    async def _reap_failed_event_pumps(self) -> dict[str, list[str]]:
        """Fail-close active sessions whose typed event stream has stopped.

        The failure lifecycle is durably spooled before the handle is removed
        or its adapter is closed.  If the bounded spool is still full, the
        registered handle remains available to this reaper and the cycle
        reports an explicit retryable error instead of silently losing the
        terminal event.
        """

        handles_to_close: list[_SessionHandle] = []
        errors: dict[str, BaseException] = {}
        async with self._session_lock:
            for session_id, handle in list(self._sessions.items()):
                task = handle.event_task
                if (
                    not handle.typed
                    or task is None
                    or not task.done()
                    or not handle.event_gate.accepting
                    or handle.event_task_expected_shutdown
                ):
                    continue
                self._record_event_pump_outcome(handle, task)
                failure_kind = handle.event_task_outcome or "unknown"
                if (
                    not handle.event_gate.terminal_event_spooled
                    and not handle.event_pump_failure_enqueued
                ):
                    try:
                        await self._enqueue_event_pump_failure(
                            handle,
                            failure_kind,
                        )
                    except asyncio.CancelledError:
                        raise
                    except BaseException as error:
                        errors[f"session_failed_event:{session_id}"] = error
                        continue

                # Removal and gate closure happen under the same lock used by
                # session commands.  Once the durable failure exists, no new
                # command or close-generated event may race this fail-close.
                if self._sessions.get(session_id) is not handle:
                    continue
                self._sessions.pop(session_id, None)
                handle.event_task_expected_shutdown = True
                handle.event_gate.accepting = False
                handles_to_close.append(handle)

        outcomes = await asyncio.gather(
            *(
                self._close_session_handle(handle, suppress_events=True)
                for handle in handles_to_close
            ),
            return_exceptions=True,
        )
        errors.update(
            {
                f"session_close:{handle.session_id}": outcome
                for handle, outcome in zip(handles_to_close, outcomes, strict=True)
                if isinstance(outcome, BaseException)
            }
        )
        if errors:
            raise DeviceRuntimeCycleError(errors)
        return {
            "reaped": [handle.session_id for handle in handles_to_close],
        }

    @staticmethod
    async def _finish_event_pump(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    async def _cancel_event_pump(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _close_session_handle(
        self,
        handle: _SessionHandle,
        *,
        suppress_events: bool = False,
    ) -> None:
        handle.event_task_expected_shutdown = True
        if suppress_events:
            handle.event_gate.accepting = False
            await self._cancel_event_pump(handle.event_task)
            await self._adapter_close(handle.adapter)
            return
        try:
            await self._adapter_close(handle.adapter)
        finally:
            try:
                await self._finish_event_pump(handle.event_task)
            finally:
                handle.event_gate.accepting = False

    async def _fail_closed_sessions(
        self, permanent_rejections: Sequence[Mapping[str, Any]]
    ) -> None:
        session_ids: list[str] = []
        seen: set[str] = set()
        for rejection in permanent_rejections:
            session_id = rejection.get("session_id")
            if (
                isinstance(session_id, str)
                and session_id
                and session_id not in seen
            ):
                seen.add(session_id)
                session_ids.append(session_id)
        if not session_ids:
            return

        handles: list[_SessionHandle] = []
        async with self._session_lock:
            for session_id in session_ids:
                handle = self._sessions.pop(session_id, None)
                if handle is None:
                    continue
                # Close the callback gate while removal is protected by the
                # same lock used by session commands. No new command can find
                # this handle and no legacy close callback can enqueue after
                # the permanent settlement becomes visible.
                handle.event_gate.accepting = False
                handles.append(handle)
        outcomes = await asyncio.gather(
            *(
                self._close_session_handle(handle, suppress_events=True)
                for handle in handles
            ),
            return_exceptions=True,
        )
        errors = {
            f"session_close:{handle.session_id}": outcome
            for handle, outcome in zip(handles, outcomes, strict=True)
            if isinstance(outcome, BaseException)
        }
        if errors:
            # Every handle has already been removed, muted and given a close
            # attempt. Surface resource teardown failures without leaving a
            # later rejected session's event pump alive merely because an
            # earlier adapter failed to close.
            raise DeviceRuntimeCycleError(errors)

    @staticmethod
    def _result_object(result: object, label: str) -> dict[str, Any] | None:
        if isinstance(result, Mapping):
            return _json_object(result, label)
        if is_dataclass(result) and not isinstance(result, type):
            return _json_object(asdict(result), label)
        as_dict = getattr(result, "as_dict", None)
        if callable(as_dict):
            return _json_object(as_dict(), label)
        return None

    @staticmethod
    def _ack_result(result: object) -> tuple[str, dict[str, Any]]:
        if result is None:
            return "completed", {}
        if isinstance(result, str):
            if result not in {"accepted", "rejected", "completed"}:
                raise DeviceRuntimeProtocolError(
                    "adapter command result string is not a valid ACK status"
                )
            return result, {}
        normalized = DeviceRuntimeHost._result_object(
            result, "adapter command result"
        )
        if normalized is None:
            raise DeviceRuntimeProtocolError(
                "adapter command result must be an object, ACK status, or null"
            )
        status = str(normalized.get("status") or "completed")
        if status not in {"accepted", "rejected", "completed"}:
            raise DeviceRuntimeProtocolError(
                "adapter command result has an invalid ACK status"
            )
        if "payload" in normalized:
            payload = _json_object(normalized.get("payload"), "adapter ACK payload")
        else:
            payload = _json_object(
                {
                    key: value
                    for key, value in normalized.items()
                    if key != "status"
                },
                "adapter ACK payload",
            )
        return status, payload

    def _command_payload(self, command: Mapping[str, Any]) -> dict[str, Any]:
        payload = _json_object(command.get("payload"), "device command payload")
        if (
            str(payload.get("device_id") or "") != self.device_id
            or str(payload.get("runtime_session_id") or "")
            != self.runtime_session_id
            or payload.get("runtime_generation") != self.generation
        ):
            raise DeviceRuntimeProtocolError(
                "device command does not match the active runtime fence"
            )
        for key in ("device_id", "runtime_session_id", "runtime_generation"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _preflight_rejection(error: BaseException) -> dict[str, str]:
        message = str(error).strip() or "runtime command failed validation"
        return {
            "error_code": "invalid_command",
            "error": message[:1000],
        }

    async def _preflight_command(
        self, command: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate a command before the journal crosses its side-effect fence."""

        command_type = str(command.get("type") or "")
        payload = self._command_payload(command)
        if command_type == "runtime.probe":
            provider = str(payload.get("provider") or "").strip()
            if provider and provider not in self.adapter_registry:
                raise DeviceRuntimeProtocolError(
                    f"runtime provider is not installed: {provider}"
                )
            return payload

        session_id = _require_text(payload.get("session_id"), "session_id")
        if command_type == "session.start":
            provider = _require_text(payload.get("provider"), "provider", limit=80)
            async with self._session_lock:
                existing = self._sessions.get(session_id)
                if existing is not None:
                    if existing.provider != provider:
                        raise DeviceRuntimeProtocolError(
                            "session_id is already bound to another provider"
                        )
                    return payload
            factory = self.adapter_registry.get(provider)
            if factory is None:
                raise DeviceRuntimeProtocolError(
                    f"runtime provider is not installed: {provider}"
                )
            if getattr(factory, "available", True) is False:
                raise DeviceRuntimeProtocolError(
                    f"runtime provider is unavailable: {provider}"
                )
            # Legacy adapter commands did not require workspace. Frozen v1
            # commands do, and can be fully validated without starting a
            # provider process.
            if payload.get("workspace") or payload.get("cwd"):
                spec = self._session_spec(payload)
                permission_mode = str(
                    getattr(spec.permission_mode, "value", spec.permission_mode)
                )
                if permission_mode not in {
                    "approval-required",
                    "workspace-write",
                    "full-access",
                    "auto",
                    "auto-accept-edits",
                }:
                    raise ValueError("session permission_mode is unsupported")
                try:
                    workspace = Path(spec.cwd).expanduser().resolve()
                except OSError as error:
                    raise ValueError("session workspace cannot be resolved") from error
                if not workspace.is_dir():
                    raise ValueError("session workspace must be an existing directory")
            return payload

        if command_type == "session.stop":
            return payload

        async with self._session_lock:
            if session_id not in self._sessions:
                raise DeviceRuntimeProtocolError("runtime session is not active")

        if command_type in {"session.turn", "turn.start"}:
            self._turn_input(payload)
        elif command_type in {"session.interrupt", "turn.interrupt"}:
            turn_id = payload.get("turn_id")
            if turn_id is not None and turn_id != "":
                _require_text(turn_id, "turn_id")
        elif command_type == "session.respond":
            response = _json_object(payload.get("response"), "session response")
            _require_text(
                payload.get("interaction_id") or payload.get("request_id"),
                "request_id",
            )
            if "decision" in response:
                self._approval_decision(response.get("decision"))
            elif "answers" in response:
                if not isinstance(response.get("answers"), Mapping):
                    raise ValueError("user-input response answers must be an object")
            else:
                raise ValueError(
                    "session response must contain either decision or answers"
                )
        elif command_type == "approval.respond":
            _require_text(
                payload.get("interaction_id") or payload.get("request_id"),
                "request_id",
            )
            response = payload.get("response")
            body = response if isinstance(response, Mapping) else payload
            self._approval_decision(body.get("decision"))
        elif command_type == "user_input.respond":
            _require_text(
                payload.get("interaction_id") or payload.get("request_id"),
                "request_id",
            )
            response = payload.get("response")
            body = response if isinstance(response, Mapping) else payload
            if not isinstance(body.get("answers"), Mapping):
                raise ValueError("user-input response answers must be an object")
        else:
            raise DeviceRuntimeProtocolError(
                f"unsupported device command type: {command_type}"
            )
        return payload

    async def _probe(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        requested = str(payload.get("provider") or "").strip()
        providers = [requested] if requested else sorted(self.adapter_registry)
        if not providers:
            return {"providers": {}}
        results: dict[str, Any] = {}
        for provider in providers:
            provider = _require_text(provider, "provider", limit=80)
            adapter = await self._create_adapter(
                provider=provider, session_id=None, payload=payload
            )
            try:
                if isinstance(adapter, TypedRuntimeAdapter):
                    result = await adapter.probe()
                else:
                    result = await self._adapter_call(adapter, "probe", payload)
                normalized = self._result_object(result, "probe result")
                if normalized is not None:
                    results[provider] = normalized
                elif result is None:
                    results[provider] = {"available": True}
                else:
                    results[provider] = {"available": bool(result)}
            finally:
                await self._adapter_close(adapter)
        return {"providers": results}

    @staticmethod
    def _session_spec(payload: Mapping[str, Any]) -> RuntimeSessionSpec:
        session_id = _require_text(payload.get("session_id"), "session_id")
        cwd = _require_text(
            payload.get("workspace") or payload.get("cwd"),
            "workspace",
            limit=4096,
        )
        options = _json_object(payload.get("options"), "session options")

        def option(name: str, default: object = None) -> object:
            return payload[name] if name in payload else options.get(name, default)

        environment_value = option("environment")
        environment: dict[str, str] | None = None
        if environment_value is not None:
            if not isinstance(environment_value, Mapping):
                raise ValueError("session environment must be an object")
            environment = {}
            for key, value in environment_value.items():
                name = _require_text(key, "environment name", limit=255)
                if not isinstance(value, str) or "\0" in value:
                    raise ValueError("session environment values must be strings")
                environment[name] = value
        resume_value = option("resume_cursor")
        resume_cursor = (
            _json_object(resume_value, "resume_cursor")
            if resume_value is not None
            else None
        )

        def optional_text(name: str, *, limit: int = 255) -> str | None:
            value = option(name)
            if value is None or value == "":
                return None
            return _require_text(value, name, limit=limit)

        permission_mode = option("permission_mode", "workspace-write")
        return RuntimeSessionSpec(
            session_id=session_id,
            cwd=cwd,
            permission_mode=_require_text(
                permission_mode, "permission_mode", limit=80
            ),
            model=optional_text("model"),
            service_tier=optional_text("service_tier"),
            resume_cursor=resume_cursor,
            environment=environment,
        )

    @staticmethod
    def _turn_input(payload: Mapping[str, Any]) -> RuntimeTurnInput:
        options = _json_object(payload.get("options"), "turn options")

        def option(name: str, default: object = None) -> object:
            return payload[name] if name in payload else options.get(name, default)

        text_value = option("input", option("text"))
        text = None if text_value is None else str(text_value)
        attachments_value = option("attachments", [])
        if not isinstance(attachments_value, Sequence) or isinstance(
            attachments_value, (str, bytes, bytearray)
        ):
            raise ValueError("turn attachments must be an array")
        attachments: list[RuntimeAttachment] = []
        for raw in attachments_value:
            if not isinstance(raw, Mapping):
                raise ValueError("turn attachment must be an object")
            attachments.append(
                RuntimeAttachment(
                    type=_require_text(raw.get("type"), "attachment type", limit=80),
                    url=_require_text(raw.get("url"), "attachment url", limit=8192),
                )
            )

        def optional_text(name: str) -> str | None:
            value = option(name)
            if value is None or value == "":
                return None
            return _require_text(value, name)

        if (text is None or text == "") and not attachments:
            raise ValueError("turn input requires text or an attachment")
        return RuntimeTurnInput(
            text=text,
            attachments=tuple(attachments),
            model=optional_text("model"),
            service_tier=optional_text("service_tier"),
            effort=optional_text("effort"),
        )

    async def _start_session(self, payload: Mapping[str, Any]) -> object:
        session_id = _require_text(payload.get("session_id"), "session_id")
        provider = _require_text(payload.get("provider"), "provider", limit=80)
        async with self._session_lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                if existing.provider != provider:
                    raise DeviceRuntimeProtocolError(
                        "session_id is already bound to another provider"
                    )
                return {
                    "status": "completed",
                    "payload": {"session_id": session_id, "already_started": True},
                }
            event_gate = _SessionEventGate()
            adapter = await self._create_adapter(
                provider=provider,
                session_id=session_id,
                payload=payload,
                event_gate=event_gate,
            )
            typed = isinstance(adapter, TypedRuntimeAdapter)
            try:
                if typed:
                    result = await adapter.start_session(self._session_spec(payload))
                else:
                    result = await self._adapter_call(
                        adapter, "start_session", payload
                    )
            except BaseException:
                event_gate.accepting = False
                with contextlib.suppress(BaseException):
                    await self._adapter_close(adapter)
                raise
            handle = _SessionHandle(
                session_id=session_id,
                provider=provider,
                adapter=adapter,
                started_at=time.time(),
                event_gate=event_gate,
                typed=typed,
            )
            self._sessions[session_id] = handle
            if typed:
                handle.event_task = asyncio.create_task(
                    self._pump_adapter_events(adapter, session_id, event_gate),
                    name=f"device-runtime-events:{session_id}",
                )
                handle.event_task.add_done_callback(
                    lambda task, session_handle=handle: (
                        self._record_event_pump_outcome(session_handle, task)
                    )
                )
            return result

    async def _stop_session(self, payload: Mapping[str, Any]) -> object:
        session_id = _require_text(payload.get("session_id"), "session_id")
        async with self._session_lock:
            handle = self._sessions.get(session_id)
            if handle is None:
                await self._emit_adapter_event(
                    RuntimeEvent(
                        type="session.stopped",
                        payload={"already_stopped": True},
                        session_id=session_id,
                    ),
                    default_session_id=session_id,
                )
                return {
                    "status": "completed",
                    "payload": {"session_id": session_id, "already_stopped": True},
                }
            if handle.typed:
                result = await handle.adapter.stop_session(session_id)
            else:
                result = await self._adapter_call(
                    handle.adapter, "stop_session", payload
                )
            self._sessions.pop(session_id, None)
            await self._close_session_handle(handle)
            return result

    async def _session_handle(self, payload: Mapping[str, Any]) -> _SessionHandle:
        session_id = _require_text(payload.get("session_id"), "session_id")
        handle = self._sessions.get(session_id)
        if handle is None:
            raise DeviceRuntimeProtocolError("runtime session is not active")
        return handle

    async def _turn_session(self, payload: Mapping[str, Any]) -> object:
        async with self._session_lock:
            handle = await self._session_handle(payload)
            turn_input = self._turn_input(payload)
            # Typed adapters expose the normalized event stream used by the
            # Runtime workspace. Legacy adapters retain their historical event
            # contract and do not receive a new durable prompt event.
            if handle.typed and handle.provider == "codex":
                turn_id_value = payload.get("turn_id")
                turn_id = (
                    _require_text(turn_id_value, "turn_id")
                    if turn_id_value is not None and turn_id_value != ""
                    else None
                )
                turn_payload: dict[str, Any] = {
                    "text": _public_text(turn_input.text or ""),
                    "attachment_count": len(turn_input.attachments),
                }
                if turn_id is not None:
                    turn_payload["turn_id"] = turn_id
                await self._emit_adapter_event(
                    RuntimeEvent(
                        type="turn.input",
                        payload=turn_payload,
                    ),
                    default_session_id=handle.session_id,
                )
            if handle.typed:
                return await handle.adapter.send_turn(
                    handle.session_id,
                    turn_input,
                )
            return await self._adapter_call(handle.adapter, "start_turn", payload)

    async def _interrupt_session(self, payload: Mapping[str, Any]) -> object:
        async with self._session_lock:
            handle = await self._session_handle(payload)
            if handle.typed:
                turn_id_value = payload.get("turn_id")
                turn_id = (
                    _require_text(turn_id_value, "turn_id")
                    if turn_id_value is not None and turn_id_value != ""
                    else None
                )
                return await handle.adapter.interrupt_turn(
                    handle.session_id,
                    turn_id,
                )
            return await self._adapter_call(
                handle.adapter, "interrupt_turn", payload
            )

    @staticmethod
    def _approval_decision(value: object) -> ApprovalDecision:
        raw = str(value or "").strip()
        aliases = {
            "accept": ApprovalDecision.APPROVE_ONCE,
            "approve": ApprovalDecision.APPROVE_ONCE,
            "approved": ApprovalDecision.APPROVE_ONCE,
            "acceptForSession": ApprovalDecision.APPROVE_SESSION,
            "approve_session": ApprovalDecision.APPROVE_SESSION,
            "decline": ApprovalDecision.DENY,
            "reject": ApprovalDecision.DENY,
            "cancel": ApprovalDecision.CANCEL_TURN,
        }
        if raw in aliases:
            return aliases[raw]
        try:
            return ApprovalDecision(raw)
        except ValueError as error:
            raise ValueError("approval response contains an invalid decision") from error

    async def _respond_session(self, payload: Mapping[str, Any]) -> object:
        async with self._session_lock:
            handle = await self._session_handle(payload)
            response = _json_object(payload.get("response"), "session response")
            interaction_id = _require_text(
                payload.get("interaction_id") or payload.get("request_id"),
                "request_id",
            )
            if "decision" in response:
                if handle.typed:
                    return await handle.adapter.respond_to_approval(
                        handle.session_id,
                        interaction_id,
                        self._approval_decision(response.get("decision")),
                    )
                return await self._adapter_call(
                    handle.adapter, "respond_to_approval", payload
                )
            if "answers" in response:
                answers = response.get("answers")
                if not isinstance(answers, Mapping):
                    raise ValueError("user-input response answers must be an object")
                if handle.typed:
                    return await handle.adapter.respond_to_user_input(
                        handle.session_id,
                        interaction_id,
                        answers,
                    )
                return await self._adapter_call(
                    handle.adapter, "respond_to_user_input", payload
                )
            raise ValueError(
                "session response must contain either decision or answers"
            )

    async def _legacy_response(
        self, payload: Mapping[str, Any], method_name: str
    ) -> object:
        async with self._session_lock:
            handle = await self._session_handle(payload)
            if handle.typed:
                interaction_id = _require_text(
                    payload.get("interaction_id") or payload.get("request_id"),
                    "request_id",
                )
                if method_name == "respond_to_approval":
                    response = payload.get("response")
                    body = response if isinstance(response, Mapping) else payload
                    return await handle.adapter.respond_to_approval(
                        handle.session_id,
                        interaction_id,
                        self._approval_decision(body.get("decision")),
                    )
                response = payload.get("response")
                body = response if isinstance(response, Mapping) else payload
                answers = body.get("answers")
                if not isinstance(answers, Mapping):
                    raise ValueError("user-input response answers must be an object")
                return await handle.adapter.respond_to_user_input(
                    handle.session_id,
                    interaction_id,
                    answers,
                )
            return await self._adapter_call(handle.adapter, method_name, payload)

    async def _handle_command(
        self,
        command: Mapping[str, Any],
        *,
        prepared_payload: Mapping[str, Any] | None = None,
    ) -> object:
        command_type = str(command.get("type") or "")
        payload = (
            dict(prepared_payload)
            if prepared_payload is not None
            else self._command_payload(command)
        )
        if command_type == "runtime.probe":
            return await self._probe(payload)
        if command_type == "session.start":
            return await self._start_session(payload)
        if command_type == "session.stop":
            return await self._stop_session(payload)
        if command_type in {"session.turn", "turn.start"}:
            return await self._turn_session(payload)
        if command_type in {"session.interrupt", "turn.interrupt"}:
            return await self._interrupt_session(payload)
        if command_type == "session.respond":
            return await self._respond_session(payload)
        if command_type == "approval.respond":
            return await self._legacy_response(payload, "respond_to_approval")
        if command_type == "user_input.respond":
            return await self._legacy_response(payload, "respond_to_user_input")
        raise DeviceRuntimeProtocolError(
            f"unsupported device command type: {command_type}"
        )

    def _validate_server_commands(
        self, values: object
    ) -> list[Mapping[str, Any]]:
        if not isinstance(values, list) or any(
            not isinstance(value, Mapping) for value in values
        ):
            raise DeviceRuntimeProtocolError(
                "device commands response must contain an array of objects"
            )
        commands = list(values)
        for command in commands:
            if str(command.get("target_kind") or "") != "device":
                raise DeviceRuntimeProtocolError(
                    "device command target_kind must be device"
                )
            if str(command.get("target_id") or "") != self.device_id:
                raise DeviceRuntimeProtocolError(
                    "device command target does not match this device"
                )
            if str(command.get("type") or "") not in SUPPORTED_COMMAND_TYPES:
                raise DeviceRuntimeProtocolError(
                    "device command type is not allowlisted"
                )
        return commands

    async def _recover_idempotent_uncertain(self, *, now: float) -> None:
        summary = await asyncio.to_thread(
            self.command_journal.status_summary, now=now
        )
        for command in summary["uncertain"]:
            if (
                command.get("status") == "uncertain"
                and command.get("type") in IDEMPOTENT_COMMAND_TYPES
            ):
                await asyncio.to_thread(
                    self.command_journal.retry_uncertain,
                    str(command["command_id"]),
                    now=now,
                )

    def _advance_command_cursor(self, value: object) -> int:
        """Persist an authenticated server page cursor without allowing rollback."""

        if value is None:
            return self.command_journal.cursor
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value >= 2**63
        ):
            raise DeviceRuntimeProtocolError(
                "device commands response has an invalid next_sequence"
            )
        # BridgeCommandJournal owns this metadata table. Its lock keeps this
        # monotonic update atomic with any direct journal operation in-process;
        # BEGIN IMMEDIATE also serializes another process that is inspecting a
        # stopped Host's durable state.
        with (
            self.command_journal._lock,
            self.command_journal._connect() as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM bridge_command_metadata "
                "WHERE key = 'server_cursor'"
            ).fetchone()
            current = int(row["value"]) if row is not None else 0
            cursor = max(current, value)
            connection.execute(
                """
                INSERT INTO bridge_command_metadata(key, value)
                VALUES ('server_cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(cursor),),
            )
        return cursor

    async def poll_commands(self) -> list[dict[str, Any]]:
        self.credential_file.load()
        async with self._command_lock:
            started_at = time.monotonic()
            response = await self.client.commands(
                after_sequence=self.command_journal.cursor,
                **self._runtime_identity(),
            )
            server_time = self._calibrate_server_time(
                response.get("server_time"), elapsed=time.monotonic() - started_at
            )
            commands = self._validate_server_commands(response.get("commands", []))
            await asyncio.to_thread(
                self.command_journal.record_server_commands,
                commands,
                now=server_time,
            )
            await asyncio.to_thread(
                self._advance_command_cursor,
                response.get("next_sequence"),
            )
            await self._recover_idempotent_uncertain(now=server_time)
            identifiers = await asyncio.to_thread(
                self.command_journal.dispatchable_ids, now=server_time
            )
            errors: dict[str, BaseException] = {}
            for command_id in identifiers:
                candidate = await asyncio.to_thread(
                    self.command_journal.command,
                    command_id,
                    now=self._journal_time(),
                )
                if candidate is None or candidate.get("status") != "pending":
                    continue
                try:
                    prepared_payload = await self._preflight_command(candidate)
                except asyncio.CancelledError:
                    raise
                except (DeviceRuntimeProtocolError, ValueError) as error:
                    try:
                        await asyncio.to_thread(
                            self.command_journal.prepare_ack,
                            command_id=command_id,
                            status="rejected",
                            payload=self._preflight_rejection(error),
                            now=self._journal_time(),
                        )
                    except BaseException as journal_error:
                        errors[command_id] = journal_error
                    continue
                except BaseException as error:
                    # No side effect has started, so leave the command pending
                    # for a later healthy cycle instead of mislabelling it
                    # uncertain.
                    errors[command_id] = error
                    continue
                command = await asyncio.to_thread(
                    self.command_journal.begin_handler,
                    command_id,
                    now=self._journal_time(),
                )
                if command is None:
                    continue
                try:
                    result = await self._handle_command(
                        command,
                        prepared_payload=prepared_payload,
                    )
                    status, payload = self._ack_result(result)
                    await asyncio.to_thread(
                        self.command_journal.prepare_ack,
                        command_id=command_id,
                        status=status,
                        payload=payload,
                        now=self._journal_time(),
                    )
                except asyncio.CancelledError:
                    # Leave ``executing`` intact. Reopening the journal converts
                    # it to uncertain, accurately representing an interrupted
                    # side-effect boundary.
                    raise
                except BaseException as error:
                    with contextlib.suppress(BridgeCommandJournalError):
                        await asyncio.to_thread(
                            self.command_journal.mark_uncertain,
                            command_id,
                            f"{type(error).__name__}: handler outcome unknown",
                            now=self._journal_time(),
                        )
                    errors[command_id] = error
            try:
                await self._flush_command_acks_locked()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                errors["acks"] = error
            if errors:
                raise DeviceRuntimeCycleError(errors)
            return await asyncio.to_thread(
                self.command_journal.pending, now=self._journal_time()
            )

    async def _flush_command_acks_locked(self) -> dict[str, Mapping[str, Any]]:
        acknowledgements = await asyncio.to_thread(
            self.command_journal.replayable_acks, now=self._journal_time()
        )
        results: dict[str, Mapping[str, Any]] = {}
        errors: dict[str, BaseException] = {}
        for acknowledgement in acknowledgements:
            try:
                response = await self.client.acknowledge_command(
                    acknowledgement.command_id,
                    acknowledgement.request_body(),
                    **self._runtime_identity(),
                )
                await asyncio.to_thread(
                    self.command_journal.mark_acknowledged,
                    acknowledgement.ack_id,
                    response,
                    now=self._journal_time(),
                )
                results[acknowledgement.ack_id] = response
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                errors[acknowledgement.ack_id] = error
        if errors:
            raise DeviceRuntimeCycleError(errors)
        return results

    async def flush_command_acks(self) -> dict[str, Mapping[str, Any]]:
        self.credential_file.load()
        async with self._command_lock:
            return await self._flush_command_acks_locked()

    async def flush_events(self) -> Mapping[str, Any]:
        self.credential_file.load()
        # Provider callbacks may enqueue lifecycle events before their command
        # handler returns.  The durable ACK carries the provider's opaque turn
        # identity, so it must reach the server before those events can be
        # projected.  Retrying the ACK first also closes the response-loss
        # window without weakening the server's turn-reservation CAS.
        async with self._command_lock:
            ack_error: DeviceRuntimeCycleError | None = None
            try:
                await self._flush_command_acks_locked()
            except asyncio.CancelledError:
                raise
            except DeviceRuntimeCycleError as error:
                ack_error = error
            barriers = await asyncio.to_thread(
                self.command_journal.causal_barriers,
                now=self._journal_time(),
            )
            if barriers:
                causal_error = DeviceRuntimeCycleError(
                    {
                        "causal_barrier": BridgeCommandJournalError(
                            "command side effects are awaiting durable ACK settlement"
                        )
                    }
                )
                if ack_error is not None:
                    raise causal_error from ack_error
                raise causal_error
            if ack_error is not None:
                raise ack_error
            async with self._event_lock:
                events = await asyncio.to_thread(
                    self.event_spool.delivery_batch,
                    limit=MAX_DEVICE_EVENT_BATCH,
                    maximum_bytes=MAX_DEVICE_EVENT_BATCH_BYTES,
                )
                if not events:
                    return {
                        "accepted_through_seq": 0,
                        "missing_ranges": [],
                        "results": [],
                    }
                # Network/HTTP failures happen before local settlement, so the
                # exact envelopes remain live for at-least-once retry.
                response = await self.client.send_events(
                    events,
                    **self._runtime_identity(),
                )
                settlement = await asyncio.to_thread(
                    self.event_spool.settle_delivery,
                    events,
                    response.get("results"),
                )
                await self._fail_closed_sessions(
                    settlement["permanent_rejections"]
                )
                return {**dict(response), "settlement": settlement}

    async def run_once(self, *, force_heartbeat: bool = False) -> dict[str, Any]:
        if not self.enrolled:
            raise DeviceRuntimeError("device runtime is not enrolled")
        errors: dict[str, BaseException] = {}
        results: dict[str, Any] = {}
        if self.rotation_request_path.exists() or self.rotation_request_path.is_symlink():
            try:
                # A committed rotation revokes the credential still on disk.
                # Recover the durable request before any heartbeat or command
                # request attempts to authenticate with that stale value.
                results["credential_rotation"] = await self.rotate_credential()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                # Do not let a failed recovery heartbeat with this process's
                # new generation: the pending request is fenced to the old
                # generation and must remain recoverable on the next cycle.
                raise DeviceRuntimeCycleError(
                    {"credential_rotation": error}
                ) from error
        now = time.monotonic()
        if force_heartbeat or now >= self._next_heartbeat_at:
            heartbeat: dict[str, Any] | None = None
            try:
                heartbeat = dict(await self.heartbeat())
                results["heartbeat"] = heartbeat
                self._next_heartbeat_at = time.monotonic() + self.heartbeat_interval
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                errors["heartbeat"] = error
            if (
                heartbeat is not None
                and "credential_rotation" not in results
                and "credential_rotation" not in errors
            ):
                try:
                    if self._credential_rotation_due(heartbeat):
                        results["credential_rotation"] = await self.rotate_credential(
                            claim_current_fence=False
                        )
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    errors["credential_rotation"] = error
        for name, operation in (
            ("events", self.flush_events),
            # Flush first: a pump commonly dies because the bounded spool is
            # full.  Settling that batch creates room for the durable
            # session.failed event in this same cycle.
            ("event_pumps", self._reap_failed_event_pumps),
            ("acks", self.flush_command_acks),
            ("commands", self.poll_commands),
        ):
            try:
                results[name] = await operation()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                errors[name] = error
        if errors:
            raise DeviceRuntimeCycleError(errors)
        return results

    async def _wait_for_stop(self, delay: float, external: asyncio.Event | None) -> bool:
        if self._stop_event.is_set() or (external is not None and external.is_set()):
            return True
        tasks = [asyncio.create_task(self._stop_event.wait())]
        if external is not None:
            tasks.append(asyncio.create_task(external.wait()))
        try:
            done, _pending = await asyncio.wait(
                tasks, timeout=max(0.0, delay), return_when=asyncio.FIRST_COMPLETED
            )
            return bool(done)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        if not self.enrolled:
            raise DeviceRuntimeError("device runtime is not enrolled")
        if not self._lock_acquired:
            self._instance_lock.acquire()
            self._lock_acquired = True
        self._stop_event.clear()
        backoff = self.initial_backoff
        while not self._stop_event.is_set() and not (
            stop_event is not None and stop_event.is_set()
        ):
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except BaseException:
                if await self._wait_for_stop(backoff, stop_event):
                    break
                backoff = min(self.max_backoff, backoff * 2)
            else:
                backoff = self.initial_backoff
                if await self._wait_for_stop(self.poll_interval, stop_event):
                    break

    def request_stop(self) -> None:
        self._stop_event.set()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        try:
            async with self._session_lock:
                sessions = list(self._sessions.values())
                self._sessions.clear()
            for handle in sessions:
                with contextlib.suppress(BaseException):
                    await self._close_session_handle(handle)
            with contextlib.suppress(BaseException):
                if self.enrolled:
                    await self.flush_events()
            await self.client.close()
        finally:
            if self._lock_acquired:
                self._instance_lock.release()
                self._lock_acquired = False


__all__ = [
    "AdapterContext",
    "AdapterEventSink",
    "AdapterFactory",
    "DeviceEventSpool",
    "DeviceEventSpoolFull",
    "DeviceRuntimeClient",
    "DeviceRuntimeCycleError",
    "DeviceRuntimeError",
    "DeviceRuntimeHost",
    "DeviceRuntimeHTTPClient",
    "DeviceRuntimeProtocolError",
    "PrivateCredentialFile",
    "RuntimeAdapter",
    "RuntimeEvent",
    "load_private_text_file",
]
