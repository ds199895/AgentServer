from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import secrets
import socket
import stat
import struct
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from multiprocessing.connection import Client as PipeClient
from multiprocessing.connection import Listener as PipeListener
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .bridge_commands import (
    BridgeCommandAck,
    BridgeCommandJournal,
    BridgeCommandJournalError,
)
from .control import LocalLaunchAuthorizer, PeerCredentials
from .reporter import RuntimeReporter, load_reporter_token_file
from .runtime_lock import RuntimeInstanceLock


MAX_BRIDGE_MESSAGE_BYTES = 64 * 1024
LOOPBACK_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class BridgeProtocolError(ValueError):
    pass


def _validated_base_url(value: str) -> str:
    result = str(value or "").strip().rstrip("/")
    if not result or any(character.isspace() for character in result):
        raise ValueError("bridge base_url must be a valid URL")
    try:
        parsed = urlsplit(result)
        # Accessing port performs urllib's range and syntax validation.
        parsed.port
    except ValueError as error:
        raise ValueError("bridge base_url must be a valid URL") from error
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("bridge base_url must use https or loopback http")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("bridge base_url must not contain userinfo")
    if "?" in result or "#" in result:
        raise ValueError("bridge base_url must not contain query or fragment")
    if parsed.scheme == "http" and parsed.hostname.lower() not in LOOPBACK_HTTP_HOSTS:
        raise ValueError("plain HTTP is allowed only for a loopback base_url")
    return result


