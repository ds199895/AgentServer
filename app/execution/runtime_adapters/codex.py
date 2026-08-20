from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .base import (
    ApprovalDecision,
    PermissionMode,
    RuntimeAdapter,
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
)
from .jsonrpc import (
    MAX_JSONRPC_MESSAGE_BYTES,
    AsyncJsonRpcPeer,
    JsonRpcPeerError,
    JsonRpcProtocolError,
    JsonRpcRemoteError,
    JsonRpcRequestError,
    JsonRpcRequestTimeout,
    JsonRpcTransportError,
)


PROVIDER = "codex"
_END = object()
_SAFE_INHERITED_ENVIRONMENT = frozenset(
    {
        "ALL_PROXY",
        "COLORTERM",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LANGUAGE",
        "LOGNAME",
        "NO_COLOR",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TZ",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_FORBIDDEN_ENVIRONMENT_NAMES = frozenset(
    {
        "BASH_ENV",
        "CDPATH",
        "ENV",
        "GCONV_PATH",
        "GLIBC_TUNABLES",
        "IFS",
        "JAVA_TOOL_OPTIONS",
        "LOCPATH",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NLSPATH",
        "PERL5LIB",
        "PERL5OPT",
        "PROMPT_COMMAND",
        "RUBYLIB",
        "RUBYOPT",
        "SHELLOPTS",
        "ZDOTDIR",
        "_JAVA_OPTIONS",
    }
)
_FORBIDDEN_ENVIRONMENT_PREFIXES = (
    "DYLD_",
    "LD_",
    "MALLOC_",
    "PYTHON",
)
_DELTA_METHODS = frozenset(
    {
        "item/agentMessage/delta",
        "item/plan/delta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/textDelta",
        "item/commandExecution/outputDelta",
        "item/commandExecution/terminalInteraction",
        "item/fileChange/outputDelta",
        "item/fileChange/patchUpdated",
        "rawResponseItem/completed",
        "rawResponse/completed",
    }
)

_DISPLAY_DELTA_TYPES = {
    "item/agentMessage/delta": ("message.delta", "assistant"),
    "item/reasoning/summaryTextDelta": ("reasoning.delta", "reasoning_summary"),
    "item/reasoning/textDelta": ("reasoning.delta", "reasoning_summary"),
    "item/commandExecution/outputDelta": ("tool.output.delta", "tool"),
    "item/fileChange/outputDelta": ("file.output.delta", "file"),
}

_PUBLIC_SECRET_MARKER = re.compile(r"\b(?:LEAK|SECRET|TOKEN|PASSWORD|CREDENTIAL)_[A-Z0-9_]+\b")
_PUBLIC_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|credential)\b\s*[:=]\s*[^\s,;]+"
)


