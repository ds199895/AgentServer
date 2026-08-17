from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import hmac
import json
import os
import socket
import stat
import struct
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the fail-closed path below.
    fcntl = None  # type: ignore[assignment]

from .events import EventEnvelope, EventScope, Evidence, ProducerRef, new_id
from .models import ProducerMode
from .provider_adapters import ADAPTERS, sanitize_runtime_payload
from .security import ADAPTER_REPORT_CAPABILITY, REPORT_CAPABILITIES, ReporterClaims
from .service import ExecutionService


MAX_CONTROL_MESSAGE_BYTES = 64 * 1024
MAX_PROCESS_ANCESTRY_DEPTH = 256


class ControlProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class PeerCredentials:
    """Kernel-authenticated identity of one local control connection."""

    pid: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ProcessIdentity:
    """One Linux PID incarnation, including fields stable across ``exec``."""

    pid: int
    ppid: int
    process_group_id: int
    session_id: int
    tty_device: int
    start_time_ticks: int
    uid: int


@dataclass(frozen=True)
class LaunchProcessBinding:
    """Server-created binding between a managed launch and its process root."""

    owner_id: str
    terminal_id: str
    launch_id: str
    root: ProcessIdentity


ProcessIdentityReader = Callable[[int], ProcessIdentity | None]


