from __future__ import annotations

import base64
import binascii
import codecs
import contextlib
import errno
import hashlib
import heapq
import json
import mimetypes
import os
import posixpath
import re
import secrets
import stat as stat_module
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Literal, Mapping
from urllib.parse import quote


WorkspaceEntryKind = Literal["file", "directory", "symlink", "other"]
WorkspacePreviewKind = Literal["image", "text", "pdf", "download"]


class WorkspaceError(Exception):
    """Base error raised by the workspace capability."""

    code = "WORKSPACE_ERROR"


class WorkspaceConfigurationError(WorkspaceError):
    code = "WORKSPACE_CONFIGURATION_ERROR"


class WorkspaceNotFound(WorkspaceError):
    code = "WORKSPACE_NOT_FOUND"


class WorkspaceAccessDenied(WorkspaceError):
    code = "WORKSPACE_ACCESS_DENIED"


class WorkspaceNotDirectory(WorkspaceError):
    code = "WORKSPACE_NOT_DIRECTORY"


class WorkspaceNotFile(WorkspaceError):
    code = "WORKSPACE_NOT_FILE"


class WorkspaceTooLarge(WorkspaceError):
    code = "WORKSPACE_TOO_LARGE"


class WorkspaceInvalidRange(WorkspaceError):
    code = "WORKSPACE_INVALID_RANGE"

    def __init__(self, message: str, *, total: int | None = None) -> None:
        super().__init__(message)
        self.total = total


class WorkspaceFileChanged(WorkspaceError):
    code = "WORKSPACE_FILE_CHANGED"


class WorkspaceGrantNotFound(WorkspaceError):
    code = "WORKSPACE_GRANT_NOT_FOUND"


class WorkspaceUnavailable(WorkspaceError):
    code = "WORKSPACE_UNAVAILABLE"


@dataclass(frozen=True)
class WorkspaceEntry:
    """Stable metadata for one root-relative workspace entry."""

    path: str
    name: str
    kind: WorkspaceEntryKind
    size: int
    modified_at: float
    etag: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "modified_at": self.modified_at,
            "etag": self.etag,
        }


@dataclass(frozen=True)
class WorkspaceRead:
    """One bounded, internally version-checked byte window."""

    entry: WorkspaceEntry
    data: bytes
    start: int
    end: int

    @property
    def total(self) -> int:
        return self.entry.size


@dataclass(frozen=True)
class WorkspaceDirectoryPage:
    """One stable, bounded page of a directory listing."""

    directory: WorkspaceEntry
    entries: tuple[WorkspaceEntry, ...]
    revision: str
    next_cursor: str | None


@dataclass(frozen=True)
class WorkspaceBinding:
    """Public description of an owner/terminal workspace binding."""

    id: str
    owner: str
    terminal_id: str
    root: str
    provider_kind: str
    created_at: float

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "owner": self.owner,
            "terminal_id": self.terminal_id,
            "root": self.root,
            "provider_kind": self.provider_kind,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class FileGrant:
    """Short-lived opaque authorization for a version-checked live file.

    This is not a byte snapshot. In particular, SFTP v3 cannot expose a
    cryptographic file version; the service combines its weak metadata ETag
    with a bounded prefix probe and revalidates both on reads.
    """

    id: str
    owner: str
    terminal_id: str
    path: str
    name: str
    media_type: str
    preview_kind: WorkspacePreviewKind
    inline_safe: bool
    size: int
    modified_at: float
    etag: str
    provider_etag: str
    created_at: float
    expires_at: float
    image_width: int | None = None
    image_height: int | None = None
    _binding_id: str = field(default="", repr=False, compare=False)
    _probe_hash: str = field(default="", repr=False, compare=False)
    _probe_bytes: int = field(default=0, repr=False, compare=False)

    @property
    def version(self) -> str:
        """Compatibility name used by artifact/file API payloads."""

        return self.etag

    @property
    def preview_mode(self) -> WorkspacePreviewKind:
        """Compatibility name used by the browser preview surface."""

        return self.preview_kind

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id,
            "owner": self.owner,
            "terminal_id": self.terminal_id,
            "path": self.path,
            "name": self.name,
            "media_type": self.media_type,
            "kind": "file",
            "preview_kind": self.preview_kind,
            "preview_mode": self.preview_mode,
            "inline_safe": self.inline_safe,
            "size": self.size,
            "modified_at": self.modified_at,
            "etag": self.etag,
            "provider_etag": self.provider_etag,
            "version": self.version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if self.image_width is not None and self.image_height is not None:
            result["width"] = self.image_width
            result["height"] = self.image_height
        return result


@dataclass(frozen=True)
class GrantedFileResponse:
    """Transport-neutral response data ready for a FastAPI Response."""

    status_code: int
    headers: dict[str, str]
    body: bytes
    grant: FileGrant