def _display_delta(value: object) -> str | None:
    """Extract only provider display text; never forward the raw RPC object."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, Mapping):
        text = _text(value.get("delta") or value.get("text")) or ""
    else:
        text = ""
    if not text:
        return None
    # Runtime events are bounded by the envelope limit. Keep one provider
    # fragment bounded too so a malformed provider cannot monopolize the spool.
    # Provider output is user-visible, but it is still untrusted text. Remove
    # obvious secret markers/assignments before placing it in the durable spool.
    text = _PUBLIC_SECRET_MARKER.sub("[redacted]", text)
    text = _PUBLIC_SECRET_ASSIGNMENT.sub(r"\1=[redacted]", text)
    return text[:16_384]


def _completed_item_text(value: object) -> str | None:
    """Extract final assistant text from Codex item/completed payloads.

    Codex has emitted both a plain ``text`` field and structured ``content``
    parts across app-server releases. Keep this projection deliberately
    narrow: only text-like fields are exposed to the durable runtime log.
    """
    if isinstance(value, str):
        return _display_delta(value)
    if isinstance(value, Mapping):
        for key in ("text", "message", "content", "parts"):
            candidate = _completed_item_text(value.get(key))
            if candidate:
                return candidate
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        fragments = [part for item in value if (part := _completed_item_text(item))]
        return "".join(fragments)[:16_384] if fragments else None
    return None


def _unsafe_environment_name(value: str) -> bool:
    name = value.upper()
    return name in _FORBIDDEN_ENVIRONMENT_NAMES or any(
        name.startswith(prefix) for prefix in _FORBIDDEN_ENVIRONMENT_PREFIXES
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


_RECOVERABLE_RESUME_SNIPPETS = (
    "not found",
    "missing thread",
    "no such thread",
    "unknown thread",
    "does not exist",
    "no rollout found",
)
_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "dynamic_tool_call",
        "web_search",
    }
)


def _permission_mode(value: PermissionMode | str) -> PermissionMode:
    raw = value.value if isinstance(value, PermissionMode) else str(value or "")
    if raw == "auto-accept-edits":
        raw = PermissionMode.WORKSPACE_WRITE.value
    try:
        return PermissionMode(raw)
    except ValueError as error:
        raise ValueError(f"unsupported Codex permission mode: {raw}") from error


def codex_permission_config(mode: PermissionMode | str) -> dict[str, Any]:
    value = _permission_mode(mode)
    if value is PermissionMode.APPROVAL_REQUIRED:
        return {
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "read-only",
            "sandboxPolicy": {"type": "readOnly"},
        }
    if value is PermissionMode.WORKSPACE_WRITE:
        return {
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandbox": "workspace-write",
            "sandboxPolicy": {"type": "workspaceWrite"},
        }
    if value is PermissionMode.AUTO:
        return {
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
            "sandbox": "workspace-write",
            "sandboxPolicy": {"type": "workspaceWrite"},
        }
    return {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": "danger-full-access",
        "sandboxPolicy": {"type": "dangerFullAccess"},
    }


def is_recoverable_thread_resume_error(error: BaseException) -> bool:
    if not isinstance(error, JsonRpcRemoteError):
        return False
    message = error.remote_message.lower()
    return "thread" in message and any(
        snippet in message for snippet in _RECOVERABLE_RESUME_SNIPPETS
    )


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[Any] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def _canonical_item_type(value: object) -> str:
    raw = _text(value) or ""
    compact = raw.replace("_", "").replace("-", "").lower()
    return {
        "usermessage": "user_message",
        "agentmessage": "assistant_message",
        "assistantmessage": "assistant_message",
        "reasoning": "reasoning",
        "plan": "plan",
        "commandexecution": "command_execution",
        "filechange": "file_change",
        "mcptoolcall": "mcp_tool_call",
        "dynamictoolcall": "dynamic_tool_call",
        "websearch": "web_search",
        "imageview": "image_view",
        "contextcompaction": "context_compaction",
        "error": "error",
    }.get(compact, "unknown")


def _item_activity(item_type: str) -> str:
    if item_type == "file_change":
        return "coding"
    if item_type == "plan" or item_type == "context_compaction":
        return "planning"
    if item_type in _TOOL_ITEM_TYPES:
        return "tooling"
    return "thinking"


def _item_outcome(value: object, *, tool: bool) -> str:
    status = str(value or "").strip().lower()
    if status in {"completed", "complete", "succeeded", "success"}:
        return "succeeded"
    if status in {"declined", "cancelled", "canceled", "interrupted"}:
        return "cancelled"
    if status in {"failed", "error", "failure"}:
        return "failed"
    return "failed" if tool else "succeeded"


def _resume_thread_id(cursor: Mapping[str, Any] | None) -> str | None:
    if not cursor:
        return None
    return _text(cursor.get("thread_id")) or _text(cursor.get("threadId"))


def _detach_json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeProtocolError(
            "Codex returned a non-JSON object", provider=PROVIDER
        ) from error
    if not isinstance(decoded, dict):
        raise RuntimeProtocolError("Codex returned an invalid object", provider=PROVIDER)
    return decoded


@dataclass
class _PendingInteraction:
    id: str
    kind: str
    turn_id: str
    item_id: str
    provider_request_id: str
    inbound_request_id: str | int
    future: asyncio.Future[dict[str, Any]]
    question_ids: tuple[str, ...] = ()


@dataclass
class _CodexSessionState:
    spec: RuntimeSessionSpec
    permission_mode: PermissionMode
    cwd: str
    process: asyncio.subprocess.Process
    peer: AsyncJsonRpcPeer
    event_queue: asyncio.Queue[RuntimeEvent | object]
    state: str = "starting"
    model: str | None = None
    provider_thread_id: str | None = None
    active_turn_id: str | None = None
    pending: dict[str, _PendingInteraction] = field(default_factory=dict)
    pending_by_provider: dict[str, str] = field(default_factory=dict)
    resolved: dict[str, str] = field(default_factory=dict)
    stderr_task: asyncio.Task[None] | None = None
    process_task: asyncio.Task[None] | None = None
    peer_task: asyncio.Task[None] | None = None
    closing: bool = False
    exit_emitted: bool = False
    thread_started_emitted: bool = False
    shutdown_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_start_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_start_in_flight: bool = False
    terminal_turns_during_start: set[str] = field(default_factory=set)


class CodexRuntimeAdapter(RuntimeAdapter):
    provider = PROVIDER
    capabilities = RuntimeCapabilities(
        resume=True,
        interrupt=True,
        approvals=True,
        user_input=True,
        read_thread=True,
        rollback=True,
        model_switch=True,
    )

    def __init__(
        self,
        *,
        binary_path: str = "codex",
        app_server_args: Sequence[str] = (),
        command: Sequence[str] | None = None,
        home_path: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        isolation_enabled: bool = True,
        bubblewrap_path: str = "bwrap",
        host_state_dir: str | Path | None = None,
        isolation_probe_timeout: float = 5.0,
        client_version: str = "1",
        initialize_timeout: float = 15.0,
        request_timeout: float = 30.0,
        interrupt_timeout: float = 10.0,
        kill_timeout: float = 2.0,
        event_queue_size: int = 1024,
    ) -> None:
        explicit = tuple(str(value) for value in command or ())
        self._command = explicit or (
            str(binary_path),
            "app-server",
            *(str(value) for value in app_server_args),
        )
        if not self._command or not self._command[0]:
            raise ValueError("Codex runtime command is required")
        if any("\0" in value for value in self._command):
            raise ValueError("Codex runtime command contains an invalid argument")
        default_home = os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
        self.home_path = str(Path(home_path or default_home).expanduser().resolve())
        self.environment = dict(environment or {})
        self.isolation_enabled = bool(isolation_enabled)
        self.bubblewrap_path = str(bubblewrap_path or "")
        self.host_state_dir = (
            str(Path(host_state_dir).expanduser().resolve())
            if host_state_dir is not None
            else None
        )
        self.isolation_probe_timeout = max(0.05, float(isolation_probe_timeout))
        self.client_version = str(client_version or "1")
        self.initialize_timeout = max(0.05, float(initialize_timeout))
        self.request_timeout = max(0.05, float(request_timeout))
        self.interrupt_timeout = max(0.05, float(interrupt_timeout))
        self.kill_timeout = max(0.05, float(kill_timeout))
        self.event_queue_size = max(16, int(event_queue_size))
        self._sessions: dict[str, _CodexSessionState] = {}
        self._session_queues: dict[str, asyncio.Queue[RuntimeEvent | object]] = {}
        self._events: asyncio.Queue[RuntimeEvent | object] = asyncio.Queue(
            maxsize=self.event_queue_size
        )
        self._global_event_consumers = 0
        self._sessions_lock = asyncio.Lock()
        self._closed = False

    async def probe(self) -> RuntimeProbe:
        return await asyncio.to_thread(self.probe_sync)

    def validate_session(self, spec: RuntimeSessionSpec) -> None:
        """Validate deterministic launch inputs before the command side-effect fence."""

        session_id = _text(spec.session_id)
        if session_id is None or len(session_id) > 255:
            raise ValueError("runtime session_id must contain 1..255 characters")
        _permission_mode(spec.permission_mode)
        cwd_path = self._resolved_directory(spec.cwd, "Codex session cwd")
        if (
            spec.resume_cursor is not None
            and _resume_thread_id(spec.resume_cursor) is None
        ):
            raise ValueError("Codex resume_cursor requires thread_id")
        try:
            self._resolved_executable(self._command[0])
            if self.isolation_enabled:
                if not sys.platform.startswith("linux"):
                    raise ValueError("Codex process isolation requires Linux")
                self._resolved_executable(self.bubblewrap_path)
                self._isolation_paths(cwd_path)
        except FileNotFoundError as error:
            raise ValueError("Codex runtime dependency is unavailable") from error

    def probe_sync(self, *, cwd: str | Path | None = None) -> RuntimeProbe:
        """Probe the executable and, when enabled, the real sandbox boundary."""

        try:
            self._resolved_executable(self._command[0])
        except (OSError, ValueError):
            return RuntimeProbe(available=False, detail_code="binary_not_found")
        if not self.isolation_enabled:
            return RuntimeProbe(available=True)
        if not sys.platform.startswith("linux"):
            return RuntimeProbe(
                available=False, detail_code="isolation_unsupported_platform"
            )
        try:
            cwd_path = (
                self._resolved_directory(cwd, "Codex session cwd") if cwd else None
            )
            command = self._bubblewrap_command(cwd_path, ("/bin/true",))
            environment = self._spawn_environment(None, cwd=cwd_path)
        except FileNotFoundError:
            return RuntimeProbe(available=False, detail_code="bubblewrap_not_found")
        except (OSError, ValueError):
            return RuntimeProbe(available=False, detail_code="isolation_invalid")
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(cwd_path or Path("/")),
                env=environment,
                timeout=self.isolation_probe_timeout,
                check=False,
            )
        except FileNotFoundError:
            return RuntimeProbe(available=False, detail_code="bubblewrap_not_found")
        except (OSError, subprocess.TimeoutExpired):
            return RuntimeProbe(available=False, detail_code="isolation_probe_failed")
        if completed.returncode != 0:
            return RuntimeProbe(available=False, detail_code="isolation_probe_failed")
        return RuntimeProbe(available=True)

    @staticmethod
    def _resolved_directory(value: str | Path, label: str) -> Path:
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"{label} cannot be resolved") from error
        if not path.is_dir():
            raise ValueError(f"{label} must be an existing directory")
        return path

    @staticmethod
    def _resolved_executable(value: str) -> str:
        if not value or "\0" in value:
            raise ValueError("runtime executable is invalid")
        if os.path.sep in value or (os.path.altsep and os.path.altsep in value):
            path = Path(value).expanduser().absolute()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise FileNotFoundError("runtime executable is unavailable")
            return str(path)
        resolved = shutil.which(value)
        if not resolved:
            raise FileNotFoundError("runtime executable is unavailable")
        return str(Path(resolved).absolute())

    def _isolation_paths(self, cwd: Path | None) -> tuple[Path, Path]:
        if not self.host_state_dir:
            raise ValueError("Codex isolation requires the Runtime Host state directory")
        state_dir = Path(self.host_state_dir).expanduser().resolve()
        home = self._resolved_directory(self.home_path, "CODEX_HOME")
        if _paths_overlap(state_dir, home):
            raise ValueError("Runtime Host state directory overlaps CODEX_HOME")
        if cwd is not None:
            if _paths_overlap(state_dir, cwd):
                raise ValueError("Runtime Host state directory overlaps session cwd")
            if _paths_overlap(home, cwd):
                raise ValueError("CODEX_HOME overlaps session cwd")
        return state_dir, home

    def _bubblewrap_command(
        self,
        cwd: Path | None,
        provider_command: Sequence[str],
    ) -> tuple[str, ...]:
        if not self.isolation_enabled:
            raise ValueError("Codex process isolation is disabled")
        bubblewrap = self._resolved_executable(self.bubblewrap_path)
        state_dir, home = self._isolation_paths(cwd)
        command = [
            bubblewrap,
            "--die-with-parent",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--dev",
            "/dev",
        ]
        if cwd is not None:
            command.extend(("--bind", str(cwd), str(cwd)))
        command.extend(("--bind", str(home), str(home)))
        # This must remain the final mount operation: it hides device.credential,
        # runtime.db and journals even though the host root was bound above.
        command.extend(("--tmpfs", str(state_dir)))
        if cwd is not None:
            command.extend(("--chdir", str(cwd)))
        command.append("--")
        command.extend(str(value) for value in provider_command)
        return tuple(command)

    def _spawn_environment(
        self,
        session_environment: Mapping[str, str] | None,
        *,
        cwd: Path | None,
    ) -> dict[str, str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in _SAFE_INHERITED_ENVIRONMENT or name.startswith("LC_")
        }
        for source in (self.environment, dict(session_environment or {})):
            for raw_name, raw_value in source.items():
                name = str(raw_name)
                if not name or "=" in name or "\0" in name:
                    raise ValueError("Codex environment contains an invalid name")
                if not isinstance(raw_value, str) or "\0" in raw_value:
                    raise ValueError("Codex environment values must be strings")
                if name == "CODEX_HOME" or _unsafe_environment_name(name):
                    raise ValueError(f"Codex environment variable {name} is forbidden")
                environment[name] = raw_value
        for name in tuple(environment):
            if _unsafe_environment_name(name):
                environment.pop(name, None)
        environment["CODEX_HOME"] = self.home_path
        environment["TMPDIR"] = "/tmp"
        environment["TMP"] = "/tmp"
        environment["TEMP"] = "/tmp"
        if cwd is not None:
            environment["PWD"] = str(cwd)
        return environment

    async def start_session(self, spec: RuntimeSessionSpec) -> RuntimeSession:
        if self._closed:
            raise RuntimeSessionClosedError(
                "Codex runtime adapter is closed",
                provider=PROVIDER,
                operation="start_session",
            )
        session_id = _text(spec.session_id)
        if session_id is None or len(session_id) > 255:
            raise ValueError("runtime session_id must contain 1..255 characters")
        permission_mode = _permission_mode(spec.permission_mode)
        cwd_path = self._resolved_directory(spec.cwd, "Codex session cwd")
        if spec.resume_cursor is not None and _resume_thread_id(spec.resume_cursor) is None:
            raise ValueError("Codex resume_cursor requires thread_id")
        env = self._spawn_environment(spec.environment, cwd=cwd_path)
        try:
            provider_command = (
                self._resolved_executable(self._command[0]),
                *self._command[1:],
            )
        except OSError as error:
            raise RuntimeSpawnError(
                "Codex executable is unavailable",
                provider=PROVIDER,
                operation="spawn",
                cause=error,
            ) from error
        spawn_command = provider_command
        if self.isolation_enabled:
            probe = await asyncio.to_thread(self.probe_sync, cwd=cwd_path)
            if not probe.available:
                raise RuntimeSpawnError(
                    "Codex process isolation is unavailable",
                    provider=PROVIDER,
                    operation="isolation",
                )
            spawn_command = self._bubblewrap_command(cwd_path, provider_command)
        async with self._sessions_lock:
            if session_id in self._sessions:
                raise RuntimeRequestError(
                    "Codex session already exists",
                    provider=PROVIDER,
                    operation="start_session",
                )
            queue: asyncio.Queue[RuntimeEvent | object] = asyncio.Queue(
                maxsize=self.event_queue_size
            )
            self._session_queues[session_id] = queue

        spawn_options: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(cwd_path),
            "env": env,
            "limit": MAX_JSONRPC_MESSAGE_BYTES + 1,
        }
        if os.name != "nt":
            spawn_options["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            spawn_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = await asyncio.create_subprocess_exec(
                *spawn_command, **spawn_options
            )
        except OSError as error:
            self._session_queues.pop(session_id, None)
            raise RuntimeSpawnError(
                "Failed to start Codex app-server",
                provider=PROVIDER,
                operation="spawn",
                cause=error,
            ) from error
        if process.stdout is None or process.stdin is None or process.stderr is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            self._session_queues.pop(session_id, None)
            raise RuntimeSpawnError(
                "Codex app-server pipes were not created",
                provider=PROVIDER,
                operation="spawn",
            )

        peer = AsyncJsonRpcPeer(process.stdout, process.stdin)
        session = _CodexSessionState(
            spec=replace(spec, session_id=session_id, cwd=str(cwd_path)),
            permission_mode=permission_mode,
            cwd=str(cwd_path),
            model=spec.model,
            process=process,
            peer=peer,
            event_queue=queue,
        )
        peer.handle_request(
            "item/commandExecution/requestApproval",
            lambda params: self._open_approval(
                session, "command_execution_approval", params
            ),
        )
        peer.handle_request(
            "item/fileChange/requestApproval",
            lambda params: self._open_approval(
                session, "file_change_approval", params
            ),
        )
        peer.handle_request(
            "item/tool/requestUserInput",
            lambda params: self._open_user_input(session, params),
        )
        peer.handle_unknown_notification(
            lambda method, params: self._handle_notification(session, method, params)
        )
        async with self._sessions_lock:
            self._sessions[session_id] = session
        await peer.start()
        session.stderr_task = asyncio.create_task(
            self._drain_stderr(session), name=f"codex-stderr:{session_id}"
        )
        session.process_task = asyncio.create_task(
            self._watch_process(session), name=f"codex-process:{session_id}"
        )
        session.peer_task = asyncio.create_task(
            self._watch_peer(session), name=f"codex-peer:{session_id}"
        )
        await self._emit(
            session,
            "session.state.changed",
            {"state": "starting"},
        )
        try:
            initialized = await self._request(
                session,
                "initialize",
                {
                    "clientInfo": {
                        "name": "agentserver_runtime",
                        "title": "AgentServer Runtime",
                        "version": self.client_version,
                    },
                    "capabilities": {"experimentalApi": True},
                },
                timeout=self.initialize_timeout,
            )
            self._validate_initialize_response(initialized)
            await peer.notify("initialized")
            opened = await self._open_thread(session)
            thread = _mapping(opened.get("thread"))
            assert thread is not None
            provider_thread_id = _text(thread.get("id"))
            assert provider_thread_id is not None
            session.provider_thread_id = provider_thread_id
            session.cwd = _text(opened.get("cwd")) or session.cwd
            session.model = _text(opened.get("model")) or session.model
            session.state = "ready"
            if not session.thread_started_emitted:
                session.thread_started_emitted = True
                await self._emit(
                    session,
                    "thread.started",
                    {"provider_thread_id": provider_thread_id},
                )
            await self._emit(
                session,
                "session.started",
                {
                    "state": "ready",
                    "provider_session_id": provider_thread_id,
                },
            )
            await self._emit(
                session, "session.state.changed", {"state": "ready"}
            )
            return self._session_view(session)
        except BaseException as error:
            mapped = self._map_error(error, "start_session")
            session.closing = True
            session.state = "error"
            with contextlib.suppress(BaseException):
                await self._emit(
                    session,
                    "session.state.changed",
                    {
                        "state": "error",
                        "code": getattr(mapped, "code", "cancelled"),
                    },
                )
            await self._shutdown_session(session, graceful=False)
            if mapped is error:
                raise
            raise mapped from error

    async def send_turn(
        self, session_id: str, turn: RuntimeTurnInput
    ) -> RuntimeTurn:
        session = self._require_session(session_id)
        if turn.text is None and not turn.attachments:
            raise ValueError("Codex turn requires text or an attachment")
        provider_thread_id = self._provider_thread_id(session)
        inputs: list[dict[str, Any]] = []
        if turn.text is not None:
            inputs.append({"type": "text", "text": str(turn.text)})
        for attachment in turn.attachments:
            if not isinstance(attachment, RuntimeAttachment):
                raise ValueError("Codex attachments must be RuntimeAttachment values")
            if attachment.type != "image" or not _text(attachment.url):
                raise ValueError("Codex supports only image URL attachments")
            inputs.append({"type": "image", "url": attachment.url})
        config = codex_permission_config(session.permission_mode)
        params: dict[str, Any] = {
            "threadId": provider_thread_id,
            "input": inputs,
            "approvalPolicy": config["approvalPolicy"],
            "approvalsReviewer": config["approvalsReviewer"],
            "sandboxPolicy": config["sandboxPolicy"],
        }
        model = _text(turn.model) or session.model
        if model:
            params["model"] = model
        if _text(turn.service_tier):
            params["serviceTier"] = turn.service_tier
        if _text(turn.effort):
            params["effort"] = turn.effort
        async with session.turn_start_lock:
            session.turn_start_in_flight = True
            session.terminal_turns_during_start.clear()
            try:
                response = await self._request(session, "turn/start", params)
                body = _mapping(response)
                result_turn = _mapping(body.get("turn")) if body else None
                turn_id = _text(result_turn.get("id")) if result_turn else None
                if turn_id is None:
                    raise RuntimeProtocolError(
                        "Codex turn/start response has no turn id",
                        provider=PROVIDER,
                        operation="turn/start",
                    )
                # Codex may emit turn/completed immediately after the response.
                # The reader can process that notification before this coroutine
                # resumes, so never overwrite its terminal state with "running".
                if turn_id not in session.terminal_turns_during_start:
                    session.state = "running"
                    session.active_turn_id = session.active_turn_id or turn_id
                if model:
                    session.model = model
                return RuntimeTurn(
                    session_id=session.spec.session_id,
                    turn_id=turn_id,
                    resume_cursor={"thread_id": provider_thread_id},
                )
            finally:
                session.turn_start_in_flight = False
                session.terminal_turns_during_start.clear()

    async def interrupt_turn(
        self, session_id: str, turn_id: str | None = None
    ) -> None:
        session = self._require_session(session_id)
        effective = _text(turn_id) or session.active_turn_id
        if not effective:
            return
        await self._request(
            session,
            "turn/interrupt",
            {
                "threadId": self._provider_thread_id(session),
                "turnId": effective,
            },
            timeout=self.interrupt_timeout,
        )

    async def respond_to_approval(
        self,
        session_id: str,
        interaction_id: str,
        decision: ApprovalDecision | str,
    ) -> None:
        session = self._require_session(session_id)
        pending = self._pending_interaction(session, interaction_id)
        if pending.kind not in {
            "command_execution_approval",
            "file_change_approval",
        }:
            raise RuntimeInvalidDecisionError(
                "interaction is not an approval request",
                provider=PROVIDER,
                operation="respond_to_approval",
            )
        try:
            normalized = (
                decision
                if isinstance(decision, ApprovalDecision)
                else ApprovalDecision(str(decision))
            )
        except ValueError as error:
            raise RuntimeInvalidDecisionError(
                "unsupported approval decision",
                provider=PROVIDER,
                operation="respond_to_approval",
            ) from error
        provider_decision = {
            ApprovalDecision.APPROVE_ONCE: "accept",
            ApprovalDecision.APPROVE_SESSION: "acceptForSession",
            ApprovalDecision.DENY: "decline",
            ApprovalDecision.CANCEL_TURN: "cancel",
        }[normalized]
        if pending.future.done():
            raise RuntimeInteractionResolvedError(
                "runtime interaction is already resolved",
                provider=PROVIDER,
                operation="respond_to_approval",
            )
        pending.future.set_result({"decision": provider_decision})
        await self._mark_resolved(
            session,
            pending,
            normalized.value,
            event_type="interaction.resolved",
        )
        await session.peer.wait_inbound_response(pending.inbound_request_id)

    async def respond_to_user_input(
        self,
        session_id: str,
        interaction_id: str,
        answers: Mapping[str, str | Sequence[str]],
    ) -> None:
        session = self._require_session(session_id)
        pending = self._pending_interaction(session, interaction_id)
        if pending.kind != "tool_user_input":
            raise RuntimeInvalidDecisionError(
                "interaction is not a user-input request",
                provider=PROVIDER,
                operation="respond_to_user_input",
            )
        if not isinstance(answers, Mapping):
            raise RuntimeInvalidDecisionError(
                "user-input answers must be an object",
                provider=PROVIDER,
                operation="respond_to_user_input",
            )
        question_ids = set(pending.question_ids)
        if set(answers) != question_ids:
            raise RuntimeInvalidDecisionError(
                "user-input answers must match the requested question ids",
                provider=PROVIDER,
                operation="respond_to_user_input",
            )
        encoded: dict[str, dict[str, list[str]]] = {}
        for question_id, answer in answers.items():
            if isinstance(answer, str):
                values = [answer]
            elif isinstance(answer, Sequence) and not isinstance(
                answer, (bytes, bytearray)
            ):
                values = list(answer)
            else:
                raise RuntimeInvalidDecisionError(
                    "each user-input answer must be text or a text list",
                    provider=PROVIDER,
                    operation="respond_to_user_input",
                )
            if not values or any(not isinstance(value, str) for value in values):
                raise RuntimeInvalidDecisionError(
                    "each user-input answer must contain text",
                    provider=PROVIDER,
                    operation="respond_to_user_input",
                )
            encoded[str(question_id)] = {"answers": values}
        if pending.future.done():
            raise RuntimeInteractionResolvedError(
                "runtime interaction is already resolved",
                provider=PROVIDER,
                operation="respond_to_user_input",
            )
        pending.future.set_result({"answers": encoded})
        await self._mark_resolved(
            session,
            pending,
            "answered",
            event_type="interaction.resolved",
            extra={"question_ids": sorted(encoded)},
        )
        await session.peer.wait_inbound_response(pending.inbound_request_id)

    async def read_thread(self, session_id: str) -> RuntimeThreadSnapshot:
        session = self._require_session(session_id)
        response = await self._request(
            session,
            "thread/read",
            {"threadId": self._provider_thread_id(session), "includeTurns": True},
        )
        return self._thread_snapshot(response, operation="thread/read")

    async def rollback_thread(
        self, session_id: str, num_turns: int
    ) -> RuntimeThreadSnapshot:
        if not isinstance(num_turns, int) or isinstance(num_turns, bool) or num_turns < 1:
            raise ValueError("num_turns must be an integer >= 1")
        session = self._require_session(session_id)
        response = await self._request(
            session,
            "thread/rollback",
            {"threadId": self._provider_thread_id(session), "numTurns": num_turns},
        )
        session.active_turn_id = None
        session.state = "ready"
        return self._thread_snapshot(response, operation="thread/rollback")

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.get(str(session_id))
        if session is None:
            return
        await self._shutdown_session(session, graceful=True)

    async def list_sessions(self) -> tuple[RuntimeSession, ...]:
        return tuple(
            self._session_view(session)
            for session in self._sessions.values()
            if not session.closing
        )

    def events(self, session_id: str | None = None) -> AsyncIterator[RuntimeEvent]:
        if session_id is None:
            queue = self._events
        else:
            try:
                queue = self._session_queues[str(session_id)]
            except KeyError as error:
                raise RuntimeSessionNotFoundError(
                    "Codex session event stream does not exist",
                    provider=PROVIDER,
                    operation="events",
                ) from error

        global_stream = session_id is None

        async def consume() -> AsyncIterator[RuntimeEvent]:
            if global_stream:
                self._global_event_consumers += 1
            try:
                while True:
                    value = await queue.get()
                    if value is _END:
                        return
                    assert isinstance(value, RuntimeEvent)
                    yield value
            finally:
                if global_stream:
                    self._global_event_consumers = max(
                        0, self._global_event_consumers - 1
                    )

        return consume()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(self._shutdown_session(session, graceful=True) for session in tuple(self._sessions.values())),
            return_exceptions=True,
        )
        self._queue_end(self._events)

    async def _open_thread(self, session: _CodexSessionState) -> Mapping[str, Any]:
        config = codex_permission_config(session.permission_mode)
        common: dict[str, Any] = {
            "cwd": session.cwd,
            "approvalPolicy": config["approvalPolicy"],
            "approvalsReviewer": config["approvalsReviewer"],
            "sandbox": config["sandbox"],
        }
        if _text(session.spec.model):
            common["model"] = session.spec.model
        if _text(session.spec.service_tier):
            common["serviceTier"] = session.spec.service_tier
        resume_thread_id = _resume_thread_id(session.spec.resume_cursor)
        if resume_thread_id:
            try:
                response = await session.peer.request(
                    "thread/resume",
                    {"threadId": resume_thread_id, **common},
                    timeout=self.request_timeout,
                )
            except JsonRpcRemoteError as error:
                if not is_recoverable_thread_resume_error(error):
                    raise
                await self._emit(
                    session,
                    "runtime.warning",
                    {"code": "thread_resume_fell_back"},
                )
                response = await session.peer.request(
                    "thread/start", common, timeout=self.request_timeout
                )
        else:
            response = await session.peer.request(
                "thread/start", common, timeout=self.request_timeout
            )
        body = _mapping(response)
        thread = _mapping(body.get("thread")) if body else None
        if thread is None or _text(thread.get("id")) is None:
            raise RuntimeProtocolError(
                "Codex thread response has no thread id",
                provider=PROVIDER,
                operation="thread/open",
            )
        return body

    async def _request(
        self,
        session: _CodexSessionState,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        try:
            return await session.peer.request(
                method,
                params,
                timeout=self.request_timeout if timeout is None else timeout,
            )
        except BaseException as error:
            raise self._map_error(error, method) from error

    @staticmethod
    def _validate_initialize_response(value: object) -> None:
        payload = _mapping(value)
        if payload is None or any(
            _text(payload.get(field)) is None
            for field in ("userAgent", "platformFamily", "platformOs")
        ):
            raise RuntimeProtocolError(
                "Codex initialize response is invalid",
                provider=PROVIDER,
                operation="initialize",
            )

    def _require_session(self, session_id: str) -> _CodexSessionState:
        try:
            session = self._sessions[str(session_id)]
        except KeyError as error:
            raise RuntimeSessionNotFoundError(
                "Codex session does not exist",
                provider=PROVIDER,
                operation="session",
            ) from error
        if session.closing or session.state in {"stopped", "error"}:
            raise RuntimeSessionClosedError(
                "Codex session is closed",
                provider=PROVIDER,
                operation="session",
            )
        return session

    @staticmethod
    def _provider_thread_id(session: _CodexSessionState) -> str:
        if not session.provider_thread_id:
            raise RuntimeSessionNotFoundError(
                "Codex session has no provider thread",
                provider=PROVIDER,
                operation="session",
            )
        return session.provider_thread_id

    @staticmethod
    def _session_view(session: _CodexSessionState) -> RuntimeSession:
        return RuntimeSession(
            session_id=session.spec.session_id,
            provider=PROVIDER,
            state=session.state,
            cwd=session.cwd,
            model=session.model,
            active_turn_id=session.active_turn_id,
            resume_cursor=(
                {"thread_id": session.provider_thread_id}
                if session.provider_thread_id
                else None
            ),
        )

    async def _open_approval(
        self, session: _CodexSessionState, kind: str, params: object
    ) -> dict[str, Any]:
        payload = self._validate_interaction_params(session, params)
        return await self._open_interaction(session, kind, payload)

    async def _open_user_input(
        self, session: _CodexSessionState, params: object
    ) -> dict[str, Any]:
        payload = self._validate_interaction_params(session, params)
        raw_questions = _sequence(payload.get("questions"))
        if not raw_questions:
            raise JsonRpcRequestError(-32602, "User input questions are required")
        questions: list[dict[str, Any]] = []
        question_ids: list[str] = []
        for raw in raw_questions:
            question = _mapping(raw)
            question_id = _text(question.get("id")) if question else None
            header = _text(question.get("header")) if question else None
            prompt = _text(question.get("question")) if question else None
            if not question_id or not header or not prompt:
                raise JsonRpcRequestError(-32602, "User input question is invalid")
            options: list[dict[str, str]] = []
            for raw_option in _sequence(question.get("options")) or ():
                option = _mapping(raw_option)
                label = _text(option.get("label")) if option else None
                description = _text(option.get("description")) if option else None
                if label and description:
                    options.append({"label": label, "description": description})
            questions.append(
                {
                    "id": question_id,
                    "header": header,
                    "question": prompt,
                    "options": options,
                }
            )
            question_ids.append(question_id)
        return await self._open_interaction(
            session,
            "tool_user_input",
            payload,
            questions=questions,
            question_ids=tuple(question_ids),
        )

    def _validate_interaction_params(
        self, session: _CodexSessionState, params: object
    ) -> Mapping[str, Any]:
        payload = _mapping(params)
        if payload is None:
            raise JsonRpcRequestError(-32602, "Interaction params must be an object")
        thread_id = _text(payload.get("threadId"))
        turn_id = _text(payload.get("turnId"))
        item_id = _text(payload.get("itemId"))
        if not thread_id or not turn_id or not item_id:
            raise JsonRpcRequestError(-32602, "Interaction routing fields are required")
        if session.provider_thread_id and thread_id != session.provider_thread_id:
            raise JsonRpcRequestError(-32602, "Interaction thread is not active")
        return payload

    async def _open_interaction(
        self,
        session: _CodexSessionState,
        kind: str,
        payload: Mapping[str, Any],
        *,
        questions: list[dict[str, Any]] | None = None,
        question_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        turn_id = str(payload["turnId"])
        item_id = str(payload["itemId"])
        provider_request_id = (
            _text(payload.get("approvalId")) or item_id
        )
        inbound_request_id = session.peer.current_inbound_request_id()
        if inbound_request_id is None:
            raise JsonRpcRequestError(
                -32603, "Interaction request context is unavailable"
            )
        interaction_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        pending = _PendingInteraction(
            id=interaction_id,
            kind=kind,
            turn_id=turn_id,
            item_id=item_id,
            provider_request_id=provider_request_id,
            inbound_request_id=inbound_request_id,
            future=future,
            question_ids=question_ids,
        )
        session.pending[interaction_id] = pending
        session.pending_by_provider[provider_request_id] = interaction_id
        public_payload: dict[str, Any] = {
            "interaction_id": interaction_id,
            "request_type": kind,
        }
        if questions is not None:
            public_payload["questions"] = questions
        await self._emit(
            session,
            "interaction.opened",
            public_payload,
            turn_id=turn_id,
            item_id=item_id,
            interaction_id=interaction_id,
        )
        try:
            return await future
        except asyncio.CancelledError:
            if interaction_id not in session.resolved:
                await self._mark_resolved(
                    session,
                    pending,
                    "runtime_closed",
                    event_type="interaction.resolved",
                )
            raise
        finally:
            session.pending.pop(interaction_id, None)
            if session.pending_by_provider.get(provider_request_id) == interaction_id:
                session.pending_by_provider.pop(provider_request_id, None)

    def _pending_interaction(
        self, session: _CodexSessionState, interaction_id: str
    ) -> _PendingInteraction:
        identifier = str(interaction_id or "")
        if identifier in session.resolved:
            raise RuntimeInteractionResolvedError(
                "runtime interaction is already resolved",
                provider=PROVIDER,
                operation="interaction",
            )
        try:
            return session.pending[identifier]
        except KeyError as error:
            raise RuntimeInteractionNotFoundError(
                "runtime interaction does not exist",
                provider=PROVIDER,
                operation="interaction",
            ) from error

    async def _mark_resolved(
        self,
        session: _CodexSessionState,
        pending: _PendingInteraction,
        resolution: str,
        *,
        event_type: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if pending.id in session.resolved:
            return
        session.resolved[pending.id] = resolution
        await self._emit(
            session,
            event_type,
            {
                "interaction_id": pending.id,
                "request_type": pending.kind,
                "resolution": resolution,
                **dict(extra or {}),
            },
            turn_id=pending.turn_id,
            item_id=pending.item_id,
            interaction_id=pending.id,
        )

    async def _settle_pending(
        self,
        session: _CodexSessionState,
        *,
        reason: str,
        turn_id: str | None = None,
    ) -> None:
        for pending in tuple(session.pending.values()):
            if turn_id is not None and pending.turn_id != turn_id:
                continue
            if pending.future.done():
                continue
            if pending.kind == "tool_user_input":
                pending.future.set_result({"answers": {}})
            else:
                pending.future.set_result({"decision": "cancel"})
            await self._mark_resolved(
                session,
                pending,
                reason,
                event_type="interaction.resolved",
            )

    async def _handle_notification(
        self, session: _CodexSessionState, method: str, params: object
    ) -> None:
        display_delta = _DISPLAY_DELTA_TYPES.get(method)
        if display_delta is not None:
            payload = _mapping(params)
            text = _display_delta(payload.get("delta") if payload else None)
            if text is not None:
                turn_id = _text(payload.get("turnId")) if payload else None
                item_id = _text(
                    (payload or {}).get("itemId") or (payload or {}).get("item_id")
                )
                if turn_id is None and payload:
                    turn = _mapping(payload.get("turn"))
                    turn_id = _text(turn.get("id")) if turn else None
                await self._emit(
                    session,
                    display_delta[0],
                    {"text": text, "channel": display_delta[1]},
                    turn_id=turn_id,
                    item_id=item_id,
                )
            return
        if method in _DELTA_METHODS or method.endswith("/delta"):
            # Unknown deltas stay private until an explicit display projection
            # is defined for the provider method.
            return
        payload = _mapping(params)
        if method == "thread/started":
            thread = _mapping(payload.get("thread")) if payload else None
            thread_id = _text(thread.get("id")) if thread else None
            if not thread_id:
                return
            if session.provider_thread_id and thread_id != session.provider_thread_id:
                return
            session.provider_thread_id = thread_id
            if not session.thread_started_emitted:
                session.thread_started_emitted = True
                await self._emit(
                    session,
                    "thread.started",
                    {"provider_thread_id": thread_id},
                )
            return
        if method == "turn/started":
            turn = _mapping(payload.get("turn")) if payload else None
            turn_id = _text(turn.get("id")) if turn else None
            if not turn_id or not self._belongs_to_session(session, payload):
                return
            session.active_turn_id = turn_id
            session.state = "running"
            await self._emit(
                session,
                "turn.started",
                {"activity": "thinking"},
                turn_id=turn_id,
            )
            return
        if method == "turn/completed":
            turn = _mapping(payload.get("turn")) if payload else None
            turn_id = _text(turn.get("id")) if turn else None
            if not turn_id or not self._belongs_to_session(session, payload):
                return
            status = str(turn.get("status") or "failed")
            state = (
                status
                if status in {"completed", "failed", "interrupted"}
                else "interrupted"
                if status in {"cancelled", "canceled"}
                else "failed"
            )
            if session.turn_start_in_flight:
                session.terminal_turns_during_start.add(turn_id)
            if session.active_turn_id == turn_id:
                session.active_turn_id = None
            # A failed turn is recoverable; it must not make the provider
            # session unusable for all subsequent turns.
            session.state = "ready"
            await self._settle_pending(session, reason="turn_completed", turn_id=turn_id)
            event_type = "turn.failed" if state == "failed" else "turn.completed"
            event_payload: dict[str, Any] = {
                "state": state,
                "activity": "finalizing",
            }
            if state == "failed":
                event_payload["error"] = "provider_turn_failed"
            await self._emit(
                session,
                event_type,
                event_payload,
                turn_id=turn_id,
            )
            return
        if method == "turn/plan/updated":
            if not payload or not self._belongs_to_session(session, payload):
                return
            turn_id = _text(payload.get("turnId"))
            plan = _sequence(payload.get("plan")) or ()
            await self._emit(
                session,
                "turn.plan.updated",
                {"activity": "planning", "step_count": len(plan)},
                turn_id=turn_id,
            )
            return
        if method in {"item/started", "item/completed"}:
            await self._handle_item_notification(session, method, payload)
            return
        if method == "serverRequest/resolved":
            request_id = _text(payload.get("requestId")) if payload else None
            local_id = session.pending_by_provider.get(request_id or "")
            pending = session.pending.get(local_id or "")
            if pending and not pending.future.done():
                pending.future.cancel()
                await self._mark_resolved(
                    session,
                    pending,
                    "provider_cleared",
                    event_type="interaction.resolved",
                )
            return
        if method == "thread/status/changed":
            if not payload or not self._belongs_to_session(session, payload):
                return
            status = _mapping(payload.get("status"))
            state_type = _text(status.get("type")) if status else None
            state = {
                "idle": "ready",
                "active": "running",
                "systemError": "error",
                "notLoaded": "stopped",
            }.get(state_type or "", session.state)
            session.state = state
            await self._emit(
                session, "session.state.changed", {"state": state}
            )
            return
        if method in {"thread/closed", "thread/compacted"}:
            if not payload or not self._belongs_to_session(session, payload):
                return
            await self._emit(
                session,
                "thread.state.changed",
                {"state": "closed" if method == "thread/closed" else "compacted"},
            )
            return
        if method == "error":
            if payload and not self._belongs_to_session(session, payload):
                return
            retrying = bool(payload.get("willRetry")) if payload else False
            if not retrying:
                session.state = "error"
            await self._emit(
                session,
                "runtime.warning" if retrying else "runtime.error",
                {"code": "provider_error", "retrying": retrying},
                turn_id=_text(payload.get("turnId")) if payload else None,
            )

    async def _handle_item_notification(
        self,
        session: _CodexSessionState,
        method: str,
        payload: Mapping[str, Any] | None,
    ) -> None:
        if not payload or not self._belongs_to_session(session, payload):
            return
        item = _mapping(payload.get("item"))
        if item is None:
            return
        item_id = _text(item.get("id"))
        turn_id = _text(payload.get("turnId"))
        if not item_id or not turn_id:
            return
        item_type = _canonical_item_type(item.get("type"))
        if item_type == "unknown":
            return
        if method == "item/completed" and item_type == "assistant_message":
            text = _completed_item_text(item)
            if text:
                await self._emit(
                    session,
                    "message.completed",
                    {"text": text, "role": "assistant", "final": True, "streaming": False},
                    turn_id=turn_id,
                    item_id=item_id,
                )
            return
        activity = _item_activity(item_type)
        body: dict[str, Any] = {"item_type": item_type, "activity": activity}
        if method == "item/started":
            body["status"] = "in_progress"
            event_type = "item.started"
        else:
            body["status"] = _item_outcome(
                item.get("status"), tool=item_type in _TOOL_ITEM_TYPES
            )
            event_type = "item.completed"
        await self._emit(
            session,
            event_type,
            body,
            turn_id=turn_id,
            item_id=item_id,
        )

    @staticmethod
    def _belongs_to_session(
        session: _CodexSessionState, payload: Mapping[str, Any] | None
    ) -> bool:
        if not payload or not session.provider_thread_id:
            return True
        thread_id = _text(payload.get("threadId"))
        return thread_id is None or thread_id == session.provider_thread_id

    async def _emit(
        self,
        session: _CodexSessionState,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        turn_id: str | None = None,
        item_id: str | None = None,
        interaction_id: str | None = None,
    ) -> None:
        event = RuntimeEvent(
            event_id=uuid.uuid4().hex,
            provider=PROVIDER,
            session_id=session.spec.session_id,
            type=event_type,
            payload=_detach_json_object(payload),
            turn_id=turn_id,
            item_id=item_id,
            interaction_id=interaction_id,
            occurred_at=time.time(),
        )
        await session.event_queue.put(event)
        # The Host consumes a session-specific stream. An unobserved global
        # stream must never apply backpressure and deadlock a long session.
        if self._global_event_consumers:
            await self._events.put(event)

    async def _drain_stderr(self, session: _CodexSessionState) -> None:
        stream = session.process.stderr
        if stream is None:
            return
        try:
            while await stream.read(64 * 1024):
                pass
        except (ConnectionError, OSError, ValueError):
            return

    async def _watch_process(self, session: _CodexSessionState) -> None:
        try:
            exit_code = await session.process.wait()
        except asyncio.CancelledError:
            return
        if session.closing:
            return
        session.closing = True
        await self._finish_unexpected(session, exit_code=exit_code)

    async def _watch_peer(self, session: _CodexSessionState) -> None:
        try:
            await session.peer.wait_closed()
        except asyncio.CancelledError:
            return
        if session.closing:
            return
        session.closing = True
        await self._terminate_process(session)
        await self._finish_unexpected(
            session, exit_code=session.process.returncode
        )

    async def _finish_unexpected(
        self, session: _CodexSessionState, *, exit_code: int | None
    ) -> None:
        session.state = "error"
        session.active_turn_id = None
        await self._settle_pending(session, reason="runtime_exited")
        if not session.peer.closed:
            await session.peer.close()
        if not session.exit_emitted:
            session.exit_emitted = True
            await self._emit(
                session,
                "session.failed",
                {"error": "runtime_exited"},
            )
            await self._emit(
                session,
                "session.exited",
                {
                    "exit_kind": "error",
                    "exit_code": exit_code if exit_code is not None else -1,
                },
            )
        await self._remove_session(session)

    async def _shutdown_session(
        self, session: _CodexSessionState, *, graceful: bool
    ) -> None:
        async with session.shutdown_lock:
            if session.state == "stopped" and session.spec.session_id not in self._sessions:
                return
            session.closing = True
            await self._settle_pending(session, reason="session_closed")
            if not session.peer.closed:
                await session.peer.wait_inbound(timeout=0.5)
                await session.peer.close()
            await self._terminate_process(session)
            session.active_turn_id = None
            session.state = "stopped" if graceful else "error"
            if not session.exit_emitted:
                session.exit_emitted = True
                await self._emit(
                    session,
                    "session.stopped" if graceful else "session.failed",
                    {} if graceful else {"error": "runtime_start_failed"},
                )
                await self._emit(
                    session,
                    "session.exited",
                    {
                        "exit_kind": "graceful" if graceful else "error",
                        "exit_code": (
                            session.process.returncode
                            if session.process.returncode is not None
                            else -1
                        ),
                    },
                )
            await self._remove_session(session)

    async def _remove_session(self, session: _CodexSessionState) -> None:
        current = asyncio.current_task()
        for task in (session.stderr_task, session.process_task, session.peer_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        async with self._sessions_lock:
            if self._sessions.get(session.spec.session_id) is session:
                self._sessions.pop(session.spec.session_id, None)
        self._queue_end(session.event_queue)

    async def _terminate_process(self, session: _CodexSessionState) -> None:
        process = session.process
        if process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self.kill_timeout)
            return
        except asyncio.TimeoutError:
            pass
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            pass
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=self.kill_timeout)

    @staticmethod
    def _queue_end(queue: asyncio.Queue[RuntimeEvent | object]) -> None:
        try:
            queue.put_nowait(_END)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(_END)

    @staticmethod
    def _thread_snapshot(value: object, *, operation: str) -> RuntimeThreadSnapshot:
        payload = _mapping(value)
        thread = _mapping(payload.get("thread")) if payload else None
        thread_id = _text(thread.get("id")) if thread else None
        raw_turns = _sequence(thread.get("turns")) if thread else None
        if not thread_id or raw_turns is None:
            raise RuntimeProtocolError(
                "Codex thread snapshot is invalid",
                provider=PROVIDER,
                operation=operation,
            )
        turns: list[RuntimeThreadTurnSnapshot] = []
        for raw_turn in raw_turns:
            turn = _mapping(raw_turn)
            turn_id = _text(turn.get("id")) if turn else None
            if not turn_id:
                continue
            items: list[Mapping[str, Any]] = []
            for raw_item in _sequence(turn.get("items")) or ():
                item = _mapping(raw_item)
                if item is None:
                    continue
                # Snapshots deliberately expose lifecycle identifiers only.
                public = {
                    key: item[key]
                    for key in ("id", "type", "status")
                    if key in item and isinstance(item[key], (str, int, float, bool, type(None)))
                }
                items.append(_detach_json_object(public))
            turns.append(RuntimeThreadTurnSnapshot(id=turn_id, items=tuple(items)))
        return RuntimeThreadSnapshot(thread_id=thread_id, turns=tuple(turns))

    @staticmethod
    def _map_error(error: BaseException, operation: str):
        if isinstance(error, (RuntimeProtocolError, RuntimeRequestError)):
            return error
        if isinstance(error, JsonRpcRequestTimeout):
            return RuntimeOperationTimeoutError(
                "Codex runtime operation timed out",
                provider=PROVIDER,
                operation=operation,
                cause=error,
            )
        if isinstance(error, JsonRpcRemoteError):
            return RuntimeRequestError(
                "Codex app-server rejected a request",
                provider=PROVIDER,
                operation=operation,
                request_code=error.code,
                retryable=error.code == -32001,
                cause=error,
            )
        if isinstance(error, JsonRpcProtocolError):
            return RuntimeProtocolError(
                "Codex app-server protocol failed",
                provider=PROVIDER,
                operation=operation,
                cause=error,
            )
        if isinstance(error, (JsonRpcTransportError, JsonRpcPeerError)):
            return RuntimeTransportError(
                "Codex app-server transport failed",
                provider=PROVIDER,
                operation=operation,
                cause=error,
            )
        if isinstance(error, asyncio.CancelledError):
            return error
        return RuntimeProtocolError(
            "Codex runtime operation failed",
            provider=PROVIDER,
            operation=operation,
            cause=error,
        )
