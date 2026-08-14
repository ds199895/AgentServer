from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ATTACHMENT_ID = re.compile(r"^sha256:([0-9a-f]{64})$")
SUPPORTED_IMAGE_MEDIA_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class ArtifactError(Exception):
    """Base error for the artifact subsystem."""


class ImageValidationError(ArtifactError, ValueError):
    """Raised when bytes do not contain an admitted image."""


class ImageSupportUnavailable(ArtifactError, RuntimeError):
    """Raised when the optional image decoder is unavailable."""


class AttachmentAccessDenied(ArtifactError, PermissionError):
    """Raised when a session log does not authorize an attachment read."""


class AttachmentIntegrityError(ArtifactError, OSError):
    """Raised when stored bytes no longer match their durable reference."""


@dataclass(frozen=True)
class WorkspaceFileRef:
    """A display/reference fact about a file, never an access capability."""

    path: str
    name: str = ""
    media_type: str | None = None
    size: int | None = None
    kind: str = "file"

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("workspace file path must not be empty")
        if self.size is not None and self.size < 0:
            raise ValueError("workspace file size must not be negative")
        if not self.kind:
            raise ValueError("workspace file kind must not be empty")

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        normalized = self.path.replace("\\", "/").rstrip("/")
        return normalized.rsplit("/", 1)[-1] or self.path

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.display_name,
            "media_type": self.media_type,
            "size": self.size,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class AttachmentRef:
    """Immutable metadata for content-addressed, validated image bytes."""

    id: str
    media_type: str
    size: int
    width: int
    height: int
    name: str = ""

    def __post_init__(self) -> None:
        if not ATTACHMENT_ID.fullmatch(self.id):
            raise ValueError(
                "attachment id must be 'sha256:' plus 64 lowercase hex digits"
            )
        if self.media_type not in SUPPORTED_IMAGE_MEDIA_TYPES.values():
            raise ValueError("attachment media type is not supported")
        if self.size <= 0:
            raise ValueError("attachment size must be positive")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("attachment dimensions must be positive")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id,
            "media_type": self.media_type,
            "size": self.size,
            "width": self.width,
            "height": self.height,
        }
        if self.name:
            result["name"] = self.name
        return result


@dataclass(frozen=True)
class AttachmentPayload:
    ref: AttachmentRef
    data: bytes


@dataclass(frozen=True)
class ArtifactEvent:
    """One durable artifact observation scoped to an owner and terminal."""

    sequence: int
    id: str
    owner: str
    terminal_id: str
    event_type: str
    file: WorkspaceFileRef
    source: str
    version: str
    created_at: float
    attachment: AttachmentRef | None = None
    schema_version: int = 1

    def as_dict(self) -> dict[str, object]:
        """Return the nested canonical form plus convenient UI fields."""
        result: dict[str, object] = {
            "sequence": self.sequence,
            "id": self.id,
            "type": self.event_type,
            "event": self.event_type,
            "owner": self.owner,
            "terminal_id": self.terminal_id,
            "file": self.file.as_dict(),
            "path": self.file.path,
            "name": self.file.display_name,
            "media_type": self.file.media_type,
            "size": self.file.size,
            "kind": self.file.kind,
            "source": self.source,
            "version": self.version,
            "created_at": self.created_at,
            "timestamp": self.created_at,
            "schema_version": self.schema_version,
            "attachment": self.attachment.as_dict() if self.attachment else None,
        }
        return result


@dataclass(eq=False)
class ArtifactSubscription:
    """An atomic snapshot followed by live events for one owner/terminal."""

    snapshot: tuple[ArtifactEvent, ...]
    _store: ArtifactEventStore
    _key: tuple[str, str]
    _queue: asyncio.Queue[ArtifactEvent]
    _loop: asyncio.AbstractEventLoop
    _closed: bool = False

    def __aiter__(self) -> ArtifactSubscription:
        return self

    async def __anext__(self) -> ArtifactEvent:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def next(self) -> ArtifactEvent:
        return await self.__anext__()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._store._unsubscribe(self)

    async def __aenter__(self) -> ArtifactSubscription:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