class WorkspaceProvider(ABC):
    """Read-only filesystem provider for one explicit workspace root."""

    kind = "abstract"

    @property
    @abstractmethod
    def root(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def stat(self, path: str = ".") -> WorkspaceEntry:
        raise NotImplementedError

    @abstractmethod
    def list(
        self, path: str = ".", *, max_entries: int = 1_000
    ) -> tuple[WorkspaceEntry, ...]:
        raise NotImplementedError

    @abstractmethod
    def read_range(
        self,
        path: str,
        *,
        start: int = 0,
        end: int | None = None,
        max_bytes: int,
    ) -> WorkspaceRead:
        """Read ``[start, end)`` without ever buffering over ``max_bytes``."""

        raise NotImplementedError

    def list_page(
        self,
        path: str = ".",
        *,
        after: tuple[int, str, str] | None = None,
        limit: int = 200,
    ) -> tuple[tuple[WorkspaceEntry, ...], bool]:
        """Return a directory-first page.

        Providers with streaming directory APIs override this method so the
        page remains memory bounded. The compatibility implementation keeps
        third-party/test providers working.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        entries = self.list(path, max_entries=max(1_000, limit + 1))
        return _select_entry_page(entries, after=after, limit=limit)

    def close(self) -> None:
        """Release provider resources; local/stateless providers may do nothing."""


def _path_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or "\x00" in value:
        raise WorkspaceAccessDenied("workspace path is invalid")
    if len(value) > 4_096:
        raise WorkspaceAccessDenied("workspace path is too long")
    # Treat both separator styles as separators. That intentionally makes a
    # backslash-containing POSIX filename inaccessible in exchange for one
    # traversal rule across Linux and Windows clients.
    normalized = value.replace("\\", "/")
    if normalized in {"", "."}:
        return ()
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise WorkspaceAccessDenied("workspace paths must be relative to the grant root")
    parts: list[str] = []
    for part in posix.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise WorkspaceAccessDenied("parent traversal is outside the workspace root")
        parts.append(part)
    return tuple(parts)


def _display_path(parts: tuple[str, ...]) -> str:
    return "/".join(parts) if parts else "."


def _entry_kind(mode: int) -> WorkspaceEntryKind:
    if stat_module.S_ISREG(mode):
        return "file"
    if stat_module.S_ISDIR(mode):
        return "directory"
    if stat_module.S_ISLNK(mode):
        return "symlink"
    return "other"


def _entry_sort_key(entry: WorkspaceEntry) -> tuple[int, str, str]:
    rank = {"directory": 0, "file": 1, "symlink": 2, "other": 3}[entry.kind]
    return rank, entry.name.casefold(), entry.name


def _select_entry_page(
    entries: Any,
    *,
    after: tuple[int, str, str] | None,
    limit: int,
) -> tuple[tuple[WorkspaceEntry, ...], bool]:
    candidates = (entry for entry in entries if after is None or _entry_sort_key(entry) > after)
    selected = heapq.nsmallest(limit + 1, candidates, key=_entry_sort_key)
    has_more = len(selected) > limit
    return tuple(selected[:limit]), has_more


def _encode_directory_cursor(
    path: str,
    revision: str,
    entry: WorkspaceEntry,
) -> str:
    payload = json.dumps(
        {"v": 1, "p": path, "r": revision, "k": list(_entry_sort_key(entry))},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_directory_cursor(
    value: str,
    *,
    path: str,
    revision: str,
) -> tuple[int, str, str]:
    if not value or len(value) > 8_192:
        raise WorkspaceAccessDenied("workspace cursor is invalid")
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        key = payload["k"]
        decoded = (int(key[0]), str(key[1]), str(key[2]))
        if (
            payload.get("v") != 1
            or payload.get("p") != path
            or payload.get("r") != revision
            or decoded[0] not in {0, 1, 2, 3}
            or len(decoded[1]) > 4_096
            or len(decoded[2]) > 4_096
        ):
            raise ValueError
        return decoded
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeError,
    ) as exc:
        raise WorkspaceFileChanged(
            "directory changed while its paginated listing was open"
        ) from exc


def _weak_etag(*values: object) -> str:
    digest = hashlib.sha256("\x00".join(str(value) for value in values).encode()).hexdigest()
    return f'W/"{digest[:32]}"'


def _probe_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _grant_etag(
    provider_etag: str, size: int, probe_hash: str, probe_bytes: int
) -> str:
    """Version exposed by a grant, strengthened with bounded file content."""

    return _weak_etag(
        "workspace-grant-v1", provider_etag, size, probe_bytes, probe_hash
    )


def _window(total: int, start: int, end: int | None, max_bytes: int) -> tuple[int, int]:
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if start < 0 or (end is not None and end < start):
        raise WorkspaceInvalidRange("invalid byte range", total=total)
    if start > total:
        raise WorkspaceInvalidRange(
            "byte range starts beyond end of file", total=total
        )
    stop = total if end is None else min(end, total)
    if stop - start > max_bytes:
        raise WorkspaceTooLarge(
            f"requested byte range exceeds the {max_bytes}-byte response limit"
        )
    return start, stop


def _raise_local_error(error: OSError, path: str) -> None:
    if error.errno == errno.ENOENT:
        raise WorkspaceNotFound(f'workspace path "{path}" was not found') from error
    if error.errno in {errno.ELOOP, errno.EACCES, errno.EPERM}:
        raise WorkspaceAccessDenied(
            f'workspace path "{path}" is not accessible; symbolic links are not followed'
        ) from error
    if error.errno == errno.ENOTDIR:
        raise WorkspaceNotDirectory(f'workspace path "{path}" is not a directory') from error
    raise WorkspaceUnavailable(f'unable to access workspace path "{path}"') from error


class LocalWorkspaceProvider(WorkspaceProvider):
    """Descriptor-rooted local provider that never follows workspace symlinks."""

    kind = "local"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        requested = Path(root).expanduser()
        try:
            canonical = requested.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceConfigurationError(
                f"local workspace root does not exist: {requested}"
            ) from exc
        if not canonical.is_dir():
            raise WorkspaceConfigurationError(
                f"local workspace root is not a directory: {canonical}"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            self._root_fd = os.open(canonical, flags)
        except OSError as exc:
            raise WorkspaceConfigurationError(
                f"local workspace root cannot be opened: {canonical}"
            ) from exc
        self._root = canonical
        self._closed = False
        self._lock = threading.RLock()

    @property
    def root(self) -> str:
        return str(self._root)

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkspaceUnavailable("local workspace provider is closed")

    def _ensure_contained(self, parts: tuple[str, ...]) -> None:
        try:
            canonical = self._root.joinpath(*parts).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceAccessDenied("workspace path cannot be resolved safely") from exc
        if canonical != self._root and self._root not in canonical.parents:
            raise WorkspaceAccessDenied("workspace path resolves outside the workspace root")

    def _open_directory(self, parts: tuple[str, ...]) -> int:
        self._ensure_open()
        self._ensure_contained(parts)
        descriptor = os.dup(self._root_fd)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for part in parts:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except OSError as exc:
            os.close(descriptor)
            _raise_local_error(exc, _display_path(parts))
            raise AssertionError("unreachable")

    def _lstat(self, parts: tuple[str, ...]) -> os.stat_result:
        if not parts:
            self._ensure_open()
            return os.fstat(self._root_fd)
        self._ensure_contained(parts)
        parent = self._open_directory(parts[:-1])
        try:
            return os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            _raise_local_error(exc, _display_path(parts))
            raise AssertionError("unreachable")
        finally:
            os.close(parent)

    def _entry(self, parts: tuple[str, ...], info: os.stat_result) -> WorkspaceEntry:
        name = parts[-1] if parts else (self._root.name or "/")
        modified_ns = getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))
        changed_ns = getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))
        return WorkspaceEntry(
            path=_display_path(parts),
            name=name,
            kind=_entry_kind(info.st_mode),
            size=max(0, int(info.st_size)),
            modified_at=modified_ns / 1_000_000_000,
            etag=_weak_etag(
                "local",
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                modified_ns,
                changed_ns,
            ),
        )

    def stat(self, path: str = ".") -> WorkspaceEntry:
        parts = _path_parts(path)
        with self._lock:
            return self._entry(parts, self._lstat(parts))

    def list(
        self, path: str = ".", *, max_entries: int = 1_000
    ) -> tuple[WorkspaceEntry, ...]:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        parts = _path_parts(path)
        with self._lock:
            descriptor = self._open_directory(parts)
            entries: list[WorkspaceEntry] = []
            try:
                with os.scandir(descriptor) as scanner:
                    for item in scanner:
                        if len(entries) >= max_entries:
                            raise WorkspaceTooLarge(
                                f'directory "{_display_path(parts)}" exceeds the '
                                f"{max_entries}-entry listing limit"
                            )
                        try:
                            info = item.stat(follow_symlinks=False)
                        except FileNotFoundError as exc:
                            raise WorkspaceFileChanged(
                                "directory changed while it was being listed"
                            ) from exc
                        entries.append(self._entry((*parts, item.name), info))
            finally:
                os.close(descriptor)
        entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        return tuple(entries)

    def list_page(
        self,
        path: str = ".",
        *,
        after: tuple[int, str, str] | None = None,
        limit: int = 200,
    ) -> tuple[tuple[WorkspaceEntry, ...], bool]:
        if limit < 1:
            raise ValueError("limit must be positive")
        parts = _path_parts(path)
        with self._lock:
            descriptor = self._open_directory(parts)
            try:
                with os.scandir(descriptor) as scanner:
                    return _select_entry_page(
                        (
                            self._entry((*parts, item.name), item.stat(follow_symlinks=False))
                            for item in scanner
                        ),
                        after=after,
                        limit=limit,
                    )
            except FileNotFoundError as exc:
                raise WorkspaceFileChanged(
                    "directory changed while it was being listed"
                ) from exc
            except OSError as exc:
                _raise_local_error(exc, _display_path(parts))
                raise AssertionError("unreachable")
            finally:
                os.close(descriptor)

    def read_range(
        self,
        path: str,
        *,
        start: int = 0,
        end: int | None = None,
        max_bytes: int,
    ) -> WorkspaceRead:
        parts = _path_parts(path)
        if not parts:
            raise WorkspaceNotFile("workspace root is not a regular file")
        with self._lock:
            self._ensure_contained(parts)
            parent = self._open_directory(parts[:-1])
            descriptor = -1
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(parts[-1], flags, dir_fd=parent)
                before = os.fstat(descriptor)
                if not stat_module.S_ISREG(before.st_mode):
                    raise WorkspaceNotFile(
                        f'workspace path "{_display_path(parts)}" is not a regular file'
                    )
                first, stop = _window(int(before.st_size), start, end, max_bytes)
                chunks: list[bytes] = []
                offset = first
                while offset < stop:
                    chunk = os.pread(descriptor, min(64 * 1024, stop - offset), offset)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    offset += len(chunk)
                after = os.fstat(descriptor)
                before_signature = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    getattr(before, "st_mtime_ns", int(before.st_mtime * 1_000_000_000)),
                    getattr(before, "st_ctime_ns", int(before.st_ctime * 1_000_000_000)),
                )
                after_signature = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    getattr(after, "st_mtime_ns", int(after.st_mtime * 1_000_000_000)),
                    getattr(after, "st_ctime_ns", int(after.st_ctime * 1_000_000_000)),
                )
                if offset != stop or before_signature != after_signature:
                    raise WorkspaceFileChanged("file changed while it was being read")
                return WorkspaceRead(
                    entry=self._entry(parts, after),
                    data=b"".join(chunks),
                    start=first,
                    end=stop,
                )
            except OSError as exc:
                _raise_local_error(exc, _display_path(parts))
                raise AssertionError("unreachable")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                os.close(parent)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            os.close(self._root_fd)
            self._closed = True


class SftpWorkspaceProvider(WorkspaceProvider):
    """Read-only SFTP provider over the same SSH identity used by terminals."""

    kind = "sftp"

    def __init__(
        self,
        root: str,
        *,
        host: str,
        port: int,
        username: str,
        private_key: str | os.PathLike[str],
        known_hosts: str | os.PathLike[str],
        strict_host_key: str = "accept-new",
        connect_timeout: float = 10,
        operation_timeout: float = 15,
        _client_factory: Callable[[], tuple[Any, Any]] | None = None,
    ) -> None:
        if root == "":
            raise WorkspaceConfigurationError("SFTP workspace root is required")
        if not host.strip() or not username.strip() or not 1 <= int(port) <= 65_535:
            raise WorkspaceConfigurationError("SFTP connection settings are invalid")
        if strict_host_key not in {"yes", "accept-new", "no"}:
            raise WorkspaceConfigurationError(
                "SSH_STRICT_HOST_KEY must be yes, accept-new, or no"
            )
        if connect_timeout <= 0 or operation_timeout <= 0:
            raise WorkspaceConfigurationError("SFTP timeouts must be positive")
        self._configured_root = root
        self.host = host.strip()
        self.port = int(port)
        self.username = username.strip()
        self.private_key = Path(private_key).expanduser()
        self.known_hosts = Path(known_hosts).expanduser()
        self.strict_host_key = strict_host_key
        self.connect_timeout = connect_timeout
        self.operation_timeout = operation_timeout
        self._client_factory = _client_factory
        self._ssh: Any | None = None
        self._sftp: Any | None = None
        self._canonical_root: str | None = None
        self._closed = False
        self._lock = threading.RLock()

    @classmethod
    def from_device(
        cls,
        root: str,
        device: Mapping[str, object],
        *,
        data_dir: str | os.PathLike[str],
        environ: Mapping[str, str] | None = None,
    ) -> "SftpWorkspaceProvider":
        env = os.environ if environ is None else environ
        private_key = env.get("SSH_PRIVATE_KEY", "").strip()
        if not private_key:
            raise WorkspaceConfigurationError("SSH_PRIVATE_KEY is not configured")
        known_hosts = env.get("SSH_KNOWN_HOSTS", "").strip()
        if not known_hosts:
            known_hosts = str(Path(data_dir) / "ssh_known_hosts")
        try:
            port = int(device["remote_port"])
            username = str(device["ssh_user"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceConfigurationError(
                "device is missing remote_port or ssh_user"
            ) from exc
        try:
            connect_timeout = float(env.get("SSH_CONNECT_TIMEOUT", "10"))
            operation_timeout = float(env.get("SFTP_OPERATION_TIMEOUT", "15"))
        except ValueError as exc:
            raise WorkspaceConfigurationError(
                "SSH_CONNECT_TIMEOUT and SFTP_OPERATION_TIMEOUT must be numbers"
            ) from exc
        return cls(
            root,
            host=env.get("FRP_PROXY_HOST", "127.0.0.1"),
            port=port,
            username=username,
            private_key=private_key,
            known_hosts=known_hosts,
            strict_host_key=env.get("SSH_STRICT_HOST_KEY", "accept-new"),
            connect_timeout=connect_timeout,
            operation_timeout=operation_timeout,
        )

    @property
    def root(self) -> str:
        return self._canonical_root or self._configured_root

    def _connect(self) -> tuple[Any, Any]:
        if self._client_factory is not None:
            return self._client_factory()
        # Paramiko is deliberately imported only when an SFTP workspace is
        # first used. Local-only deployments can import/start AgentServer even
        # when the optional transport is unavailable.
        try:
            import paramiko  # type: ignore[import-not-found]
        except ImportError as exc:
            raise WorkspaceUnavailable(
                "SFTP support requires the paramiko package"
            ) from exc
        if not self.private_key.is_file():
            raise WorkspaceConfigurationError(
                f"SSH_PRIVATE_KEY does not exist: {self.private_key}"
            )
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self.known_hosts.touch(mode=0o600, exist_ok=True)
        with contextlib.suppress(OSError):
            self.known_hosts.chmod(0o600)
        client = paramiko.SSHClient()
        client.load_host_keys(str(self.known_hosts))
        if self.strict_host_key == "yes":
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # AutoAddPolicy accepts only a missing key. Paramiko still raises
            # BadHostKeyException when a pinned key changes.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                key_filename=str(self.private_key),
                allow_agent=False,
                look_for_keys=False,
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
            )
            return client, client.open_sftp()
        except Exception:
            client.close()
            raise

    def _ensure_connected(self) -> Any:
        if self._closed:
            raise WorkspaceUnavailable("SFTP workspace provider is closed")
        if self._sftp is not None:
            return self._sftp
        try:
            self._ssh, self._sftp = self._connect()
            channel = self._sftp.get_channel()
            channel.settimeout(self.operation_timeout)
            canonical = str(self._sftp.normalize(self._configured_root))
            info = self._sftp.stat(canonical)
            if not stat_module.S_ISDIR(int(info.st_mode)):
                raise WorkspaceConfigurationError(
                    f"SFTP workspace root is not a directory: {self._configured_root}"
                )
            self._canonical_root = posixpath.normpath(canonical)
            return self._sftp
        except WorkspaceError:
            self._disconnect()
            raise
        except Exception as exc:
            self._disconnect()
            raise WorkspaceUnavailable("unable to establish the SFTP workspace") from exc

    def _disconnect(self) -> None:
        sftp, ssh = self._sftp, self._ssh
        self._sftp = None
        self._ssh = None
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass

    def _raise_remote(self, error: Exception, path: str) -> None:
        number = getattr(error, "errno", None)
        if number == errno.ENOENT:
            raise WorkspaceNotFound(f'workspace path "{path}" was not found') from error
        if number in {errno.EACCES, errno.EPERM}:
            raise WorkspaceAccessDenied(
                f'workspace path "{path}" is not accessible'
            ) from error
        if number == errno.ENOTDIR:
            raise WorkspaceNotDirectory(
                f'workspace path "{path}" is not a directory'
            ) from error
        # Paramiko channels are not reliably reusable after EOF, timeout,
        # connection reset, or an otherwise unclassified operation failure.
        # Drop both SFTP and SSH handles so the next request reconnects rather
        # than repeatedly hitting the poisoned channel.
        self._disconnect()
        raise WorkspaceUnavailable(f'unable to access workspace path "{path}"') from error

    def _resolve(self, parts: tuple[str, ...]) -> tuple[Any, str]:
        sftp = self._ensure_connected()
        root = self._canonical_root
        if root is None:
            raise WorkspaceUnavailable("SFTP workspace root was not resolved")
        candidate = posixpath.join(root, *parts)
        current = root
        try:
            # Reject every user-addressable symlink. This prevents ordinary
            # symlink escapes; SFTP v3 has no openat/O_NOFOLLOW equivalent, so
            # the configured root itself must remain a trusted directory.
            for part in parts:
                current = posixpath.join(current, part)
                if stat_module.S_ISLNK(int(sftp.lstat(current).st_mode)):
                    raise WorkspaceAccessDenied(
                        "symbolic links are not followed in SFTP workspaces"
                    )
            canonical = posixpath.normpath(str(sftp.normalize(candidate)))
        except WorkspaceError:
            raise
        except Exception as exc:
            self._raise_remote(exc, _display_path(parts))
            raise AssertionError("unreachable")
        try:
            contained = posixpath.commonpath((root, canonical)) == root
        except ValueError:
            contained = False
        if not contained:
            raise WorkspaceAccessDenied("workspace path resolves outside the workspace root")
        return sftp, canonical

    def _entry(self, parts: tuple[str, ...], info: Any) -> WorkspaceEntry:
        mode = int(info.st_mode)
        size = max(0, int(getattr(info, "st_size", 0) or 0))
        modified = float(getattr(info, "st_mtime", 0) or 0)
        return WorkspaceEntry(
            path=_display_path(parts),
            name=parts[-1] if parts else posixpath.basename(self.root.rstrip("/")) or "/",
            kind=_entry_kind(mode),
            size=size,
            modified_at=modified,
            # SFTP v3 normally exposes only second-resolution mtime, size,
            # ownership and mode—no inode/change time. This is intentionally a
            # weak provider version; WorkspaceService mixes in a bounded probe
            # hash for grants and revalidates that prefix on every read.
            etag=_weak_etag(
                "sftp",
                _display_path(parts),
                mode,
                size,
                int(modified),
                getattr(info, "st_uid", None),
                getattr(info, "st_gid", None),
            ),
        )

    def stat(self, path: str = ".") -> WorkspaceEntry:
        parts = _path_parts(path)
        with self._lock:
            sftp, canonical = self._resolve(parts)
            try:
                return self._entry(parts, sftp.lstat(canonical))
            except Exception as exc:
                self._raise_remote(exc, _display_path(parts))
                raise AssertionError("unreachable")

    def list(
        self, path: str = ".", *, max_entries: int = 1_000
    ) -> tuple[WorkspaceEntry, ...]:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        parts = _path_parts(path)
        with self._lock:
            sftp, canonical = self._resolve(parts)
            iterator: Any | None = None
            try:
                listdir_iter = getattr(sftp, "listdir_iter", None)
                if callable(listdir_iter):
                    # Paramiko's generator bounds client memory. One read-ahead
                    # batch plus max_entries+1 is enough to decide whether the
                    # directory can be represented; never materialize the
                    # complete remote listing merely to reject it.
                    iterator = iter(listdir_iter(canonical, read_aheads=1))
                else:
                    # Compatibility fallback for non-Paramiko test doubles or
                    # older SFTP implementations without the streaming API.
                    iterator = iter(sftp.listdir_attr(canonical))
                entries: list[WorkspaceEntry] = []
                for value in iterator:
                    if len(entries) >= max_entries:
                        raise WorkspaceTooLarge(
                            f'directory "{_display_path(parts)}" exceeds the '
                            f"{max_entries}-entry listing limit"
                        )
                    entries.append(
                        self._entry((*parts, str(value.filename)), value)
                    )
            except WorkspaceError:
                raise
            except Exception as exc:
                self._raise_remote(exc, _display_path(parts))
                raise AssertionError("unreachable")
            finally:
                close_iterator = getattr(iterator, "close", None)
                if callable(close_iterator):
                    try:
                        close_iterator()
                    except Exception:
                        self._disconnect()
        entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        return tuple(entries)

    def list_page(
        self,
        path: str = ".",
        *,
        after: tuple[int, str, str] | None = None,
        limit: int = 200,
    ) -> tuple[tuple[WorkspaceEntry, ...], bool]:
        if limit < 1:
            raise ValueError("limit must be positive")
        parts = _path_parts(path)
        with self._lock:
            sftp, canonical = self._resolve(parts)
            iterator: Any | None = None
            try:
                listdir_iter = getattr(sftp, "listdir_iter", None)
                if callable(listdir_iter):
                    iterator = iter(listdir_iter(canonical, read_aheads=1))
                else:
                    iterator = iter(sftp.listdir_attr(canonical))
                return _select_entry_page(
                    (
                        self._entry((*parts, str(value.filename)), value)
                        for value in iterator
                    ),
                    after=after,
                    limit=limit,
                )
            except WorkspaceError:
                raise
            except Exception as exc:
                self._raise_remote(exc, _display_path(parts))
                raise AssertionError("unreachable")
            finally:
                close_iterator = getattr(iterator, "close", None)
                if callable(close_iterator):
                    try:
                        close_iterator()
                    except Exception:
                        self._disconnect()

    def read_range(
        self,
        path: str,
        *,
        start: int = 0,
        end: int | None = None,
        max_bytes: int,
    ) -> WorkspaceRead:
        parts = _path_parts(path)
        if not parts:
            raise WorkspaceNotFile("workspace root is not a regular file")
        with self._lock:
            sftp, canonical = self._resolve(parts)
            handle = None
            try:
                handle = sftp.open(canonical, "rb")
                before = handle.stat()
                if not stat_module.S_ISREG(int(before.st_mode)):
                    raise WorkspaceNotFile(
                        f'workspace path "{_display_path(parts)}" is not a regular file'
                    )
                first, stop = _window(int(before.st_size), start, end, max_bytes)
                handle.seek(first)
                chunks: list[bytes] = []
                offset = first
                while offset < stop:
                    chunk = handle.read(min(64 * 1024, stop - offset))
                    if not chunk:
                        self._disconnect()
                        raise WorkspaceUnavailable(
                            "SFTP stream ended before the requested byte range completed"
                        )
                    binary = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                    chunks.append(binary)
                    offset += len(binary)
                after = handle.stat()
                before_signature = (
                    int(before.st_mode),
                    int(before.st_size),
                    int(getattr(before, "st_mtime", 0) or 0),
                )
                after_signature = (
                    int(after.st_mode),
                    int(after.st_size),
                    int(getattr(after, "st_mtime", 0) or 0),
                )
                if offset != stop or before_signature != after_signature:
                    raise WorkspaceFileChanged("file changed while it was being read")
                # Re-resolve after opening to catch an ordinary ancestor swap.
                _sftp, refreshed = self._resolve(parts)
                if refreshed != canonical:
                    raise WorkspaceFileChanged("file path changed while it was being read")
                return WorkspaceRead(
                    entry=self._entry(parts, after),
                    data=b"".join(chunks),
                    start=first,
                    end=stop,
                )
            except WorkspaceError:
                raise
            except Exception as exc:
                self._raise_remote(exc, _display_path(parts))
                raise AssertionError("unreachable")
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        self._disconnect()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._disconnect()
            self._closed = True


@dataclass
class _BindingState:
    public: WorkspaceBinding
    provider: WorkspaceProvider
    lock: threading.RLock = field(default_factory=threading.RLock)
    closed: bool = False


_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class WorkspaceService:
    """Owner-scoped workspace registry and short-lived file grant manager."""

    def __init__(
        self,
        *,
        grant_ttl: float = 120,
        max_file_bytes: int = 32 * 1024 * 1024,
        max_read_bytes: int = 8 * 1024 * 1024,
        max_list_entries: int = 1_000,
        sniff_bytes: int = 256 * 1024,
        max_image_pixels: int = 40_000_000,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if grant_ttl <= 0:
            raise ValueError("grant_ttl must be positive")
        for name, value in {
            "max_file_bytes": max_file_bytes,
            "max_read_bytes": max_read_bytes,
            "max_list_entries": max_list_entries,
            "sniff_bytes": sniff_bytes,
            "max_image_pixels": max_image_pixels,
        }.items():
            if value < 1:
                raise ValueError(f"{name} must be positive")
        self.grant_ttl = grant_ttl
        self.max_file_bytes = max_file_bytes
        self.max_read_bytes = max_read_bytes
        self.max_list_entries = max_list_entries
        self.sniff_bytes = min(sniff_bytes, max_file_bytes)
        self.max_image_pixels = max_image_pixels
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._bindings: dict[tuple[str, str], _BindingState] = {}
        self._grants: dict[str, FileGrant] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _key(owner: str, terminal_id: str) -> tuple[str, str]:
        owner = owner.strip()
        terminal_id = terminal_id.strip()
        if not owner or not terminal_id:
            raise WorkspaceAccessDenied("owner and terminal_id are required")
        return owner, terminal_id

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkspaceUnavailable("workspace service is closed")

    def bind(
        self, owner: str, terminal_id: str, provider: WorkspaceProvider
    ) -> WorkspaceBinding:
        key = self._key(owner, terminal_id)
        now = self._clock()
        public = WorkspaceBinding(
            id=self._token_factory(),
            owner=key[0],
            terminal_id=key[1],
            root=provider.root,
            provider_kind=provider.kind,
            created_at=now,
        )
        replacement = _BindingState(public=public, provider=provider)
        with self._lock:
            self._ensure_open()
            previous = self._bindings.get(key)
            self._bindings[key] = replacement
            self._grants = {
                token: grant
                for token, grant in self._grants.items()
                if not (grant.owner == key[0] and grant.terminal_id == key[1])
            }
        if previous is not None:
            with previous.lock:
                previous.closed = True
                previous.provider.close()
        return public

    def binding(self, owner: str, terminal_id: str) -> WorkspaceBinding:
        state = self._binding_state(owner, terminal_id)
        with state.lock:
            return state.public

    @staticmethod
    def _refresh_binding_root(state: _BindingState) -> None:
        root = state.provider.root
        if root != state.public.root:
            # SFTP roots are normalized lazily on the first connection. Keep
            # the binding id and creation time stable while publishing the
            # canonical remote root to subsequent API reads.
            state.public = replace(state.public, root=root)

    def _binding_state(self, owner: str, terminal_id: str) -> _BindingState:
        key = self._key(owner, terminal_id)
        with self._lock:
            self._ensure_open()
            state = self._bindings.get(key)
        if state is None or state.closed:
            raise WorkspaceNotFound("terminal has no workspace binding")
        return state

    def unbind(self, owner: str, terminal_id: str) -> bool:
        key = self._key(owner, terminal_id)
        with self._lock:
            state = self._bindings.pop(key, None)
            self._grants = {
                token: grant
                for token, grant in self._grants.items()
                if not (grant.owner == key[0] and grant.terminal_id == key[1])
            }
        if state is None:
            return False
        with state.lock:
            state.closed = True
            state.provider.close()
        return True

    def list(
        self, owner: str, terminal_id: str, path: str = "."
    ) -> tuple[WorkspaceEntry, ...]:
        state = self._binding_state(owner, terminal_id)
        with state.lock:
            if state.closed:
                raise WorkspaceNotFound("terminal has no workspace binding")
            entries = state.provider.list(path, max_entries=self.max_list_entries)
            self._refresh_binding_root(state)
            return entries

    def list_page(
        self,
        owner: str,
        terminal_id: str,
        path: str = ".",
        *,
        cursor: str | None = None,
        limit: int = 200,
        expected_revision: str | None = None,
    ) -> WorkspaceDirectoryPage:
        if limit < 1 or limit > self.max_list_entries:
            raise WorkspaceTooLarge(
                f"directory page size must be between 1 and {self.max_list_entries}"
            )
        state = self._binding_state(owner, terminal_id)
        with state.lock:
            if state.closed:
                raise WorkspaceNotFound("terminal has no workspace binding")
            directory = state.provider.stat(path)
            if directory.kind != "directory":
                raise WorkspaceNotDirectory(
                    f'workspace path "{directory.path}" is not a directory'
                )
            revision = directory.etag
            if expected_revision is not None and expected_revision != revision:
                raise WorkspaceFileChanged(
                    "directory changed while its paginated listing was open"
                )
            after = (
                _decode_directory_cursor(
                    cursor,
                    path=directory.path,
                    revision=revision,
                )
                if cursor
                else None
            )
            entries, has_more = state.provider.list_page(
                directory.path,
                after=after,
                limit=limit,
            )
            refreshed = state.provider.stat(directory.path)
            if refreshed.kind != "directory" or refreshed.etag != revision:
                raise WorkspaceFileChanged(
                    "directory changed while it was being listed"
                )
            self._refresh_binding_root(state)
        next_cursor = (
            _encode_directory_cursor(directory.path, revision, entries[-1])
            if has_more and entries
            else None
        )
        return WorkspaceDirectoryPage(
            directory=refreshed,
            entries=entries,
            revision=revision,
            next_cursor=next_cursor,
        )

    def stat(self, owner: str, terminal_id: str, path: str) -> WorkspaceEntry:
        state = self._binding_state(owner, terminal_id)
        with state.lock:
            if state.closed:
                raise WorkspaceNotFound("terminal has no workspace binding")
            entry = state.provider.stat(path)
            self._refresh_binding_root(state)
            return entry

    def grant(self, owner: str, terminal_id: str, path: str) -> FileGrant:
        state = self._binding_state(owner, terminal_id)
        with state.lock:
            if state.closed:
                raise WorkspaceNotFound("terminal has no workspace binding")
            info = state.provider.stat(path)
            if info.kind != "file":
                raise WorkspaceNotFile(f'workspace path "{info.path}" is not a file')
            if info.size > self.max_file_bytes:
                raise WorkspaceTooLarge(
                    f'file "{info.path}" exceeds the {self.max_file_bytes}-byte preview limit'
                )
            if info.size:
                probe = state.provider.read_range(
                    info.path,
                    start=0,
                    end=min(info.size, self.sniff_bytes),
                    max_bytes=self.sniff_bytes,
                )
                if probe.entry.etag != info.etag:
                    raise WorkspaceFileChanged("file changed while its type was inspected")
                sample = probe.data
            else:
                sample = b""
            self._refresh_binding_root(state)
        probe_hash = _probe_digest(sample)
        probe_bytes = len(sample)
        provider_etag = info.etag
        grant_etag = _grant_etag(
            provider_etag, info.size, probe_hash, probe_bytes
        )
        media_type, preview_kind, inline_safe, dimensions = _sniff_file(
            info.name, sample
        )
        width, height = dimensions or (None, None)
        if width is not None and height is not None:
            if width < 1 or height < 1 or width * height > self.max_image_pixels:
                raise WorkspaceTooLarge(
                    f'file "{info.path}" exceeds the {self.max_image_pixels}-pixel image limit'
                )
        elif preview_kind == "image":
            # A raster whose dimensions cannot be established from the bounded
            # probe stays downloadable but is not sent to a browser decoder as
            # an inline preview.
            preview_kind = "download"
            inline_safe = False
        now = self._clock()
        grant = FileGrant(
            id=self._token_factory(),
            owner=state.public.owner,
            terminal_id=state.public.terminal_id,
            path=info.path,
            name=info.name,
            media_type=media_type,
            preview_kind=preview_kind,
            inline_safe=inline_safe,
            size=info.size,
            modified_at=info.modified_at,
            etag=grant_etag,
            provider_etag=provider_etag,
            created_at=now,
            expires_at=now + self.grant_ttl,
            image_width=width,
            image_height=height,
            _binding_id=state.public.id,
            _probe_hash=probe_hash,
            _probe_bytes=probe_bytes,
        )
        with self._lock:
            self._ensure_open()
            current = self._bindings.get((grant.owner, grant.terminal_id))
            if current is not state or current.public.id != grant._binding_id:
                raise WorkspaceGrantNotFound("workspace binding changed")
            self.cleanup_expired(now=now)
            self._grants[grant.id] = grant
        return grant

    def resolve_grant(
        self, grant_id: str, owner: str, terminal_id: str
    ) -> FileGrant:
        key = self._key(owner, terminal_id)
        now = self._clock()
        with self._lock:
            self._ensure_open()
            grant = self._grants.get(grant_id)
            if grant is None:
                raise WorkspaceGrantNotFound("file grant is invalid or expired")
            grant_state = self._bindings.get((grant.owner, grant.terminal_id))
            stale = (
                grant.expires_at <= now
                or grant_state is None
                or grant_state.closed
                or grant_state.public.id != grant._binding_id
            )
            if stale:
                self._grants.pop(grant_id, None)
                raise WorkspaceGrantNotFound("file grant is invalid or expired")
            # A scope mismatch must look exactly like a missing opaque id, but
            # must not let another owner revoke the real owner's grant.
            if grant.owner != key[0] or grant.terminal_id != key[1]:
                raise WorkspaceGrantNotFound("file grant is invalid or expired")
            return grant

    def read(
        self,
        grant_id: str,
        owner: str,
        terminal_id: str,
        *,
        range_header: str | None = None,
        if_none_match: str | None = None,
    ) -> GrantedFileResponse:
        grant = self.resolve_grant(grant_id, owner, terminal_id)
        state = self._binding_state(owner, terminal_id)
        headers = self._headers(grant)
        with state.lock:
            if state.closed or state.public.id != grant._binding_id:
                raise WorkspaceGrantNotFound("workspace binding changed")
            current = state.provider.stat(grant.path)
            if (
                current.kind != "file"
                or current.etag != grant.provider_etag
                or current.size != grant.size
            ):
                raise WorkspaceFileChanged("file changed after the grant was issued")
            # SFTP v3 metadata is typically limited to size and whole-second
            # mtime. Re-read only the bounded grant probe before every response
            # so a replacement cannot retain the grant merely by preserving
            # that weak metadata. This is intentionally not a whole-file hash:
            # changes outside the probe that also preserve SFTP metadata remain
            # a documented transport limitation.
            probe: WorkspaceRead | None = None
            if grant._probe_bytes:
                probe = state.provider.read_range(
                    grant.path,
                    start=0,
                    end=grant._probe_bytes,
                    max_bytes=grant._probe_bytes,
                )
                if (
                    probe.entry.etag != grant.provider_etag
                    or len(probe.data) != grant._probe_bytes
                    or _probe_digest(probe.data) != grant._probe_hash
                ):
                    raise WorkspaceFileChanged("file changed after the grant was issued")

            self._refresh_binding_root(state)
            if if_none_match and _etag_matches(if_none_match, grant.etag):
                headers["Content-Length"] = "0"
                return GrantedFileResponse(304, headers, b"", grant)

            start, end, partial = _parse_range(range_header, grant.size)
            if end - start > self.max_read_bytes:
                raise WorkspaceTooLarge(
                    f"requested response exceeds the {self.max_read_bytes}-byte limit; use a Range request"
                )

            if probe is not None and end <= grant._probe_bytes:
                body = probe.data[start:end]
            else:
                result = state.provider.read_range(
                    grant.path,
                    start=start,
                    end=end,
                    max_bytes=self.max_read_bytes,
                )
                if result.entry.etag != grant.provider_etag:
                    raise WorkspaceFileChanged("file changed after the grant was issued")
                body = result.data
            self._refresh_binding_root(state)
        headers["Content-Length"] = str(len(body))
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end - 1}/{grant.size}"
        return GrantedFileResponse(206 if partial else 200, headers, body, grant)

    @staticmethod
    def _headers(grant: FileGrant) -> dict[str, str]:
        disposition = "inline" if grant.inline_safe else "attachment"
        encoded_name = quote(grant.name, safe="")
        return {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=0, must-revalidate",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}",
            "Content-Type": grant.media_type,
            "Cross-Origin-Resource-Policy": "same-origin",
            "ETag": grant.etag,
            "X-Content-Type-Options": "nosniff",
        }

    def cleanup_expired(self, *, now: float | None = None) -> int:
        current = self._clock() if now is None else now
        with self._lock:
            expired = [
                token for token, grant in self._grants.items() if grant.expires_at <= current
            ]
            for token in expired:
                self._grants.pop(token, None)
        return len(expired)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            states = list(self._bindings.values())
            self._bindings.clear()
            self._grants.clear()
            self._closed = True
        for state in states:
            with state.lock:
                if state.closed:
                    continue
                state.closed = True
                state.provider.close()


def _parse_range(value: str | None, total: int) -> tuple[int, int, bool]:
    if value is None or not value.strip():
        return 0, total, False
    match = _RANGE.fullmatch(value.strip())
    if match is None or "," in value:
        raise WorkspaceInvalidRange("only one bytes range is supported", total=total)
    first, last = match.groups()
    if not first and not last:
        raise WorkspaceInvalidRange("byte range is empty", total=total)
    if total == 0:
        raise WorkspaceInvalidRange(
            "an empty file has no satisfiable byte range", total=total
        )
    if not first:
        length = int(last)
        if length <= 0:
            raise WorkspaceInvalidRange(
                "suffix byte range must be positive", total=total
            )
        return max(0, total - length), total, True
    start = int(first)
    if start >= total:
        raise WorkspaceInvalidRange(
            "byte range starts beyond end of file", total=total
        )
    if not last:
        return start, total, True
    inclusive_end = int(last)
    if inclusive_end < start:
        raise WorkspaceInvalidRange(
            "byte range end precedes its start", total=total
        )
    return start, min(total, inclusive_end + 1), True


def _etag_matches(header: str, etag: str) -> bool:
    def weak_value(value: str) -> str:
        value = value.strip()
        return value[2:] if value.startswith("W/") else value

    return any(
        candidate == "*" or weak_value(candidate) == weak_value(etag)
        for candidate in header.split(",")
    )


_PNG = b"\x89PNG\r\n\x1a\n"


def _image_dimensions(media_type: str, data: bytes) -> tuple[int, int] | None:
    if media_type == "image/png" and len(data) >= 24 and data.startswith(_PNG):
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if media_type == "image/gif" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if media_type == "image/webp" and len(data) >= 30:
        kind = data[12:16]
        if kind == b"VP8X":
            return (
                1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"),
            )
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        if kind == b"VP8L" and data[20:21] == b"/" and len(data) >= 25:
            b0, b1, b2, b3 = data[21:25]
            return 1 + b0 + ((b1 & 0x3F) << 8), 1 + (b1 >> 6) + (b2 << 2) + ((b3 & 0x0F) << 10)
    if media_type == "image/jpeg" and data.startswith(b"\xff\xd8"):
        position = 2
        sof_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while position + 3 < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(data):
                break
            length = int.from_bytes(data[position : position + 2], "big")
            if length < 2 or position + length > len(data):
                break
            if marker in sof_markers and length >= 7:
                return (
                    int.from_bytes(data[position + 5 : position + 7], "big"),
                    int.from_bytes(data[position + 3 : position + 5], "big"),
                )
            position += length
    return None


def _looks_like_text(data: bytes) -> bool:
    if not data:
        return True
    if b"\x00" in data:
        return False
    try:
        # A bounded prefix may end in the middle of one UTF-8 sequence. The
        # incremental decoder validates all complete input while tolerating
        # only that unfinished tail.
        text = codecs.getincrementaldecoder("utf-8-sig")("strict").decode(
            data, final=False
        )
    except UnicodeDecodeError:
        return False
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t\f\b")
    return controls <= max(1, len(text) // 100)


def _sniff_file(
    name: str, data: bytes
) -> tuple[str, WorkspacePreviewKind, bool, tuple[int, int] | None]:
    media_type: str | None = None
    if data.startswith(_PNG):
        media_type = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        media_type = "image/gif"
    elif len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        media_type = "image/webp"
    if media_type is not None:
        return media_type, "image", True, _image_dimensions(media_type, data)

    stripped = data.lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0c\x0d\x20")[:1_024].lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<script", b"<iframe")):
        return "text/html", "text", False, None
    if stripped.startswith(b"<svg") or (b"<svg" in stripped and stripped.startswith(b"<?xml")):
        return "image/svg+xml", "text", False, None
    if data.startswith(b"%PDF-"):
        # Kept non-inline at the byte endpoint. A UI may fetch it into a
        # bounded Blob and hand it to its isolated PDF surface.
        return "application/pdf", "pdf", False, None
    if _looks_like_text(data):
        guessed = mimetypes.guess_type(name, strict=False)[0]
        if guessed is None:
            guessed = "text/plain"
        if guessed.startswith("image/") or guessed in {
            "application/pdf",
            "application/octet-stream",
            "application/zip",
        }:
            guessed = "text/plain"
        return guessed, "text", False, None
    guessed = mimetypes.guess_type(name, strict=False)[0]
    return guessed or "application/octet-stream", "download", False, None


__all__ = [
    "FileGrant",
    "GrantedFileResponse",
    "LocalWorkspaceProvider",
    "SftpWorkspaceProvider",
    "WorkspaceAccessDenied",
    "WorkspaceBinding",
    "WorkspaceConfigurationError",
    "WorkspaceDirectoryPage",
    "WorkspaceEntry",
    "WorkspaceError",
    "WorkspaceFileChanged",
    "WorkspaceGrantNotFound",
    "WorkspaceInvalidRange",
    "WorkspaceNotDirectory",
    "WorkspaceNotFile",
    "WorkspaceNotFound",
    "WorkspaceProvider",
    "WorkspaceRead",
    "WorkspaceService",
    "WorkspaceTooLarge",
    "WorkspaceUnavailable",
]