ContextProvider = Callable[[], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
CommandHandlerResult = str | Mapping[str, Any]
CommandHandler = Callable[
    [Mapping[str, Any]], CommandHandlerResult | Awaitable[CommandHandlerResult]
]
TokenProvider = Callable[[], str]


class ReloadingTokenFile:
    """Thread-safe, strict token-file provider for long-running bridges.

    The path is inspected on every call and re-read after an atomic replace or
    metadata change.  The bridge, rather than this provider, owns the fallback
    to the last valid token so a failed reload remains visible in health.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._signature: tuple[int, int, int, int, int, int] | None = None
        self._token = ""
        self._reload()

    def _stat_signature(self) -> tuple[int, int, int, int, int, int]:
        try:
            info = self.path.lstat()
        except OSError as error:
            raise ValueError(f"cannot inspect bridge token file: {self.path}") from error
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("bridge token path must be a regular file")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("bridge token file mode must be exactly 0600")
        return (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
            int(info.st_size),
            int(stat.S_IMODE(info.st_mode)),
        )

    def _reload(self) -> str:
        before = self._stat_signature()
        token = load_reporter_token_file(self.path)
        after = self._stat_signature()
        if before != after:
            raise ValueError("bridge token file changed while it was being read")
        self._token = token
        self._signature = after
        return token

    @property
    def last_valid_token(self) -> str:
        return self._token

    def __call__(self) -> str:
        with self._lock:
            signature = self._stat_signature()
            if signature != self._signature:
                return self._reload()
            return self._token

    def replace(self, token: str) -> str:
        """Persist a rotated credential with an atomic, mode-0600 swap."""

        value = str(token or "")
        if not value or any(character.isspace() for character in value):
            raise ValueError("bridge token replacement is invalid")
        encoded = (value + "\n").encode("utf-8")
        if len(encoded) > 4096:
            raise ValueError("bridge token replacement is too large")
        with self._lock:
            parent = self.path.parent
            try:
                directory = parent.lstat()
            except OSError as error:
                raise ValueError(
                    f"cannot inspect bridge token directory: {parent}"
                ) from error
            if not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode):
                raise ValueError("bridge token directory must be a real directory")
            if os.name != "nt" and directory.st_uid != os.geteuid():
                raise ValueError("bridge token directory is not owned by this uid")

            temporary = parent / (
                f".{self.path.name}.rotate-{os.getpid()}-{secrets.token_hex(8)}"
            )
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
                offset = 0
                while offset < len(encoded):
                    offset += os.write(descriptor, encoded[offset:])
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                temporary.chmod(0o600)
                os.replace(temporary, self.path)
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
                return self._reload()
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()


def _token_refresh_due(token: str, *, now: float | None = None) -> bool:
    """Use untrusted token timing only as a refresh scheduling hint."""

    try:
        encoded = token.split(".", 1)[0]
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(decoded)
        if not isinstance(payload, Mapping):
            return False
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        if issued_at < 0 or expires_at <= issued_at:
            return False
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    timestamp = time.time() if now is None else float(now)
    lifetime = expires_at - issued_at
    refresh_window = max(5.0, min(300.0, lifetime * 0.2))
    return expires_at - timestamp <= refresh_window


class AgentBridge:
    """Device-local bridge for managed context, reports, and command ACKs.

    Linux uses a mode-0600 Unix socket plus peer-PID/launch-lineage checks.
    Windows currently fails closed until a named-pipe DACL and client-process
    identity implementation is available.  Only outbound HTTP is used for the
    AgentServer connection.
    """

    def __init__(
        self,
        reporter: RuntimeReporter,
        *,
        address: str,
        base_url: str,
        reporter_token: str | None = None,
        command_token: str | None = None,
        reporter_token_provider: TokenProvider | None = None,
        command_token_provider: TokenProvider | None = None,
        context_provider: ContextProvider,
        command_handler: CommandHandler | None = None,
        command_handler_idempotent: bool = False,
        command_journal: BridgeCommandJournal | None = None,
        launch_root_pid: int | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        heartbeat_interval: float = 10.0,
        command_interval: float = 2.0,
    ) -> None:
        self.reporter = reporter
        self.address = address
        self.base_url = _validated_base_url(base_url)
        initial_reporter_token = reporter_token
        if not initial_reporter_token and reporter_token_provider is not None:
            initial_reporter_token = reporter_token_provider()
        initial_command_token = command_token
        if not initial_command_token and command_token_provider is not None:
            initial_command_token = command_token_provider()
        self.reporter_token = self._validate_token(initial_reporter_token)
        self.command_token = self._validate_token(
            initial_command_token or self.reporter_token
        )
        self._token_providers: dict[str, TokenProvider | None] = {
            "reporter": reporter_token_provider,
            "command": command_token_provider,
        }
        self.context_provider = context_provider
        self.command_handler = command_handler
        self.command_handler_idempotent = bool(command_handler_idempotent)
        self.command_journal = command_journal or BridgeCommandJournal(
            reporter.spool.database_path
        )
        if launch_root_pid is None or isinstance(launch_root_pid, bool):
            raise ValueError("bridge requires a managed launch_root_pid")
        self._launch_authorizer = LocalLaunchAuthorizer()
        self._launch_binding = self._launch_authorizer.bind_launch(
            owner_id=reporter.context.owner_id,
            terminal_id=reporter.context.terminal_id,
            launch_id=reporter.context.launch_id,
            root_pid=int(launch_root_pid),
        )
        self.http_transport = http_transport
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        self.command_interval = max(0.25, float(command_interval))
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._address_lock: RuntimeInstanceLock | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = asyncio.Event()
        self._command_lock = asyncio.Lock()
        self._token_refresh_locks = {
            "reporter": asyncio.Lock(),
            "command": asyncio.Lock(),
        }
        # (conservative server wall time, local monotonic sample).  Command
        # deadlines are server facts; a device wall clock may be arbitrarily
        # skewed and must never resurrect an expired command.
        self._server_clock_anchor: tuple[float, float] | None = None
        self._pipe_listener: PipeListener | None = None
        self._pipe_thread: threading.Thread | None = None
        self._pipe_stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._health: dict[str, dict[str, Any]] = {
            name: {
                "status": "not_started",
                "last_success": None,
                "last_error": None,
                "last_error_at": None,
                "auth_expired": False,
            }
            for name in ("forward", "commands")
        }
        self._credential_health: dict[str, dict[str, Any]] = {
            name: {
                "status": "healthy",
                "last_success": None,
                "last_error": None,
                "last_error_at": None,
                "source": "provider" if provider is not None else "static",
            }
            for name, provider in self._token_providers.items()
        }

    @staticmethod
    def _remove_stale_socket(path: Path) -> None:
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(current.st_mode):
            raise RuntimeError(f"bridge address exists and is not a socket: {path}")
        if current.st_uid != os.geteuid():
            raise RuntimeError("bridge socket is not owned by the service uid")
        stale_identity = (int(current.st_dev), int(current.st_ino))
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.1)
            probe.connect(str(path))
        except OSError:
            try:
                latest = path.lstat()
            except FileNotFoundError as error:
                raise RuntimeError(
                    "bridge socket changed while checking stale state"
                ) from error
            if (
                not stat.S_ISSOCK(latest.st_mode)
                or (int(latest.st_dev), int(latest.st_ino)) != stale_identity
            ):
                raise RuntimeError(
                    "bridge socket changed while checking stale state"
                )
            path.unlink()
        else:
            raise RuntimeError(f"bridge address is already in use: {path}")
        finally:
            probe.close()

    def _release_address_lock(self) -> None:
        lock = self._address_lock
        self._address_lock = None
        if lock is not None:
            lock.release()

    async def start(self) -> None:
        if self._server or self._pipe_listener:
            return
        self._closed.clear()
        self._loop = asyncio.get_running_loop()
        if os.name == "nt" or self.address.startswith("\\\\.\\pipe\\"):
            raise RuntimeError(
                "Windows Bridge control requires an owner-only named-pipe DACL, "
                "GetNamedPipeClientProcessId, and process creation-time ancestry; "
                "the unauthenticated AF_PIPE listener is disabled"
            )
        else:
            path = Path(self.address)
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory = path.parent.lstat()
            if (
                not stat.S_ISDIR(directory.st_mode)
                or stat.S_ISLNK(directory.st_mode)
                or directory.st_uid != os.geteuid()
            ):
                raise RuntimeError("bridge socket directory is not private and owned")
            path.parent.chmod(0o700)
            address_lock = RuntimeInstanceLock(
                path.parent / f".{path.name}.instance.lock"
            )
            try:
                address_lock.acquire()
            except RuntimeError as error:
                raise RuntimeError(f"bridge address is already in use: {path}") from error
            self._address_lock = address_lock
            try:
                self._remove_stale_socket(path)
                self._server = await asyncio.start_unix_server(
                    self._handle_unix_client,
                    path=str(path),
                    limit=MAX_BRIDGE_MESSAGE_BYTES + 1,
                )
                path.chmod(0o600)
                created = path.lstat()
                self._socket_identity = (
                    int(created.st_dev),
                    int(created.st_ino),
                )
                if (
                    not stat.S_ISSOCK(created.st_mode)
                    or created.st_uid != os.geteuid()
                    or stat.S_IMODE(created.st_mode) != 0o600
                ):
                    raise RuntimeError(
                        "bridge socket ownership or permissions are unsafe"
                    )
            except BaseException:
                if self._server is not None:
                    self._server.close()
                    await self._server.wait_closed()
                    self._server = None
                identity = self._socket_identity
                self._socket_identity = None
                if identity is not None:
                    with contextlib.suppress(FileNotFoundError):
                        created = path.lstat()
                        if (
                            stat.S_ISSOCK(created.st_mode)
                            and (int(created.st_dev), int(created.st_ino))
                            == identity
                        ):
                            path.unlink()
                self._release_address_lock()
                raise
        self._spawn("forward", self._forward_loop())
        self._spawn("commands", self._command_loop())

    def _spawn(self, name: str, coroutine: Awaitable[None]) -> None:
        self._health[name]["status"] = "running"
        task = asyncio.create_task(coroutine, name=f"agent-bridge-{name}")
        self._tasks.add(task)
        self._background_tasks[name] = task

        def finished(value: asyncio.Task[None]) -> None:
            self._tasks.discard(value)
            if value.cancelled():
                self._health[name]["status"] = "stopped"
                return
            error = value.exception()
            if error is not None:
                self._record_health_error(name, error)
                self._health[name]["status"] = "failed"
            else:
                self._health[name]["status"] = "stopped"

        task.add_done_callback(finished)

    async def close(self) -> None:
        self._closed.set()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            path = Path(self.address)
            identity = self._socket_identity
            self._socket_identity = None
            if identity is not None:
                with contextlib.suppress(FileNotFoundError):
                    current = path.lstat()
                    if (
                        stat.S_ISSOCK(current.st_mode)
                        and (int(current.st_dev), int(current.st_ino)) == identity
                    ):
                        path.unlink()
        self._release_address_lock()
        if self._pipe_listener:
            self._pipe_stop.set()
            with contextlib.suppress(OSError):
                PipeClient(self.address, family="AF_PIPE").close()
            self._pipe_listener.close()
            self._pipe_listener = None
            if self._pipe_thread:
                await asyncio.to_thread(self._pipe_thread.join, 2)
            self._pipe_thread = None
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for value in self._health.values():
            value["status"] = "stopped"

    def _record_health_success(self, component: str) -> None:
        value = self._health[component]
        value.update(
            status="running",
            last_success=time.time(),
            last_error=None,
            last_error_at=None,
            auth_expired=False,
        )

    def _record_health_error(self, component: str, error: BaseException) -> None:
        value = self._health[component]
        unauthorized = (
            isinstance(error, httpx.HTTPStatusError)
            and error.response.status_code == 401
        )
        value.update(
            status="degraded",
            last_error=f"{type(error).__name__}: {str(error)[:1000]}",
            last_error_at=time.time(),
            auth_expired=bool(value["auth_expired"] or unauthorized),
        )

    @staticmethod
    def _validate_token(value: object) -> str:
        token = str(value or "")
        if (
            not token
            or len(token.encode("utf-8")) > 4096
            or any(character.isspace() for character in token)
        ):
            raise ValueError("bridge token provider returned an invalid token")
        return token

    def _record_credential_success(self, kind: str) -> None:
        self._credential_health[kind].update(
            status="healthy",
            last_success=time.time(),
            last_error=None,
            last_error_at=None,
        )

    def _record_credential_error(self, kind: str, error: BaseException) -> None:
        self._credential_health[kind].update(
            status="degraded",
            last_error=f"{type(error).__name__}: {str(error)[:1000]}",
            last_error_at=time.time(),
        )

    async def _resolve_token(self, kind: str) -> str:
        attribute = "reporter_token" if kind == "reporter" else "command_token"
        provider = self._token_providers[kind]
        current = self._validate_token(getattr(self, attribute))
        if provider is None:
            return current
        try:
            resolved = self._validate_token(await asyncio.to_thread(provider))
        except Exception as error:
            self._record_credential_error(kind, error)
            # Continue with the last value that passed validation.  This keeps
            # an in-flight atomic rotation from causing an avoidable outage,
            # while health still makes the reload failure explicit.
            return current
        setattr(self, attribute, resolved)
        self._record_credential_success(kind)
        return resolved

    async def reporter_auth_token(self) -> str:
        """Resolve the current report token for an adapter-owned request."""

        return await self._refresh_token_if_due("reporter")

    async def _refresh_token_if_due(self, kind: str) -> str:
        """Rotate a short-lived token before expiry and persist it when possible."""

        current = await self._resolve_token(kind)
        if not _token_refresh_due(current):
            return current
        async with self._token_refresh_locks[kind]:
            current = await self._resolve_token(kind)
            if not _token_refresh_due(current):
                return current
            try:
                async with httpx.AsyncClient(
                    timeout=10, trust_env=False, transport=self.http_transport
                ) as client:
                    response = await client.post(
                        f"{self.base_url}/api/runtime/v1/token:refresh",
                        headers={"Authorization": f"Bearer {current}"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                if not isinstance(payload, Mapping):
                    raise BridgeProtocolError("runtime token refresh response is invalid")
                replacement = self._validate_token(payload.get("token"))
                provider = self._token_providers[kind]
                if provider is not None:
                    persist = getattr(provider, "replace", None)
                    if not callable(persist):
                        raise BridgeProtocolError(
                            "bridge token provider cannot persist a rotated token"
                        )
                    replacement = self._validate_token(
                        await asyncio.to_thread(persist, replacement)
                    )
                attribute = (
                    "reporter_token" if kind == "reporter" else "command_token"
                )
                setattr(self, attribute, replacement)
                self._record_credential_success(kind)
                return replacement
            except Exception as error:
                self._record_credential_error(kind, error)
                raise

    def _task_state(self, name: str) -> str:
        task = self._background_tasks.get(name)
        if task is None:
            return "not_started"
        if task.cancelled():
            return "cancelled"
        if not task.done():
            return "running"
        return "failed" if task.exception() is not None else "stopped"

    async def _health_snapshot(self) -> dict[str, Any]:
        command_status = await asyncio.to_thread(
            self.command_journal.status_summary,
            now=self._journal_time(),
        )
        spool_depth = await asyncio.to_thread(lambda: len(self.reporter.spool))
        components = {
            name: {**value, "task": self._task_state(name)}
            for name, value in self._health.items()
        }
        last_successes = [
            float(value["last_success"])
            for value in components.values()
            if value["last_success"] is not None
        ]
        errors = [
            value
            for value in (*components.values(), *self._credential_health.values())
            if value["last_error"] is not None
        ]
        latest_error = max(
            errors,
            key=lambda item: float(item["last_error_at"] or 0),
            default=None,
        )
        auth_expired = any(bool(value["auth_expired"]) for value in components.values())
        uncertain = command_status["uncertain"]
        failed_task = any(
            value["task"] == "failed" for value in components.values()
        )
        degraded = bool(errors or auth_expired or uncertain or failed_task)
        started = any(value["task"] == "running" for value in components.values())
        return {
            "status": "degraded" if degraded else ("healthy" if started else "stopped"),
            "last_success": max(last_successes) if last_successes else None,
            "last_error": latest_error["last_error"] if latest_error else None,
            "auth_expired": auth_expired,
            "spool_depth": spool_depth,
            "tasks": components,
            "credentials": {
                name: dict(value) for name, value in self._credential_health.items()
            },
            "commands": command_status,
        }

    async def _context(self) -> Mapping[str, Any]:
        result = self.context_provider()
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment,misc]
        return result  # type: ignore[return-value]

    def _estimated_server_time(self) -> float | None:
        anchor = self._server_clock_anchor
        if anchor is None:
            return None
        return anchor[0] + max(0.0, time.monotonic() - anchor[1])

    def _journal_time(self) -> float:
        # Before the first authenticated context response, avoid mutating the
        # journal from the untrusted device wall clock.  A server-calibrated
        # call will expire due rows before any command is exposed or executed.
        value = self._estimated_server_time()
        return value if value is not None else float("-inf")

    def _calibrate_server_time(
        self, reported_server_time: object, *, elapsed: float
    ) -> float:
        if (
            isinstance(reported_server_time, bool)
            or not isinstance(reported_server_time, (int, float))
            or not math.isfinite(float(reported_server_time))
        ):
            raise BridgeProtocolError(
                "runtime command context has no valid server_time"
            )
        sampled_at = time.monotonic()
        # The response timestamp was sampled somewhere inside the round trip.
        # Adding the full elapsed time is deliberately conservative: a command
        # can expire slightly early, never late because of transport latency.
        conservative = float(reported_server_time) + max(0.0, float(elapsed))
        previous = self._server_clock_anchor
        if previous is not None:
            conservative = max(
                conservative,
                previous[0] + max(0.0, sampled_at - previous[1]),
            )
        self._server_clock_anchor = (conservative, sampled_at)
        return conservative

    @staticmethod
    def _validate_command_fence(
        command: Mapping[str, Any],
        *,
        context: Mapping[str, Any],
        server_time: float,
        runtime: Any,
    ) -> None:
        payload = command.get("payload")
        if not isinstance(payload, Mapping) or any(
            str(payload.get(field) or "") != expected
            for field, expected in (
                ("run_id", runtime.run_id),
                ("assignment_id", runtime.assignment_id),
                ("terminal_id", runtime.terminal_id),
                ("launch_id", runtime.launch_id),
            )
        ):
            raise BridgeProtocolError(
                "runtime command fence does not match the active assignment"
            )
        lease = context.get("terminal_lease")
        assert isinstance(lease, Mapping)  # validated by the context boundary
        fence_revision = payload.get("terminal_lease_revision")
        current_revision = lease.get("revision")
        if (
            str(payload.get("terminal_lease_id") or "")
            != str(lease.get("id") or "")
            or not isinstance(fence_revision, int)
            or isinstance(fence_revision, bool)
            or fence_revision < 1
            or not isinstance(current_revision, int)
            or isinstance(current_revision, bool)
            or current_revision < fence_revision
        ):
            raise BridgeProtocolError(
                "runtime command terminal lease fence is no longer current"
            )
        expires_at = command.get("expires_at")
        if expires_at is not None:
            if (
                isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(float(expires_at))
            ):
                raise BridgeProtocolError("runtime command deadline is invalid")
            if float(expires_at) <= server_time:
                raise BridgeProtocolError("runtime command has expired")

    async def _require_current_command_context(
        self, command: Mapping[str, Any] | None = None
    ) -> tuple[Mapping[str, Any], float]:
        """Fail closed before exposing or executing a downloaded command."""

        started_at = time.monotonic()
        context = await self._context()
        elapsed = time.monotonic() - started_at
        if not isinstance(context, Mapping):
            raise BridgeProtocolError("runtime command context is invalid")
        assignment = context.get("assignment")
        runtime = self.reporter.context
        if (
            context.get("offline") is True
            or str(context.get("terminal_id") or "") != runtime.terminal_id
            or str(context.get("launch_id") or "") != runtime.launch_id
            or str(context.get("active_run_id") or "") != runtime.run_id
            or not isinstance(assignment, Mapping)
            or str(assignment.get("assignment_id") or "")
            != runtime.assignment_id
        ):
            raise BridgeProtocolError(
                "runtime command context is no longer the active assignment"
            )
        server_time = self._calibrate_server_time(
            context.get("server_time"), elapsed=elapsed
        )
        lease = context.get("terminal_lease")
        if not isinstance(lease, Mapping):
            raise BridgeProtocolError(
                "runtime command context has no active terminal lease"
            )
        lease_id = str(lease.get("id") or "")
        revision = lease.get("revision")
        expires_at = lease.get("expires_at")
        if (
            not lease_id
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= server_time
        ):
            raise BridgeProtocolError(
                "runtime command context terminal lease is invalid or expired"
            )
        if command is not None:
            self._validate_command_fence(
                command,
                context=context,
                server_time=server_time,
                runtime=runtime,
            )
        return context, server_time

    async def _handle_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "context":
            return {"ok": True, "context": dict(await self._context())}
        if action == "event":
            event_type = str(request.get("event_type") or "")
            payload = request.get("payload") or {}
            if not event_type or len(event_type) > 120 or not isinstance(payload, Mapping):
                raise BridgeProtocolError("event_type and object payload are required")
            event = self.reporter.emit(event_type, payload)
            return {"ok": True, "event": event, "queued": len(self.reporter.spool)}
        if action == "heartbeat":
            return {"ok": True, "heartbeat": await self.send_heartbeat(), "queued": len(self.reporter.spool)}
        if action == "flush":
            reporter_token = await self._refresh_token_if_due("reporter")
            result = await asyncio.to_thread(
                self.reporter.flush,
                self.base_url,
                reporter_token,
            )
            return {"ok": True, "result": result, "queued": len(self.reporter.spool)}
        if action == "health":
            return {"ok": True, "health": await self._health_snapshot()}
        if action == "commands":
            context, server_time = await self._require_current_command_context()
            pending = await asyncio.to_thread(
                self.command_journal.pending, now=server_time
            )
            for command in pending:
                self._validate_command_fence(
                    command,
                    context=context,
                    server_time=server_time,
                    runtime=self.reporter.context,
                )
            return {
                "ok": True,
                "commands": pending,
                "cursor": self.command_journal.cursor,
            }
        if action == "command_recover":
            command_id = str(request.get("command_id") or "")
            if request.get("strategy") != "retry_idempotent":
                raise BridgeProtocolError(
                    "uncertain command recovery requires retry_idempotent strategy"
                )
            context, server_time = await self._require_current_command_context()
            pending = await asyncio.to_thread(
                self.command_journal.command, command_id, now=server_time
            )
            if pending is None:
                raise BridgeProtocolError("command does not exist in the local journal")
            self._validate_command_fence(
                pending,
                context=context,
                server_time=server_time,
                runtime=self.reporter.context,
            )
            command = await asyncio.to_thread(
                self.command_journal.retry_uncertain,
                command_id,
                now=server_time,
            )
            return {"ok": True, "command": command}
        if action == "command_ack":
            command_id = str(request.get("command_id") or "")
            status = str(request.get("status") or "")
            payload = request.get("payload") or {}
            if not isinstance(payload, Mapping):
                raise BridgeProtocolError("command ACK payload must be an object")
            context, server_time = await self._require_current_command_context()
            pending = await asyncio.to_thread(
                self.command_journal.command, command_id, now=server_time
            )
            if pending is None:
                raise BridgeProtocolError("command does not exist in the local journal")
            try:
                self._validate_command_fence(
                    pending,
                    context=context,
                    server_time=server_time,
                    runtime=self.reporter.context,
                )
            except BridgeProtocolError as error:
                return await self._command_ack_failure(
                    command_id=command_id,
                    status=status,
                    error=error,
                    now=server_time,
                )
            try:
                acknowledgement = await asyncio.to_thread(
                    self.command_journal.prepare_ack,
                    command_id=command_id,
                    status=status,
                    payload=payload,
                    now=server_time,
                )
            except BridgeCommandJournalError as error:
                return await self._command_ack_failure(
                    command_id=command_id,
                    status=status,
                    error=error,
                    now=server_time,
                )
            try:
                responses = await self.flush_command_acks()
            except (
                BridgeCommandJournalError,
                BridgeProtocolError,
                OSError,
                RuntimeError,
                httpx.HTTPError,
            ) as error:
                self._record_health_error("commands", error)
                return await self._command_ack_failure(
                    command_id=command_id,
                    status=status,
                    error=error,
                    now=self._journal_time(),
                )
            # A background flush can win the lock between prepare_ack() and
            # this request.  Reload the durable row so the local response does
            # not claim success using a stale pre-flush ACK object.
            try:
                acknowledgement = await asyncio.to_thread(
                    self.command_journal.prepare_ack,
                    command_id=command_id,
                    status=status,
                    payload=payload,
                    now=self._journal_time(),
                )
            except BridgeCommandJournalError as error:
                return await self._command_ack_failure(
                    command_id=command_id,
                    status=status,
                    error=error,
                    now=self._journal_time(),
                )
            result = responses.get(acknowledgement.ack_id)
            if result is None:
                # It may already have been durably acknowledged before a local
                # socket response was lost.  In that case the stored response
                # is the authoritative retry result.
                result = dict(acknowledgement.response or {})
            response = {
                "ok": True,
                "ack": {
                    **acknowledgement.as_dict(),
                    "server_acknowledged": acknowledgement.server_acknowledged,
                    "response": result,
                },
            }
            if not acknowledgement.server_acknowledged:
                response.update(
                    ok=False,
                    error="command ACK has not reached the server",
                )
            else:
                self._record_health_success("commands")
            return response
        raise BridgeProtocolError("unsupported bridge action")

    async def _command_ack_failure(
        self,
        *,
        command_id: str,
        status: str,
        error: BaseException,
        now: float | None = None,
    ) -> dict[str, Any]:
        command: dict[str, Any] | None = None
        acknowledgement: BridgeCommandAck | None = None
        try:
            command = await asyncio.to_thread(
                self.command_journal.command,
                command_id,
                now=self._journal_time() if now is None else now,
            )
            acknowledgement = await asyncio.to_thread(
                self.command_journal.acknowledgement,
                command_id=command_id,
                status=status,
            )
        except BridgeCommandJournalError:
            pass
        return {
            "ok": False,
            "error": str(error),
            "command": command,
            "ack": acknowledgement.as_dict() if acknowledgement else None,
            "server_acknowledged": bool(
                acknowledgement and acknowledgement.server_acknowledged
            ),
        }

    async def _handle_unix_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            peer_socket = writer.get_extra_info("socket")
            if peer_socket is None or not hasattr(socket, "SO_PEERCRED"):
                raise BridgeProtocolError(
                    "bridge control requires kernel peer PID credentials"
                )
            try:
                pid, uid, gid = struct.unpack(
                    "3i",
                    peer_socket.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                    ),
                )
            except (OSError, struct.error) as error:
                raise BridgeProtocolError(
                    "bridge peer credentials are unavailable"
                ) from error
            authorized = await asyncio.to_thread(
                self._launch_authorizer.authorize,
                PeerCredentials(pid=pid, uid=uid, gid=gid),
                owner_id=self._launch_binding.owner_id,
                terminal_id=self._launch_binding.terminal_id,
                launch_id=self._launch_binding.launch_id,
            )
            if not authorized:
                raise BridgeProtocolError(
                    "bridge peer is not authorized for the managed launch"
                )
            encoded = await reader.readline()
            if not encoded or len(encoded) > MAX_BRIDGE_MESSAGE_BYTES:
                raise BridgeProtocolError("bridge request is empty or too large")
            request = json.loads(encoded)
            if not isinstance(request, Mapping):
                raise BridgeProtocolError("bridge request must be an object")
            response = await self._handle_request(request)
        except (
            BridgeCommandJournalError,
            BridgeProtocolError,
            ValueError,
            RuntimeError,
            httpx.HTTPError,
        ) as exc:
            response = {"ok": False, "error": str(exc)}
        writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

    def _pipe_accept_loop(self) -> None:
        listener = self._pipe_listener
        loop = self._loop
        if listener is None or loop is None:
            return
        while not self._pipe_stop.is_set():
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                return
            try:
                encoded = connection.recv_bytes(MAX_BRIDGE_MESSAGE_BYTES)
                request = json.loads(encoded)
                if not isinstance(request, Mapping):
                    raise BridgeProtocolError("bridge request must be an object")
                future = asyncio.run_coroutine_threadsafe(self._handle_request(request), loop)
                response = future.result(timeout=30)
            except (
                BridgeCommandJournalError,
                BridgeProtocolError,
                ValueError,
                RuntimeError,
                TimeoutError,
                httpx.HTTPError,
            ) as exc:
                response = {"ok": False, "error": str(exc)}
            with contextlib.suppress(OSError):
                connection.send_bytes(json.dumps(response, separators=(",", ":")).encode())
            connection.close()

    async def _wait_or_close(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def _forward_loop(self) -> None:
        while not self._closed.is_set():
            errors: list[Exception] = []
            try:
                reporter_token = await self._refresh_token_if_due("reporter")
                await asyncio.to_thread(
                    self.reporter.flush,
                    self.base_url,
                    reporter_token,
                )
            except Exception as error:
                errors.append(error)
            try:
                await self.send_heartbeat()
            except Exception as error:
                # A terminal Run may reject heartbeats while still accepting an
                # exact replay of its final event, so heartbeat failure must not
                # block the next spool flush.
                errors.append(error)
            if errors:
                for error in errors:
                    self._record_health_error("forward", error)
            else:
                self._record_health_success("forward")
            if await self._wait_or_close(self.heartbeat_interval):
                return

    async def send_heartbeat(self) -> dict[str, Any]:
        reporter_token = await self._refresh_token_if_due("reporter")
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, transport=self.http_transport
        ) as client:
            response = await client.post(
                f"{self.base_url}/api/runtime/v1/heartbeat",
                headers={"Authorization": f"Bearer {reporter_token}"},
                json={"producer_id": self.reporter.producer_id},
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise BridgeProtocolError("runtime heartbeat response is invalid")
            return value

    async def poll_commands(self) -> list[dict[str, Any]]:
        async with self._command_lock:
            command_token = await self._refresh_token_if_due("command")
            headers = {"Authorization": f"Bearer {command_token}"}
            async with httpx.AsyncClient(
                timeout=10, trust_env=False, transport=self.http_transport
            ) as client:
                response = await client.get(
                    f"{self.base_url}/api/runtime/v1/commands",
                    headers=headers,
                    params={"after_sequence": self.command_journal.cursor},
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as error:
                    raise BridgeProtocolError(
                        "runtime commands response is invalid"
                    ) from error
            if not isinstance(payload, Mapping):
                raise BridgeProtocolError("runtime commands response is invalid")
            commands = payload.get("commands", [])
            if not isinstance(commands, list) or any(
                not isinstance(command, Mapping) for command in commands
            ):
                raise BridgeProtocolError("runtime commands response is invalid")
            context, server_time = await self._require_current_command_context()
            pending = await asyncio.to_thread(
                self.command_journal.record_server_commands,
                commands,
                now=server_time,
            )
            for command in pending:
                self._validate_command_fence(
                    command,
                    context=context,
                    server_time=server_time,
                    runtime=self.reporter.context,
                )
            if self.command_handler is not None:
                await self._dispatch_commands()
            await self._flush_command_acks_locked()
            return await asyncio.to_thread(
                self.command_journal.pending, now=self._journal_time()
            )

    async def _dispatch_commands(self) -> None:
        handler = self.command_handler
        if handler is None:
            return
        context, server_time = await self._require_current_command_context()
        if self.command_handler_idempotent:
            summary = await asyncio.to_thread(
                self.command_journal.status_summary,
                now=server_time,
            )
            for command in summary["uncertain"]:
                if command.get("status") == "uncertain":
                    self._validate_command_fence(
                        command,
                        context=context,
                        server_time=server_time,
                        runtime=self.reporter.context,
                    )
                    await asyncio.to_thread(
                        self.command_journal.retry_uncertain,
                        str(command["command_id"]),
                        now=server_time,
                    )
        command_ids = await asyncio.to_thread(
            self.command_journal.dispatchable_ids,
            now=server_time,
        )
        for command_id in command_ids:
            context, server_time = await self._require_current_command_context()
            pending = await asyncio.to_thread(
                self.command_journal.command,
                command_id,
                now=server_time,
            )
            if pending is None:
                continue
            self._validate_command_fence(
                pending,
                context=context,
                server_time=server_time,
                runtime=self.reporter.context,
            )
            command = await asyncio.to_thread(
                self.command_journal.begin_handler,
                command_id,
                now=server_time,
            )
            if command is None:
                continue
            try:
                result = handler(command)
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[assignment,misc]
                if isinstance(result, Mapping):
                    status = str(result.get("status") or "")
                    ack_payload = result.get("payload") or {}
                    if not isinstance(ack_payload, Mapping):
                        raise BridgeProtocolError(
                            "command handler ACK payload must be an object"
                        )
                else:
                    status = str(result or "")
                    ack_payload = {}
                if status not in {"accepted", "rejected", "completed"}:
                    raise BridgeProtocolError(
                        "command handler must return accepted, rejected, or completed"
                    )
                await asyncio.to_thread(
                    self.command_journal.prepare_ack,
                    command_id=command_id,
                    status=status,
                    payload=ack_payload,
                    now=self._journal_time(),
                )
            except BaseException as error:
                with contextlib.suppress(BridgeCommandJournalError):
                    await asyncio.to_thread(
                        self.command_journal.mark_uncertain,
                        command_id,
                        f"{type(error).__name__}: {str(error)[:900]}",
                        now=self._journal_time(),
                    )
                raise

    async def _send_command_ack(
        self,
        client: httpx.AsyncClient,
        acknowledgement: BridgeCommandAck,
    ) -> dict[str, Any]:
        command_token = await self._refresh_token_if_due("command")
        response = await client.post(
            f"{self.base_url}/api/runtime/v1/commands/"
            f"{acknowledgement.command_id}/ack",
            headers={"Authorization": f"Bearer {command_token}"},
            json=acknowledgement.request_body(),
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise BridgeProtocolError(
                "runtime command ACK response is invalid"
            ) from error
        if not isinstance(payload, Mapping):
            raise BridgeProtocolError("runtime command ACK response is invalid")
        result = dict(payload)
        await asyncio.to_thread(
            self.command_journal.mark_acknowledged,
            acknowledgement.ack_id,
            result,
        )
        return result

    async def _flush_command_acks_locked(self) -> dict[str, dict[str, Any]]:
        acknowledgements = await asyncio.to_thread(
            self.command_journal.pending_acks,
            now=float("-inf"),
        )
        results: dict[str, dict[str, Any]] = {}
        if not acknowledgements:
            return results
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, transport=self.http_transport
        ) as client:
            for acknowledgement in acknowledgements:
                context, server_time = await self._require_current_command_context()
                command = await asyncio.to_thread(
                    self.command_journal.command,
                    acknowledgement.command_id,
                    now=server_time,
                )
                if command is None:
                    raise BridgeProtocolError(
                        "command ACK has no durable command fence"
                    )
                self._validate_command_fence(
                    command,
                    context=context,
                    server_time=server_time,
                    runtime=self.reporter.context,
                )
                current = await asyncio.to_thread(
                    self.command_journal.acknowledgement,
                    command_id=acknowledgement.command_id,
                    status=acknowledgement.status,
                )
                if current is None or current.delivery_state != "pending":
                    continue
                results[acknowledgement.ack_id] = await self._send_command_ack(
                    client, current
                )
        return results

    async def flush_command_acks(self) -> dict[str, dict[str, Any]]:
        async with self._command_lock:
            return await self._flush_command_acks_locked()

    async def _command_loop(self) -> None:
        while not self._closed.is_set():
            errors: list[Exception] = []
            try:
                await self.flush_command_acks()
            except Exception as error:
                # A durable pending ACK remains retryable.  CancelledError is
                # a BaseException and still terminates the task immediately.
                errors.append(error)
            try:
                await self.poll_commands()
            except Exception as error:
                # Handler failures leave the command uncertain so the next
                # explicit/idempotent recovery can use the same command_id.
                errors.append(error)
            if errors:
                for error in errors:
                    self._record_health_error("commands", error)
            else:
                self._record_health_success("commands")
            if await self._wait_or_close(self.command_interval):
                return