class ArtifactEventStore:
    """SQLite event log with race-free snapshot/live subscriptions.

    Events are committed before subscribers are notified. ``subscribe`` takes
    its snapshot and registers its live queue under the same process lock used
    by ``append``, so an event cannot fall into the gap between the two.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        max_subscription_queue: int = 1_024,
    ) -> None:
        if max_subscription_queue < 1:
            raise ValueError("max_subscription_queue must be positive")
        self.database_path = Path(database_path)
        self.max_subscription_queue = max_subscription_queue
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._subscribers: dict[
            tuple[str, str], set[ArtifactSubscription]
        ] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    terminal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL DEFAULT '',
                    file_media_type TEXT,
                    file_size INTEGER,
                    file_kind TEXT NOT NULL DEFAULT 'file',
                    source TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    attachment_id TEXT,
                    attachment_media_type TEXT,
                    attachment_size INTEGER,
                    attachment_width INTEGER,
                    attachment_height INTEGER,
                    attachment_name TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS artifact_events_terminal
                ON artifact_events(owner, terminal_id, sequence)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS artifact_events_attachment
                ON artifact_events(owner, terminal_id, attachment_id)
                WHERE attachment_id IS NOT NULL
                """
            )

    @staticmethod
    def _require_scope(owner: str, terminal_id: str) -> None:
        if not owner:
            raise ValueError("artifact owner must not be empty")
        if not terminal_id:
            raise ValueError("artifact terminal_id must not be empty")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ArtifactEvent:
        attachment = None
        if row["attachment_id"] is not None:
            attachment = AttachmentRef(
                id=row["attachment_id"],
                media_type=row["attachment_media_type"],
                size=int(row["attachment_size"]),
                width=int(row["attachment_width"]),
                height=int(row["attachment_height"]),
                name=row["attachment_name"] or "",
            )
        return ArtifactEvent(
            sequence=int(row["sequence"]),
            id=row["id"],
            owner=row["owner"],
            terminal_id=row["terminal_id"],
            event_type=row["event_type"],
            file=WorkspaceFileRef(
                path=row["file_path"],
                name=row["file_name"],
                media_type=row["file_media_type"],
                size=row["file_size"],
                kind=row["file_kind"],
            ),
            source=row["source"],
            version=row["version"],
            created_at=float(row["created_at"]),
            attachment=attachment,
            schema_version=int(row["schema_version"]),
        )

    def append(
        self,
        *,
        owner: str,
        terminal_id: str,
        event_type: str,
        file: WorkspaceFileRef,
        source: str,
        version: str = "",
        created_at: float | None = None,
        attachment: AttachmentRef | None = None,
        event_id: str | None = None,
    ) -> ArtifactEvent:
        self._require_scope(owner, terminal_id)
        if not event_type:
            raise ValueError("artifact event_type must not be empty")
        if not source:
            raise ValueError("artifact source must not be empty")
        if attachment and file.media_type and attachment.media_type != file.media_type:
            raise ValueError("file and attachment media types do not match")
        timestamp = time.time() if created_at is None else float(created_at)
        durable_id = event_id or uuid.uuid4().hex
        attachment_values: tuple[object, ...]
        if attachment:
            attachment_values = (
                attachment.id,
                attachment.media_type,
                attachment.size,
                attachment.width,
                attachment.height,
                attachment.name,
            )
        else:
            attachment_values = (None, None, None, None, None, None)

        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO artifact_events(
                        id, owner, terminal_id, event_type,
                        file_path, file_name, file_media_type, file_size, file_kind,
                        source, version, created_at, schema_version,
                        attachment_id, attachment_media_type, attachment_size,
                        attachment_width, attachment_height, attachment_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        durable_id,
                        owner,
                        terminal_id,
                        event_type,
                        file.path,
                        file.name,
                        file.media_type,
                        file.size,
                        file.kind,
                        source,
                        version,
                        timestamp,
                        *attachment_values,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM artifact_events WHERE sequence = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            if row is None:  # pragma: no cover - SQLite insert contract
                raise ArtifactError("artifact event insert did not return a row")
            event = self._from_row(row)
            self._publish(event)
            return event

    def snapshot(
        self,
        *,
        owner: str,
        terminal_id: str,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[ArtifactEvent]:
        self._require_scope(owner, terminal_id)
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        query = (
            "SELECT * FROM artifact_events "
            "WHERE owner = ? AND terminal_id = ? AND sequence > ? "
            "ORDER BY sequence"
        )
        parameters: tuple[object, ...] = (owner, terminal_id, after_sequence)
        if limit is not None:
            query += " LIMIT ?"
            parameters += (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._from_row(row) for row in rows]

    def recent(
        self,
        *,
        owner: str,
        terminal_id: str,
        limit: int = 500,
    ) -> list[ArtifactEvent]:
        """Return the newest bounded history in chronological order."""
        self._require_scope(owner, terminal_id)
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM artifact_events
                WHERE owner = ? AND terminal_id = ?
                ORDER BY sequence DESC LIMIT ?
                """,
                (owner, terminal_id, limit),
            ).fetchall()
        return [self._from_row(row) for row in reversed(rows)]

    def subscribe(
        self,
        *,
        owner: str,
        terminal_id: str,
        after_sequence: int = 0,
        snapshot_limit: int | None = None,
    ) -> ArtifactSubscription:
        """Atomically return persisted history and subscribe to future events."""
        self._require_scope(owner, terminal_id)
        loop = asyncio.get_running_loop()
        key = (owner, terminal_id)
        if snapshot_limit is not None and snapshot_limit <= 0:
            raise ValueError("snapshot_limit must be positive")
        queue: asyncio.Queue[ArtifactEvent] = asyncio.Queue(
            maxsize=self.max_subscription_queue
        )
        with self._lock:
            snapshot = tuple(
                self.recent(
                    owner=owner,
                    terminal_id=terminal_id,
                    limit=snapshot_limit,
                )
                if snapshot_limit is not None and after_sequence == 0
                else self.snapshot(
                    owner=owner,
                    terminal_id=terminal_id,
                    after_sequence=after_sequence,
                    limit=snapshot_limit,
                )
            )
            subscription = ArtifactSubscription(snapshot, self, key, queue, loop)
            self._subscribers.setdefault(key, set()).add(subscription)
        return subscription

    @staticmethod
    def _enqueue(
        subscription: ArtifactSubscription, event: ArtifactEvent
    ) -> None:
        if subscription._closed:
            return
        if subscription._queue.full():
            try:
                subscription._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            subscription._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Persisted history is authoritative; a slow live consumer can
            # reconnect with its last sequence rather than grow memory.
            pass

    def _publish(self, event: ArtifactEvent) -> None:
        key = (event.owner, event.terminal_id)
        stale: list[ArtifactSubscription] = []
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for subscription in tuple(self._subscribers.get(key, ())):
            if subscription._closed or subscription._loop.is_closed():
                stale.append(subscription)
                continue
            try:
                if subscription._loop is current_loop:
                    self._enqueue(subscription, event)
                else:
                    subscription._loop.call_soon_threadsafe(
                        self._enqueue, subscription, event
                    )
            except RuntimeError:
                stale.append(subscription)
        for subscription in stale:
            self._unsubscribe(subscription)

    def _unsubscribe(self, subscription: ArtifactSubscription) -> None:
        with self._lock:
            subscribers = self._subscribers.get(subscription._key)
            if not subscribers:
                return
            subscribers.discard(subscription)
            if not subscribers:
                self._subscribers.pop(subscription._key, None)

    def authorized_attachment(
        self, *, owner: str, terminal_id: str, attachment_id: str
    ) -> AttachmentRef | None:
        """Resolve a ref only when this exact scoped event log owns it."""
        self._require_scope(owner, terminal_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attachment_id, attachment_media_type, attachment_size,
                       attachment_width, attachment_height, attachment_name
                FROM artifact_events
                WHERE owner = ? AND terminal_id = ? AND attachment_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (owner, terminal_id, attachment_id),
            ).fetchone()
        if row is None:
            return None
        return AttachmentRef(
            id=row["attachment_id"],
            media_type=row["attachment_media_type"],
            size=int(row["attachment_size"]),
            width=int(row["attachment_width"]),
            height=int(row["attachment_height"]),
            name=row["attachment_name"] or "",
        )


def _load_pillow() -> tuple[Any, type[Exception], type[Warning]]:
    """Load the decoder only for image admission/read paths."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - exercised without dependency
        raise ImageSupportUnavailable(
            "Image support requires Pillow; install the 'Pillow' dependency"
        ) from exc
    return Image, UnidentifiedImageError, Image.DecompressionBombWarning


def _safe_attachment_name(name: str | None) -> str:
    if not name:
        return ""
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in basename if ord(character) >= 32)
    return cleaned[:255]


class AttachmentStore:
    """Local immutable image objects addressed by their SHA-256 digest."""

    def __init__(
        self,
        root: Path,
        *,
        max_image_bytes: int = 5 * 1024 * 1024,
        max_image_pixels: int = 40_000_000,
    ) -> None:
        if max_image_bytes <= 0:
            raise ValueError("max_image_bytes must be positive")
        if max_image_pixels <= 0:
            raise ValueError("max_image_pixels must be positive")
        self.root = Path(root)
        self.objects = self.root / "v1" / "objects"
        self.max_image_bytes = max_image_bytes
        self.max_image_pixels = max_image_pixels
        self.objects.mkdir(parents=True, exist_ok=True, mode=0o700)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            os.chmod(self.root, 0o700)
            os.chmod(self.objects, 0o700)

    def _validate_image(
        self, data: bytes, *, declared_media_type: str | None = None
    ) -> tuple[str, int, int]:
        if not data:
            raise ImageValidationError("image is empty")
        if len(data) > self.max_image_bytes:
            raise ImageValidationError(
                f"image exceeds the {self.max_image_bytes}-byte limit"
            )
        Image, UnidentifiedImageError, bomb_warning = _load_pillow()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", bomb_warning)
                with Image.open(io.BytesIO(data)) as image:
                    image_format = str(image.format or "").upper()
                    media_type = SUPPORTED_IMAGE_MEDIA_TYPES.get(image_format)
                    if media_type is None:
                        raise ImageValidationError(
                            "image must be a real PNG, JPEG, WebP, or GIF"
                        )
                    width, height = image.size
                    if width <= 0 or height <= 0:
                        raise ImageValidationError("image dimensions must be positive")
                    if width * height > self.max_image_pixels:
                        raise ImageValidationError(
                            f"image exceeds the {self.max_image_pixels}-pixel limit"
                        )
                    image.verify()
                # verify() invalidates its decoder. Reopen and decode raster data
                # so a valid header with a truncated body is not admitted.
                with Image.open(io.BytesIO(data)) as decoded:
                    decoded.load()
        except ImageValidationError:
            raise
        except (bomb_warning, Image.DecompressionBombError) as exc:
            raise ImageValidationError(
                f"image exceeds the {self.max_image_pixels}-pixel limit"
            ) from exc
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise ImageValidationError(
                "attachment bytes are not a valid supported image"
            ) from exc
        if declared_media_type and declared_media_type != media_type:
            raise ImageValidationError(
                "declared media type "
                f"{declared_media_type!r} does not match {media_type!r}"
            )
        return media_type, width, height

    def _path_for_id(self, attachment_id: str) -> Path:
        match = ATTACHMENT_ID.fullmatch(attachment_id)
        if not match:
            raise ValueError(
                "attachment id must be 'sha256:' plus 64 lowercase hex digits"
            )
        digest = match.group(1)
        return self.objects / digest[:2] / digest

    def save_image(
        self,
        data: bytes | bytearray | memoryview,
        *,
        declared_media_type: str | None = None,
        name: str | None = None,
    ) -> AttachmentRef:
        raw = bytes(data)
        media_type, width, height = self._validate_image(
            raw, declared_media_type=declared_media_type
        )
        digest = hashlib.sha256(raw).hexdigest()
        attachment_id = f"sha256:{digest}"
        destination = self._path_for_id(attachment_id)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)

        if destination.exists():
            existing = destination.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise AttachmentIntegrityError(
                    f"stored attachment {attachment_id} failed SHA-256 verification"
                )
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.", dir=destination.parent
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    descriptor = -1
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                os.chmod(destination, 0o600)
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

        return AttachmentRef(
            id=attachment_id,
            media_type=media_type,
            size=len(raw),
            width=width,
            height=height,
            name=_safe_attachment_name(name),
        )

    def _read_verified(self, ref: AttachmentRef) -> bytes:
        path = self._path_for_id(ref.id)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise AttachmentIntegrityError(
                f"stored attachment {ref.id} is missing"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if ref.id != f"sha256:{digest}":
            raise AttachmentIntegrityError(
                f"stored attachment {ref.id} failed SHA-256 verification"
            )
        if len(data) != ref.size:
            raise AttachmentIntegrityError(
                f"stored attachment {ref.id} has unexpected byte length"
            )
        try:
            media_type, width, height = self._validate_image(
                data, declared_media_type=ref.media_type
            )
        except ImageValidationError as exc:
            raise AttachmentIntegrityError(
                f"stored attachment {ref.id} failed image verification"
            ) from exc
        if (media_type, width, height) != (ref.media_type, ref.width, ref.height):
            raise AttachmentIntegrityError(
                f"stored attachment {ref.id} metadata does not match its bytes"
            )
        return data

    def read_authorized(
        self,
        event_store: ArtifactEventStore,
        *,
        owner: str,
        terminal_id: str,
        attachment_id: str,
    ) -> AttachmentPayload:
        """Read only through an exact owner+terminal durable-event grant."""
        ref = event_store.authorized_attachment(
            owner=owner,
            terminal_id=terminal_id,
            attachment_id=attachment_id,
        )
        if ref is None:
            raise AttachmentAccessDenied(
                "attachment is not referenced by this owner and terminal"
            )
        return AttachmentPayload(ref=ref, data=self._read_verified(ref))


def build_read_image_result(
    file: WorkspaceFileRef, attachment: AttachmentRef
) -> list[dict[str, object]]:
    """Build replayable attachment content without bytes or access tokens.

    This function is deliberately pure: durable storage and event persistence
    must have succeeded before its returned blocks are inserted into a message.
    """
    envelope = {
        "path": file.path,
        "image": attachment.as_dict(),
    }
    return [
        {
            "type": "text",
            "text": json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
        },
        {
            "type": "image",
            "attachment": attachment.as_dict(),
        },
    ]


def build_openai_responses_image_content(
    file: WorkspaceFileRef, payload: AttachmentPayload
) -> list[dict[str, object]]:
    """Return content blocks an OpenAI Responses adapter can submit directly.

    Unlike :func:`build_read_image_result`, this is an intentionally transient
    bridge: image bytes are embedded in a data URL only in the authenticated
    tool response.  Durable events continue to contain only the content-addressed
    attachment reference.
    """
    digest = hashlib.sha256(payload.data).hexdigest()
    if (
        len(payload.data) != payload.ref.size
        or payload.ref.id != f"sha256:{digest}"
    ):
        raise AttachmentIntegrityError(
            "model image bytes do not match their attachment reference"
        )
    encoded = base64.b64encode(payload.data).decode("ascii")
    data_url = f"data:{payload.ref.media_type};base64,{encoded}"
    return [
        {
            "type": "input_text",
            "text": json.dumps(
                {
                    "path": file.path,
                    "name": file.display_name,
                    "media_type": payload.ref.media_type,
                    "width": payload.ref.width,
                    "height": payload.ref.height,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
        {
            "type": "input_image",
            "image_url": data_url,
        },
    ]