def read_linux_process_identity(pid: int) -> ProcessIdentity | None:
    """Read a PID incarnation without trusting caller-provided process data.

    Opening the proc directory first narrows the PID-reuse race.  If the peer
    exits while it is being checked, reads fail and authorization is denied.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        process_fd = os.open(f"/proc/{pid}", flags)
    except OSError:
        return None
    try:
        uid = os.fstat(process_fd).st_uid
        stat_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        stat_fd = os.open("stat", stat_flags, dir_fd=process_fd)
        try:
            chunks = bytearray()
            while len(chunks) <= 8192:
                chunk = os.read(stat_fd, 4096)
                if not chunk:
                    break
                chunks.extend(chunk)
        finally:
            os.close(stat_fd)
    except OSError:
        return None
    finally:
        os.close(process_fd)
    if not chunks or len(chunks) > 8192:
        return None
    try:
        value = chunks.decode("utf-8", errors="strict").strip()
        # comm is parenthesized and may itself contain spaces or parentheses.
        close = value.rfind(")")
        if close <= 0 or close + 2 >= len(value):
            return None
        parsed_pid = int(value[: value.find(" ")])
        fields = value[close + 2 :].split()
        # fields starts at proc stat field 3 (state); starttime is field 22.
        if len(fields) < 20 or parsed_pid != pid:
            return None
        return ProcessIdentity(
            pid=parsed_pid,
            ppid=int(fields[1]),
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            tty_device=int(fields[4]),
            start_time_ticks=int(fields[19]),
            uid=int(uid),
        )
    except (UnicodeError, ValueError):
        return None


class LocalLaunchAuthorizer:
    """Authorize a peer only inside the process tree bound to its launch.

    Filesystem permissions and UID checks protect against other users.  This
    additional process-incarnation check separates sibling terminals that run
    under the same AgentServer service account.
    """

    def __init__(
        self,
        *,
        process_reader: ProcessIdentityReader = read_linux_process_identity,
        expected_uid: int | None = None,
        max_depth: int = MAX_PROCESS_ANCESTRY_DEPTH,
    ) -> None:
        self._process_reader = process_reader
        self.expected_uid = os.geteuid() if expected_uid is None else int(expected_uid)
        self.max_depth = max(1, int(max_depth))
        self._bindings: dict[tuple[str, str, str], LaunchProcessBinding] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(owner_id: str, terminal_id: str, launch_id: str) -> tuple[str, str, str]:
        values = tuple(str(value or "") for value in (owner_id, terminal_id, launch_id))
        if not all(values):
            raise ValueError("owner, terminal and launch identity are required")
        return values

    def bind_launch(
        self,
        *,
        owner_id: str,
        terminal_id: str,
        launch_id: str,
        root_pid: int,
    ) -> LaunchProcessBinding:
        key = self._key(owner_id, terminal_id, launch_id)
        root = self._process_reader(root_pid)
        if root is None:
            raise RuntimeError("managed launch process identity is unavailable")
        if root.uid != self.expected_uid:
            raise RuntimeError("managed launch process uid is not authorized")
        binding = LaunchProcessBinding(
            owner_id=key[0], terminal_id=key[1], launch_id=key[2], root=root
        )
        with self._lock:
            current = self._bindings.get(key)
            if current is not None and current != binding:
                raise RuntimeError("managed launch identity is already bound")
            self._bindings[key] = binding
        return binding

    def release_launch(
        self, *, owner_id: str, terminal_id: str, launch_id: str
    ) -> None:
        key = self._key(owner_id, terminal_id, launch_id)
        with self._lock:
            self._bindings.pop(key, None)

    @staticmethod
    def _same_incarnation(
        current: ProcessIdentity, expected: ProcessIdentity
    ) -> bool:
        return (
            current.pid == expected.pid
            and current.uid == expected.uid
            and current.start_time_ticks == expected.start_time_ticks
            and current.process_group_id == expected.process_group_id
            and current.session_id == expected.session_id
            and current.tty_device == expected.tty_device
        )

    def authorize(
        self,
        peer: PeerCredentials,
        *,
        owner_id: str,
        terminal_id: str,
        launch_id: str,
    ) -> bool:
        if peer.uid != self.expected_uid or peer.pid <= 0:
            return False
        key = self._key(owner_id, terminal_id, launch_id)
        with self._lock:
            binding = self._bindings.get(key)
        if binding is None:
            return False

        root = self._process_reader(binding.root.pid)
        if root is None or not self._same_incarnation(root, binding.root):
            return False

        current_pid = peer.pid
        visited: set[int] = set()
        for _depth in range(self.max_depth):
            if current_pid in visited:
                return False
            visited.add(current_pid)
            current = self._process_reader(current_pid)
            if current is None or current.uid != self.expected_uid:
                return False
            if current.pid == binding.root.pid:
                return self._same_incarnation(current, binding.root)
            # A normal child may have a separate job-control process group, but
            # it must remain in the launch's process session and controlling TTY.
            if current.session_id != binding.root.session_id:
                return False
            if binding.root.tty_device and current.tty_device != binding.root.tty_device:
                return False
            if current.ppid <= 1 or current.ppid == current.pid:
                return False
            current_pid = current.ppid
        return False


class ExecutionControlBroker:
    """Server-local, owner-only Unix socket for managed terminal context.

    This is the no-device-bridge path.  It authenticates the local OS peer,
    then validates the static terminal/launch identity on every message.  It
    never exposes a browser cookie, device credential or Reporter Token to the
    terminal environment.
    """

    def __init__(
        self,
        service: ExecutionService,
        path: Path,
        *,
        launch_authorizer: LocalLaunchAuthorizer | None = None,
        reference_key: bytes | None = None,
    ) -> None:
        if reference_key is not None and (
            not isinstance(reference_key, bytes) or len(reference_key) < 32
        ):
            raise ValueError("control reference key must contain at least 32 bytes")
        self.service = service
        self.path = Path(path)
        self.launch_authorizer = launch_authorizer or LocalLaunchAuthorizer()
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._epoch = uuid.uuid4().hex
        self._reference_key = bytes(reference_key) if reference_key is not None else None
        self._reference_key_lock = threading.Lock()
        self._sequences: dict[str, int] = {}
        self._sequence_lock = threading.Lock()

    def bind_launch(
        self,
        *,
        owner_id: str,
        terminal_id: str,
        launch_id: str,
        root_pid: int,
    ) -> LaunchProcessBinding:
        return self.launch_authorizer.bind_launch(
            owner_id=owner_id,
            terminal_id=terminal_id,
            launch_id=launch_id,
            root_pid=root_pid,
        )

    def release_launch(
        self, *, owner_id: str, terminal_id: str, launch_id: str
    ) -> None:
        self.launch_authorizer.release_launch(
            owner_id=owner_id, terminal_id=terminal_id, launch_id=launch_id
        )

    def _prepare_socket_directory(self) -> None:
        try:
            directory = self.path.parent.lstat()
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, mode=0o700)
            directory = self.path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode):
            raise RuntimeError(f"control directory is not a private directory: {self.path.parent}")
        if directory.st_uid != os.geteuid():
            raise RuntimeError("control directory is not owned by the service uid")
        self.path.parent.chmod(0o700)

    @property
    def _startup_lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    @property
    def _reference_key_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.reference-key-v1")

    def _read_reference_key(self) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._reference_key_path, flags)
        except OSError as error:
            raise RuntimeError("control reference key is unavailable") from error
        try:
            info = os.fstat(descriptor)
            linked = self._reference_key_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or (int(info.st_dev), int(info.st_ino))
                != (int(linked.st_dev), int(linked.st_ino))
            ):
                raise RuntimeError("control reference key is unsafe")
            value = bytearray()
            while len(value) <= 64:
                chunk = os.read(descriptor, 65 - len(value))
                if not chunk:
                    break
                value.extend(chunk)
        finally:
            os.close(descriptor)
        if len(value) != 32:
            raise RuntimeError("control reference key is invalid")
        return bytes(value)

    def _load_or_create_reference_key(self) -> bytes:
        self._prepare_socket_directory()
        startup_lock = self._acquire_startup_lock()
        try:
            try:
                return self._read_reference_key()
            except RuntimeError:
                if self._reference_key_path.exists() or self._reference_key_path.is_symlink():
                    raise

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self._reference_key_path, flags, 0o600)
            except FileExistsError:
                return self._read_reference_key()
            value = os.urandom(32)
            try:
                offset = 0
                while offset < len(value):
                    offset += os.write(descriptor, value[offset:])
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o600)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                with contextlib.suppress(OSError):
                    self._reference_key_path.unlink()
                raise
            else:
                os.close(descriptor)
            return self._read_reference_key()
        finally:
            self._release_startup_lock(startup_lock)

    def _provider_reference_key(
        self, *, owner_id: str, terminal_id: str, launch_id: str
    ) -> bytes:
        if self._reference_key is None:
            with self._reference_key_lock:
                if self._reference_key is None:
                    self._reference_key = self._load_or_create_reference_key()
        return hmac.new(
            self._reference_key,
            (
                "agentserver-local-reference-v1\0"
                f"{owner_id}\0{terminal_id}\0{launch_id}"
            ).encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def _acquire_startup_lock(self) -> int:
        if fcntl is None:
            raise RuntimeError("control socket startup requires filesystem flock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._startup_lock_path, flags, 0o600)
        except OSError as error:
            raise RuntimeError("control socket startup lock is unsafe") from error
        try:
            opened = os.fstat(descriptor)
            linked = self._startup_lock_path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (int(opened.st_dev), int(opened.st_ino))
                != (int(linked.st_dev), int(linked.st_ino))
            ):
                raise RuntimeError("control socket startup lock is unsafe")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("control socket startup lock is already held") from error
            # A cooperative peer must lock this exact persistent inode. Never
            # unlink the lock file, because replacing it would split the lock.
            linked = self._startup_lock_path.lstat()
            if (int(opened.st_dev), int(opened.st_ino)) != (
                int(linked.st_dev),
                int(linked.st_ino),
            ):
                raise RuntimeError("control socket startup lock changed while acquiring")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _release_startup_lock(descriptor: int) -> None:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def _remove_stale_socket(self) -> None:
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(current.st_mode):
            raise RuntimeError(f"control path exists and is not a socket: {self.path}")
        if current.st_uid != os.geteuid():
            raise RuntimeError("control socket is not owned by the service uid")
        identity = (int(current.st_dev), int(current.st_ino))
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.1)
            probe.connect(str(self.path))
        except OSError as error:
            if error.errno == errno.ENOENT:
                try:
                    replacement = self.path.lstat()
                except FileNotFoundError:
                    return
                if (int(replacement.st_dev), int(replacement.st_ino)) != identity:
                    raise RuntimeError(
                        "control socket changed during stale cleanup"
                    ) from error
            if error.errno != errno.ECONNREFUSED:
                raise RuntimeError(
                    "control socket liveness could not be verified"
                ) from error
            try:
                replacement = self.path.lstat()
            except FileNotFoundError:
                return
            if (
                not stat.S_ISSOCK(replacement.st_mode)
                or replacement.st_uid != os.geteuid()
                or (int(replacement.st_dev), int(replacement.st_ino)) != identity
            ):
                raise RuntimeError("control socket changed during stale cleanup")
            self.path.unlink()
        else:
            raise RuntimeError("control socket is already served by another process")
        finally:
            probe.close()

    async def start(self) -> None:
        if self._server is not None:
            return
        if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
            raise RuntimeError(
                "local execution control on Windows requires an owner-only named "
                "pipe DACL plus GetNamedPipeClientProcessId and process creation-time "
                "ancestry verification; the insecure path-only fallback is disabled"
            )
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError(
                "local execution control requires a peer-PID credential transport; "
                "an owner-only socket path is not a process identity boundary"
            )
        self._prepare_socket_directory()
        startup_lock = self._acquire_startup_lock()
        try:
            self._remove_stale_socket()

            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(self.path))
                self.path.chmod(0o600)
                current = self.path.lstat()
                if (
                    not stat.S_ISSOCK(current.st_mode)
                    or current.st_uid != os.geteuid()
                    or stat.S_IMODE(current.st_mode) != 0o600
                ):
                    raise RuntimeError("control socket ownership or permissions are unsafe")
                self._socket_identity = (int(current.st_dev), int(current.st_ino))
                listener.listen(socket.SOMAXCONN)
                listener.setblocking(False)
                self._server = await asyncio.start_unix_server(
                    self._handle,
                    sock=listener,
                    limit=MAX_CONTROL_MESSAGE_BYTES + 1,
                )
            except BaseException:
                listener.close()
                identity = self._socket_identity
                self._socket_identity = None
                if identity is not None:
                    with contextlib.suppress(FileNotFoundError):
                        current = self.path.lstat()
                        if (
                            stat.S_ISSOCK(current.st_mode)
                            and (int(current.st_dev), int(current.st_ino)) == identity
                        ):
                            self.path.unlink()
                raise
        finally:
            self._release_startup_lock(startup_lock)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        identity = self._socket_identity
        self._socket_identity = None
        if identity is not None:
            with contextlib.suppress(FileNotFoundError):
                current = self.path.lstat()
                if (
                    stat.S_ISSOCK(current.st_mode)
                    and (int(current.st_dev), int(current.st_ino)) == identity
                ):
                    self.path.unlink()

    @staticmethod
    def _peer_credentials(writer: asyncio.StreamWriter) -> PeerCredentials | None:
        peer = writer.get_extra_info("socket")
        if peer is None or not hasattr(socket, "SO_PEERCRED"):
            return None
        try:
            pid, uid, gid = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
        except (OSError, struct.error):
            return None
        if pid <= 0 or uid < 0 or gid < 0:
            return None
        return PeerCredentials(pid=pid, uid=uid, gid=gid)

    @staticmethod
    def _scope(request: Mapping[str, Any]) -> tuple[str, str, str]:
        scope = request.get("scope")
        if not isinstance(scope, Mapping):
            raise ControlProtocolError("managed scope is required")
        owner_id = str(scope.get("owner_id") or "")
        terminal_id = str(scope.get("terminal_id") or "")
        launch_id = str(scope.get("launch_id") or "")
        if not owner_id or not terminal_id or not launch_id:
            raise ControlProtocolError("owner, terminal and launch scope are required")
        return owner_id, terminal_id, launch_id

    def _next_sequence(self, terminal_id: str) -> int:
        with self._sequence_lock:
            value = self._sequences.get(terminal_id, 0) + 1
            self._sequences[terminal_id] = value
        return value

    def _event(
        self,
        request: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        owner_id: str,
        terminal_id: str,
        launch_id: str,
    ) -> dict[str, Any]:
        run_id = context.get("active_run_id")
        recent = context.get("recent_run")
        if not run_id or not isinstance(recent, Mapping) or recent.get("id") != run_id:
            return {
                "ok": True,
                "ignored": "no_active_assignment",
                "context": dict(context),
            }
        attributes = recent.get("attributes")
        if not isinstance(attributes, Mapping):
            raise ControlProtocolError("active Run binding is invalid")
        event_type = str(request.get("event_type") or "")
        payload = request.get("payload") or {}
        if not event_type or len(event_type) > 120 or not isinstance(payload, Mapping):
            raise ControlProtocolError("event_type and object payload are required")
        adapter_name = str(request.get("adapter") or "generic").strip().lower()
        if adapter_name not in ADAPTERS:
            raise ControlProtocolError("control runtime adapter is not supported")
        if event_type == "agent.heartbeat":
            sanitized_payload: dict[str, Any] = {}
        else:
            reference_key = self._provider_reference_key(
                owner_id=owner_id,
                terminal_id=terminal_id,
                launch_id=launch_id,
            )
            try:
                sanitized_payload = sanitize_runtime_payload(
                    event_type,
                    payload,
                    provider_kind=adapter_name,
                    reference_key=reference_key,
                )
            except ValueError as error:
                raise ControlProtocolError(
                    "control runtime event failed schema validation"
                ) from error
            wait_target = sanitized_payload.get("wait_target_run_id")
            if wait_target is not None:
                relations = self.service.store.relations(
                    owner_id=owner_id,
                    relation="parent_run",
                    source_kind="run",
                    source_id=str(run_id),
                    target_kind="run",
                    target_id=str(wait_target),
                )
                if not relations:
                    raise ControlProtocolError(
                        "control wait target is not a declared child Run"
                    )
        span_id = (
            str(sanitized_payload.get("span_id"))
            if event_type.startswith("span.") and sanitized_payload.get("span_id")
            else None
        )
        expected_revision = request.get("expected_revision")
        if expected_revision is not None and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ControlProtocolError("expected_revision must be a non-negative integer")
        event = EventEnvelope(
            type=event_type,
            event_id=new_id(),
            scope=EventScope(
                owner_id=owner_id,
                device_id=attributes.get("device_id"),
                terminal_id=terminal_id,
                launch_id=launch_id,
                agent_instance_id=attributes.get("agent_instance_id"),
                task_id=attributes.get("task_id"),
                assignment_id=attributes.get("assignment_id"),
                run_id=str(run_id),
                parent_run_id=attributes.get("parent_run_id"),
                span_id=span_id,
            ),
            producer=ProducerRef(
                id=f"local-control:{terminal_id}",
                epoch=self._epoch,
                seq=self._next_sequence(terminal_id),
                adapter=adapter_name,
                version="1",
                mode=ProducerMode.ADAPTER,
            ),
            payload=sanitized_payload,
            expected_revision=(
                expected_revision
                if expected_revision is not None
                else None
            ),
            evidence=Evidence(
                confidence=0.9,
                valid_for_ms=(
                    15_000
                    if event_type in {"run.activity.changed", "run.progress.updated"}
                    else None
                ),
            ),
        )
        claims = ReporterClaims(
            owner_id=owner_id,
            run_id=str(run_id),
            terminal_id=terminal_id,
            launch_id=launch_id,
            device_id=(str(attributes["device_id"]) if attributes.get("device_id") else None),
            agent_instance_id=(
                str(attributes["agent_instance_id"])
                if attributes.get("agent_instance_id")
                else None
            ),
            capabilities=tuple(
                sorted(REPORT_CAPABILITIES | {ADAPTER_REPORT_CAPABILITY})
            ),
            issued_at=0,
            expires_at=2**31 - 1,
            token_id=f"local-control:{launch_id}",
        )
        if event_type == "agent.heartbeat":
            lease = self.service.heartbeat(
                claims=claims, holder_id=claims.token_id
            )
            return {
                "ok": True,
                "heartbeat": True,
                "lease": lease,
                "context": self.service.terminal_context(
                    owner_id=owner_id,
                    terminal_id=terminal_id,
                    launch_id=launch_id,
                ),
            }
        result = self.service.ingest_runtime_event(event, claims=claims)
        return {
            "ok": True,
            "event": result.event.as_dict(),
            "status": result.status.value,
            "context": self.service.terminal_context(
                owner_id=owner_id,
                terminal_id=terminal_id,
                launch_id=launch_id,
            ),
        }

    def handle_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        owner_id, terminal_id, launch_id = self._scope(request)
        context = self.service.terminal_context(
            owner_id=owner_id,
            terminal_id=terminal_id,
            launch_id=launch_id,
        )
        action = request.get("action")
        if action == "context":
            return {"ok": True, "context": context}
        if action == "event":
            return self._event(
                request,
                context,
                owner_id=owner_id,
                terminal_id=terminal_id,
                launch_id=launch_id,
            )
        if action == "heartbeat":
            return self._event(
                {**dict(request), "event_type": "agent.heartbeat"},
                context,
                owner_id=owner_id,
                terminal_id=terminal_id,
                launch_id=launch_id,
            )
        if action == "flush":
            return {"ok": True, "queued": 0}
        raise ControlProtocolError("unsupported control action")

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            peer = self._peer_credentials(writer)
            if peer is None or peer.uid != os.geteuid():
                raise ControlProtocolError("control peer uid is not authorized")
            encoded = await reader.readline()
            if not encoded or len(encoded) > MAX_CONTROL_MESSAGE_BYTES:
                raise ControlProtocolError("control request is empty or too large")
            request = json.loads(encoded)
            if not isinstance(request, Mapping):
                raise ControlProtocolError("control request must be an object")
            owner_id, terminal_id, launch_id = self._scope(request)
            authorized = await asyncio.to_thread(
                self.launch_authorizer.authorize,
                peer,
                owner_id=owner_id,
                terminal_id=terminal_id,
                launch_id=launch_id,
            )
            if not authorized:
                raise ControlProtocolError(
                    "control peer is not authorized for the managed launch"
                )
            response = await asyncio.to_thread(self.handle_request, request)
        except (ControlProtocolError, ValueError, RuntimeError) as error:
            response = {"ok": False, "error": str(error)}
        writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
