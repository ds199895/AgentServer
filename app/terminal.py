from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import shlex
import shutil
import sqlite3
import stat
import struct
import subprocess
import termios
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping


RESIZE_MESSAGE = re.compile(r"^\x01\[(\d+),(\d+)\]$")
SNAPSHOT_COMPLETE_MESSAGE = "\x01[snapshot-complete]"


class StreamGap:
    """Queued when a subscriber falls so far behind that its stream is lost.

    A terminal stream is raw VT bytes, so it cannot be repaired by discarding
    part of it. The only correct recovery is for that client to reconnect and
    replay a fresh snapshot.
    """

    __slots__ = ()


STREAM_GAP = StreamGap()
ANSI_ESCAPE = re.compile(
    r"(?:\x1B\][^\x07]*(?:\x07|\x1B\\)|\x1B[@-_][0-?]*[ -/]*[@-~])"
)
LOCAL_SERVICE_URL = re.compile(
    r"(?P<url>https?://(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[?::1\]?|\[?::\]?)"
    r":(?P<port>\d{1,5})(?:/[^\s\x00-\x1f<>'\"]*)?)",
    re.IGNORECASE,
)
LOCAL_SERVICE_PORT = re.compile(
    r"\b(?:listening|running|started|ready|available|serving)\b"
    r"[^\r\n]{0,80}?(?:\bport\s*[:=]?\s*|(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\s*:)"
    r"(?P<port>\d{2,5})\b",
    re.IGNORECASE,
)
SERVICE_URL_CONTEXT = re.compile(
    r"(?:^|\s)(?:local|network)\s*:|"
    r"\b(?:listening|running|started|ready|available|serving)\b[^\r\n]{0,40}\b(?:at|on)\b|"
    r"(?:服务|前端|后端|监听|运行|启动|就绪|可用|正常|代理)[^\r\n]{0,80}",
    re.IGNORECASE,
)
NON_SERVICE_URL_CONTEXT = re.compile(
    r"(?:^|\s)(?:curl|wget|fetch|httpie)\s|"
    r"\b(?:docs?|documentation|example|configuration|config)\b|"
    r"(?:文档|示例|配置)(?:地址|链接|值|项)?\s*[:：]?",
    re.IGNORECASE,
)
MAX_DETECTED_SERVICES = 8
MAX_PROCESS_SERVICE_CANDIDATES = 20
SERVICE_REDISCOVERY_COOLDOWN = 5 * 60
LISTENER_SCAN_MARKER = "__AGENTSERVER_LISTENERS__"
LISTENER_RECORD_MARKER = "__AGENTSERVER_LISTENER__"
AGENT_SCAN_MARKER = "__AGENTSERVER_AGENTS__"
AGENT_RECORD_MARKER = "__AGENTSERVER_AGENT__"
# Agent signatures are empirically tuned per agent version: distinctive
# startup-banner / TUI substrings observed in the first seconds of output
# (the same maintenance model as service_product below). When an agent
# rebrands its banner, capture fresh output and edit this table.
AGENT_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("codex", ("openai codex", "codex-cli")),
    ("claude", ("claude code",)),
    ("kimi", ("kimi cli", "kimi-cli")),
    # deepseek has no official CLI yet; the slot is reserved for its banner.
    ("deepseek", ()),
)
KNOWN_AGENT_KINDS = tuple(kind for kind, _markers in AGENT_SIGNATURES)
# Process names matched by the local probe and the remote ps scan.
AGENT_PROCESS_KINDS = ("codex", "claude", "kimi")
_AGENT_SOURCE_PRIORITY = {"": 0, "output": 1, "process": 2}
_PROCESS_RUNTIMES = {
    "node",
    "nodejs",
    "python",
    "python3",
    "bun",
    "deno",
    "npm",
    "npx",
    "uvx",
}
ARTIFACT_OSC = re.compile(
    r"\x1b\]633;artifact;(?P<payload>[A-Za-z0-9_-]{1,8192})(?:\x07|\x1b\\)"
)
ARTIFACT_LINE_PREFIX = "__AGENTSERVER_ARTIFACT__:"
ARTIFACT_LINE = re.compile(
    rf"{re.escape(ARTIFACT_LINE_PREFIX)}"
    r"(?P<payload>[A-Za-z0-9_-]{1,8192}):AGENTSERVER_END__"
)
REMOTE_SHELL_COMMANDS = {
    "system": [],
    "powershell": ["powershell.exe", "-NoLogo", "-NoExit"],
    "cmd": ["cmd.exe", "/Q"],
}
MANAGED_ORIGIN = "agentserver"
LEGACY_ORIGIN = "legacy"
MANAGED_PROTOCOL_VERSION = "1"
EXEC_HANDSHAKE_TIMEOUT = 2.0
EXEC_ERROR_BYTES = 4096
MANAGED_ENV_ALLOWLIST = frozenset(
    {
        "AGENTSERVER_BASE_URL",
        "AGENTSERVER_CONTROL_SOCKET",
        "AGENTSERVER_CONTROL_TRANSPORT",
        "AGENTSERVER_CONTROL_SERVER_PID",
        "AGENTSERVER_CONTROL_SERVER_START_TIME",
    }
)
_MANAGED_BASE_ENV_KEYS = frozenset(
    {
        "AGENTSERVER_MANAGED",
        "AGENTSERVER_PROTOCOL_VERSION",
        "AGENTSERVER_ORIGIN",
        "AGENTSERVER_OWNER_ID",
        "AGENTSERVER_DEVICE_ID",
        "AGENTSERVER_TERMINAL_ID",
        "AGENTSERVER_LAUNCH_ID",
    }
)
_MANAGED_DYNAMIC_ENV_KEYS = frozenset(
    {
        "AGENTSERVER_REPORT_TOKEN",
        "AGENTSERVER_TASK_ID",
        "AGENTSERVER_TASK_PAYLOAD",
        "AGENTSERVER_ASSIGNMENT_ID",
        "AGENTSERVER_RUN_ID",
        "AGENTSERVER_AGENT_INSTANCE_ID",
    }
)
_MANAGED_SCRUB_ENV_KEYS = (
    _MANAGED_BASE_ENV_KEYS | MANAGED_ENV_ALLOWLIST | _MANAGED_DYNAMIC_ENV_KEYS
)
_MANAGED_DYNAMIC_ENV_PREFIXES = (
    "AGENTSERVER_TASK_",
    "AGENTSERVER_ASSIGNMENT_",
    "AGENTSERVER_RUN_",
    "AGENTSERVER_REPORT_TOKEN_",
)
_MANAGED_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def remote_shell_command(remote_shell: str) -> list[str]:
    """Return the SSH remote command for a validated device shell choice."""
    return list(REMOTE_SHELL_COMMANDS.get(remote_shell, ()))


def _managed_identifier(value: str | None, label: str) -> str:
    identifier = uuid.uuid4().hex if value is None else value
    if not isinstance(identifier, str) or not _MANAGED_ID.fullmatch(identifier):
        raise ValueError(
            f"{label} must be 1-128 ASCII letters, digits, underscores, or hyphens"
        )
    return identifier


def _validated_environment_value(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"managed environment value must be text: {key}")
    if len(value) > 4096 or any(character in value for character in "\0\r\n"):
        raise ValueError(f"managed environment value is invalid: {key}")
    return value


def _managed_environment(
    *,
    session_id: str,
    launch_id: str,
    owner: str,
    device_id: str | None,
    managed_env: Mapping[str, str] | None,
) -> dict[str, str]:
    """Build the static, non-secret context inherited by a managed terminal."""
    context = {
        "AGENTSERVER_MANAGED": "1",
        "AGENTSERVER_PROTOCOL_VERSION": MANAGED_PROTOCOL_VERSION,
        "AGENTSERVER_ORIGIN": MANAGED_ORIGIN,
        "AGENTSERVER_TERMINAL_ID": session_id,
        "AGENTSERVER_LAUNCH_ID": launch_id,
    }
    if owner:
        context["AGENTSERVER_OWNER_ID"] = _validated_environment_value(
            "AGENTSERVER_OWNER_ID", owner
        )
    if device_id:
        context["AGENTSERVER_DEVICE_ID"] = _validated_environment_value(
            "AGENTSERVER_DEVICE_ID", device_id
        )
    for key, value in (managed_env or {}).items():
        if key not in MANAGED_ENV_ALLOWLIST:
            raise ValueError(f"managed_env key is not allowed: {key}")
        context[key] = _validated_environment_value(key, value)
    return context


def _child_environment(managed_environment: Mapping[str, str]) -> dict[str, str]:
    """Copy the server environment without leaking per-run authority/context."""
    environment = os.environ.copy()
    for key in _managed_scrub_keys(environment):
        environment.pop(key, None)
    environment.update(managed_environment)
    environment.setdefault("TERM", "xterm-256color")
    environment.setdefault("COLORTERM", "truecolor")
    return environment


def _managed_scrub_keys(environment: Mapping[str, str]) -> set[str]:
    keys = set(_MANAGED_SCRUB_ENV_KEYS)
    keys.update(
        key
        for key in environment
        if key.startswith(_MANAGED_DYNAMIC_ENV_PREFIXES)
    )
    return keys


def _command_basename(command: str) -> str:
    return re.split(r"[\\/]", command)[-1].lower()


def _powershell_environment_script(
    managed_environment: Mapping[str, str],
) -> str:
    assignments = []
    for key, value in sorted(managed_environment.items()):
        quoted = value.replace("'", "''")
        assignments.append(f"$env:{key}='{quoted}'")
    return "; ".join(assignments)


def _encoded_powershell(script: str, *, no_exit: bool) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    command = ["powershell.exe", "-NoLogo"]
    if no_exit:
        command.append("-NoExit")
    else:
        command.extend(["-NoProfile", "-NonInteractive"])
    command.extend(["-EncodedCommand", encoded])
    return command


def _inject_ssh_managed_environment(
    argv: list[str],
    managed_environment: Mapping[str, str],
    workspace_platform: str,
) -> list[str]:
    """Inject context into the remote shell without relying on SSH SendEnv."""
    if not argv or _command_basename(argv[0]) not in {"ssh", "ssh.exe"}:
        return list(argv)

    command = list(argv)
    if workspace_platform == "windows":
        powershell_suffix = (
            len(command) >= 3
            and _command_basename(command[-3]) in {"powershell", "powershell.exe"}
            and [item.lower() for item in command[-2:]] == ["-nologo", "-noexit"]
        )
        cmd_suffix = (
            len(command) >= 2
            and _command_basename(command[-2]) in {"cmd", "cmd.exe"}
            and command[-1].lower() == "/q"
        )
        assignments = _powershell_environment_script(managed_environment)
        if powershell_suffix:
            return [
                *command[:-3],
                *_encoded_powershell(assignments, no_exit=True),
            ]
        if cmd_suffix:
            # The Windows listener probe already requires PowerShell for both
            # configured Windows shells. Use an encoded bootstrap here too so
            # cmd metacharacters, percent expansion, and quoting cannot mutate
            # a control-socket path or URL before cmd.exe inherits it.
            script = f"{assignments}; & cmd.exe /Q; exit $LASTEXITCODE"
            return [
                *command[:-2],
                *_encoded_powershell(script, no_exit=False),
            ]
        raise ValueError("Windows SSH command has no recognized PowerShell or cmd shell")

    assignments = " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(managed_environment.items())
    )
    remote_command = f'exec env {assignments} "${{SHELL:-/bin/sh}}" -l'
    command.append(remote_command)
    return command


def _tmux_managed_command(
    command: str, managed_environment: Mapping[str, str]
) -> str:
    """Make the pane's first shell scrub dynamic data and export static context."""
    unset = "unset " + " ".join(sorted(_managed_scrub_keys(os.environ)))
    exports = "; ".join(
        f"export {key}={shlex.quote(value)}"
        for key, value in sorted(managed_environment.items())
    )
    return f"{unset}; {exports}; exec {command}"


def service_product(line: str) -> str | None:
    """Infer a known development-server product from one sanitized line."""
    lowered = line.lower()
    products = (
        ("storybook", "Storybook"),
        ("next.js", "Next.js"),
        ("nextjs", "Next.js"),
        ("vite", "Vite"),
        ("astro", "Astro"),
        ("webpack", "webpack"),
        ("angular", "Angular"),
        ("nuxt", "Nuxt"),
        ("django", "Django"),
        ("flask", "Flask"),
        ("uvicorn", "Uvicorn"),
    )
    for marker, label in products:
        if marker in lowered:
            return label
    return None


def agent_signature(line: str) -> str | None:
    """Infer a known coding-agent CLI from one sanitized output line."""
    lowered = line.lower()
    for kind, markers in AGENT_SIGNATURES:
        if any(marker in lowered for marker in markers):
            return kind
    return None


def agent_kind_from_process(comm: str, args: str = "") -> str | None:
    """Map a scanned process identity (comm + argv) to a known agent kind."""
    fields = args.split()
    candidates = [comm.strip().lower()]
    if fields:
        candidates.append(os.path.basename(fields[0]).lower())
    if len(fields) > 1 and candidates[-1].split(".", 1)[0] in _PROCESS_RUNTIMES:
        # Runtime-wrapped CLIs (node …/claude, npx codex) hide behind the
        # interpreter's comm, so unwrap one level of script argument.
        candidates.append(os.path.basename(fields[1]).lower())
    for candidate in candidates:
        stem = candidate.split(".", 1)[0]
        for kind in AGENT_PROCESS_KINDS:
            if stem == kind or stem.startswith(f"{kind}-"):
                return kind
    return None


def service_label(line: str, port: int) -> str:
    """Infer a concise product label without trusting terminal escape output."""
    product = service_product(line)
    if product:
        return product
    lowered = line.lower()
    if "前端" in line or "frontend" in lowered:
        return "前端服务"
    if "后端" in line or "backend" in lowered or re.search(r"(?:^|\W)api(?:\W|$)", lowered):
        return "后端服务"
    return f"Web 服务 :{port}"


@dataclass(frozen=True)
class ListeningProcess:
    port: int
    pid: int | None = None
    command: str = ""


def process_service_label(command: str, port: int) -> str:
    """Infer a useful service label from a listener's process detail."""
    product = service_product(command)
    if product:
        return product
    lowered = command.lower()
    products = (
        ("node", "Node.js"),
        ("python", "Python"),
        ("gunicorn", "Gunicorn"),
        ("php", "PHP"),
        ("dotnet", ".NET"),
        ("java", "Java"),
        ("ruby", "Ruby"),
        ("rails", "Rails"),
        ("go", "Go"),
    )
    for marker, label in products:
        if marker in lowered:
            return label
    return f"Web 服务 :{port}"


def _listener_port(address: str) -> int | None:
    address = address.strip()
    match = re.search(r"(?:\]|:|\.)(\d{1,5})$", address)
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def _is_local_listener(address: str) -> bool:
    normalized = address.strip().lower()
    host = re.sub(r"(?:\]|:|\.)(\d{1,5})$", "", normalized).strip("[]")
    return host in {"", "*", "0.0.0.0", "::", "::1"} or host.startswith("127.")


def parse_listener_scan(output: str) -> list[ListeningProcess]:
    """Parse normalized Windows records or POSIX ss/lsof/netstat output."""
    lines = [line.strip() for line in output.replace("\r", "").split("\n") if line.strip()]
    records: dict[int, ListeningProcess] = {}
    mode = ""
    lsof_pid: int | None = None
    lsof_command = ""
    for line in lines:
        if line.startswith(f"{LISTENER_SCAN_MARKER}:"):
            mode = line.split(":", 1)[1].strip().lower()
            continue
        if line.startswith(f"{LISTENER_RECORD_MARKER}|"):
            parts = line.split("|", 3)
            if len(parts) < 4 or not parts[1].isdigit():
                continue
            port = int(parts[1])
            if not 1 <= port <= 65535:
                continue
            pid = int(parts[2]) if parts[2].isdigit() and int(parts[2]) > 0 else None
            records[port] = ListeningProcess(port, pid, parts[3].strip())
            continue
        if mode == "lsof":
            if line.startswith("p") and line[1:].isdigit():
                lsof_pid = int(line[1:])
            elif line.startswith("c"):
                lsof_command = line[1:].strip()
            elif line.startswith("n"):
                address = line[1:].split("->", 1)[0]
                port = _listener_port(address)
                if port and _is_local_listener(address):
                    records[port] = ListeningProcess(port, lsof_pid, lsof_command)
            continue
        fields = line.split()
        if mode == "ss" and len(fields) >= 4 and fields[0].upper() == "LISTEN":
            address = fields[3]
            port = _listener_port(address)
            if not port or not _is_local_listener(address):
                continue
            detail = " ".join(fields[5:])
            pid_match = re.search(r"pid=(\d+)", detail)
            command_match = re.search(r'\(\("([^"\\]+)', detail)
            records[port] = ListeningProcess(
                port,
                int(pid_match.group(1)) if pid_match else None,
                command_match.group(1) if command_match else detail,
            )
            continue
        if mode == "netstat" and len(fields) >= 4 and fields[0].lower().startswith("tcp"):
            state_index = next(
                (index for index, field in enumerate(fields) if field.upper() in {"LISTEN", "LISTENING"}),
                -1,
            )
            if state_index < 0:
                continue
            address = fields[3]
            port = _listener_port(address)
            if not port or not _is_local_listener(address):
                continue
            detail = fields[state_index + 1] if state_index + 1 < len(fields) else ""
            pid_text, _, command = detail.partition("/")
            records[port] = ListeningProcess(
                port,
                int(pid_text) if pid_text.isdigit() and int(pid_text) > 0 else None,
                command,
            )
    return sorted(records.values(), key=lambda item: item.port)


def parse_listener_scan_snapshot(output: str) -> list[ListeningProcess] | None:
    """Return a complete listener snapshot, or ``None`` when unsupported."""
    marker = re.search(
        rf"^{re.escape(LISTENER_SCAN_MARKER)}:([^\r\n]+)",
        output,
        re.MULTILINE,
    )
    if not marker or marker.group(1).strip().lower() == "unsupported":
        return None
    return parse_listener_scan(output)


@dataclass(frozen=True)
class RemoteAgent:
    kind: str
    pid: int | None = None
    ppid: int | None = None
    command: str = ""


def parse_agent_scan(output: str) -> list[RemoteAgent]:
    """Parse __AGENTSERVER_AGENT__ marker lines from a remote ps snapshot."""
    agents: list[RemoteAgent] = []
    seen: set[int] = set()
    for raw_line in output.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line.startswith(f"{AGENT_RECORD_MARKER}|"):
            continue
        parts = line.split("|", 4)
        if len(parts) < 4 or not parts[1].isdigit() or int(parts[1]) <= 0:
            continue
        pid = int(parts[1])
        if pid in seen:
            continue
        command = parts[4].strip() if len(parts) > 4 else ""
        kind = agent_kind_from_process(parts[3], command)
        if kind is None:
            # The remote grep matches the whole ps line, so lines that only
            # mention an agent in an unrelated argument are dropped here.
            continue
        seen.add(pid)
        agents.append(
            RemoteAgent(
                kind=kind,
                pid=pid,
                ppid=int(parts[2]) if parts[2].isdigit() else None,
                command=command[:255],
            )
        )
    return agents


def parse_agent_scan_snapshot(output: str) -> list[RemoteAgent] | None:
    """Return one complete remote Agent snapshot.

    ``[]`` means the remote process table was inspected and no known Agent was
    present. ``None`` means the probe was unsupported or its marker was absent;
    callers must preserve the previous state in that case instead of treating
    an observation failure as an Agent exit.
    """
    marker = re.search(
        rf"^{re.escape(AGENT_SCAN_MARKER)}:([^\r\n]+)",
        output,
        re.MULTILINE,
    )
    if not marker or marker.group(1).strip().lower() != "records":
        return None
    return parse_agent_scan(output)


@dataclass(eq=False)
class DetectedService:
    port: int
    url: str
    label: str
    status: str = "checking"
    detected_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    last_checked_at: float | None = None
    error: str = ""
    failure_count: int = 0
    retry_after: float = 0
    source: str = "output"
    process_pid: int | None = None
    process_detail: str = ""
    process_seen_at: float | None = None
    process_missing_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "port": self.port,
            "url": self.url,
            "label": self.label,
            "status": self.status,
            "detected_at": self.detected_at,
            "last_seen_at": self.last_seen_at,
            "last_checked_at": self.last_checked_at,
            "error": self.error,
            "source": self.source,
            "process_pid": self.process_pid,
            "process_detail": self.process_detail,
        }


@dataclass(eq=False)
class TerminalSession:
    id: str
    name: str
    pid: int
    fd: int
    command: str
    cwd: str
    owner: str = ""
    workspace_kind: str = "local"
    workspace_root: str = ""
    workspace_platform: str = "posix"
    workspace_current_path: str | None = None
    kind: str = "local"
    device_id: str | None = None
    device_name: str | None = None
    remote_port: int | None = None
    tmux_name: str | None = None
    launch_id: str = ""
    # Server-private process root used to authenticate the local execution
    # control socket.  It is deliberately omitted from as_dict()/persistence.
    control_pid: int | None = None
    managed: bool = True
    origin: str = MANAGED_ORIGIN
    created_at: float = field(default_factory=time.time)
    exited_at: float | None = None
    return_code: int | None = None
    chunks: deque[bytes] = field(default_factory=deque)
    buffer_size: int = 0
    subscribers: set[asyncio.Queue[bytes | StreamGap]] = field(default_factory=set)
    pending_input: bytearray = field(default_factory=bytearray)
    services: dict[int, DetectedService] = field(default_factory=dict)
    discovery_tail: str = ""
    discovery_label_hint: str = ""
    discovery_label_lines_left: int = 0
    artifact_tail: str = ""
    artifact_fd: int = -1
    artifact_pipe_path: str = ""
    last_activity_at: float = field(default_factory=time.time)
    agent_kind: str | None = None
    agent_cwd: str = ""
    agent_source: str = ""
    agent_since: float = 0
    agent_pid: int | None = None
    agent_hint: str | None = None
    agent_scan_tail: str = ""
    terminal_ready_reported: bool = field(default=False, init=False, repr=False)
    terminal_exit_reported: bool = field(default=False, init=False, repr=False)

    @property
    def active(self) -> bool:
        return self.exited_at is None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "cwd": self.cwd,
            "workspace": {
                "kind": self.workspace_kind,
                "root": self.workspace_root,
                "platform": self.workspace_platform,
                "current_path": self.workspace_current_path,
            },
            "kind": self.kind,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "remote_port": self.remote_port,
            "launch_id": self.launch_id,
            "managed": self.managed,
            "origin": self.origin,
            "created_at": self.created_at,
            "active": self.active,
            "return_code": self.return_code,
            "services": [
                service.as_dict()
                for service in sorted(self.services.values(), key=lambda item: item.port)
                if service.status == "online"
            ],
            "agent": (
                {
                    "kind": self.agent_kind,
                    "cwd": self.agent_cwd,
                    "source": self.agent_source,
                    "since": self.agent_since,
                }
                if self.agent_kind
                else None
            ),
        }


class TerminalStore:
    """Persist the stable identity of tmux-backed terminal sessions."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS terminal_sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    device_id TEXT,
                    device_name TEXT,
                    remote_port INTEGER,
                    tmux_name TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(terminal_sessions)")
            }
            migrations = {
                "owner": "TEXT NOT NULL DEFAULT ''",
                "workspace_kind": "TEXT NOT NULL DEFAULT 'local'",
                "workspace_root": "TEXT NOT NULL DEFAULT ''",
                "workspace_platform": "TEXT NOT NULL DEFAULT 'posix'",
                "agent_hint": "TEXT",
                "launch_id": "TEXT NOT NULL DEFAULT ''",
                # Rows that predate managed launch identity never inherited the
                # static AgentServer environment.  They must remain explicitly
                # legacy; newly-created rows are inserted with concrete values
                # by save(), so changing these ALTER defaults cannot downgrade
                # an existing managed row.
                "managed": "INTEGER NOT NULL DEFAULT 0",
                "origin": "TEXT NOT NULL DEFAULT 'legacy'",
            }
            for column, declaration in migrations.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE terminal_sessions ADD COLUMN {column} {declaration}"
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def list(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM terminal_sessions ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def save(self, session: TerminalSession) -> None:
        if not session.tmux_name:
            raise ValueError("A persistent terminal must have a tmux session name")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO terminal_sessions(
                    id, name, command, cwd, kind, device_id, device_name,
                    remote_port, tmux_name, created_at, owner, workspace_kind,
                    workspace_root, workspace_platform, agent_hint, launch_id,
                    managed, origin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.name,
                    session.command,
                    session.cwd,
                    session.kind,
                    session.device_id,
                    session.device_name,
                    session.remote_port,
                    session.tmux_name,
                    session.created_at,
                    session.owner,
                    session.workspace_kind,
                    session.workspace_root,
                    session.workspace_platform,
                    session.agent_hint,
                    session.launch_id,
                    int(session.managed),
                    session.origin,
                ),
            )

    def delete(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM terminal_sessions WHERE id = ?", (session_id,))


class TerminalManager:
    def __init__(
        self,
        command: str,
        cwd: str,
        shell: str | None = None,
        proxy: str | None = None,
        scrollback_bytes: int = 2 * 1024 * 1024,
        backend: str = "direct",
        database_path: Path | None = None,
        tmux_binary: str = "tmux",
        tmux_socket: Path | None = None,
        default_owner: str = "",
        artifact_callback: Callable[[TerminalSession, dict[str, object]], None] | None = None,
        agent_observation_callback: Callable[
            [TerminalSession | None, dict[str, object]], None
        ]
        | None = None,
        terminal_lifecycle_callback: Callable[
            [TerminalSession, dict[str, object]], None
        ]
        | None = None,
        control_binding_callback: Callable[[TerminalSession], None] | None = None,
    ) -> None:
        self.command = command
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.shell = shell or os.getenv("SHELL") or "/bin/sh"
        self.proxy = proxy
        self.scrollback_bytes = max(scrollback_bytes, 64 * 1024)
        if backend not in {"direct", "tmux"}:
            raise ValueError("TERMINAL_BACKEND must be 'direct' or 'tmux'")
        self.backend = backend
        self.tmux_binary = tmux_binary
        self.tmux_socket = tmux_socket
        self.default_owner = default_owner
        self.artifact_callback = artifact_callback
        self.agent_observation_callback = agent_observation_callback
        self.terminal_lifecycle_callback = terminal_lifecycle_callback
        self.control_binding_callback = control_binding_callback
        self.store: TerminalStore | None = None
        self._artifact_pipe_directory: Path | None = None
        self.sessions: dict[str, TerminalSession] = {}
        self._owned_pids: set[int] = set()
        self.loop = asyncio.get_running_loop()
        self.service_discovery_event = asyncio.Event()
        self._tmux_states_cache: tuple[float, dict[str, tuple[bool, str, bool]]] | None = None
        # Each state subscriber owns its own Event. A single shared Event would
        # let one client's clear() swallow a notification another had not read.
        self._state_waiters: set[asyncio.Event] = set()
        self._unattributed_agents: dict[str, dict[int, RemoteAgent]] = {}
        if self.backend == "tmux":
            if not shutil.which(self.tmux_binary):
                raise RuntimeError(f"tmux executable not found: {self.tmux_binary}")
            if database_path is None or tmux_socket is None:
                raise ValueError("tmux backend requires database_path and tmux_socket")
            self.tmux_socket = Path(tmux_socket).expanduser().resolve()
            self._artifact_pipe_directory = self.tmux_socket.parent / "artifact-pipes"
            self.store = TerminalStore(Path(database_path).expanduser().resolve())
            try:
                self._restore_tmux_sessions()
            except BaseException:
                for session in tuple(self.sessions.values()):
                    with contextlib.suppress(Exception):
                        self._stop_tmux_artifact_capture(session)
                    with contextlib.suppress(Exception):
                        self._close_client(session)
                self.sessions.clear()
                raise

    def subscribe_state(self) -> asyncio.Event:
        """Register for session-lifecycle notifications.

        Lets clients be pushed session/service changes instead of polling
        /api/terminals, which in the tmux backend costs a tmux query per call.
        """
        waiter = asyncio.Event()
        self._state_waiters.add(waiter)
        return waiter

    def unsubscribe_state(self, waiter: asyncio.Event) -> None:
        self._state_waiters.discard(waiter)

    def _set_state_waiters(self) -> None:
        for waiter in tuple(self._state_waiters):
            waiter.set()

    def _notify_state_change(self) -> None:
        """Signal a real transition only — never on an unchanged refresh.

        `list()` is what subscribers call to build their payload, and it refreshes
        tmux state on the way. Notifying unconditionally from there would make
        every push cause the next one.

        Most state is applied by the event-loop thread. Keep this method safe for
        defensive callers in workers as well: ``asyncio.Event`` is not
        thread-safe and debug mode rejects a direct cross-thread ``set()``.
        """
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self.loop:
            self._set_state_waiters()
            return
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._set_state_waiters)

    def _finalize_session_exit(self, session: TerminalSession) -> None:
        """Publish one terminal exit on the manager loop once status is known."""
        if session.return_code is None:
            return
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self.loop:
            self._finalize_session_exit_on_loop(session)
            return
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(
                self._finalize_session_exit_on_loop, session
            )

    def _report_terminal_ready(self, session: TerminalSession) -> None:
        """Publish the first non-empty PTY read on the manager event loop."""
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self.loop:
            self._report_terminal_ready_on_loop(session)
            return
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._report_terminal_ready_on_loop, session)

    def _report_terminal_ready_on_loop(self, session: TerminalSession) -> None:
        callback = self.terminal_lifecycle_callback
        if callback is None or session.terminal_ready_reported:
            return
        session.terminal_ready_reported = True
        try:
            callback(session, {"type": "terminal.ready", "source": "pty"})
        except Exception:
            # A lifecycle sink cannot be allowed to interrupt PTY draining.
            pass

    def _finalize_session_exit_on_loop(self, session: TerminalSession) -> None:
        if session.return_code is None:
            return

        # A process-backed Agent has an exact PID incarnation, so terminal death
        # is sufficient exit evidence.  Output-only signatures deliberately are
        # not promoted to a process exit merely because their terminal closed.
        agent_pid = session.agent_pid
        if (
            session.agent_kind
            and isinstance(agent_pid, int)
            and not isinstance(agent_pid, bool)
            and agent_pid > 0
        ):
            self._set_agent(
                session,
                None,
                source="process",
                return_code=session.return_code,
            )

        callback = self.terminal_lifecycle_callback
        if callback is None or session.terminal_exit_reported:
            return
        session.terminal_exit_reported = True
        try:
            callback(
                session,
                {
                    "type": "terminal.exited",
                    "return_code": session.return_code,
                    "exited_at": session.exited_at,
                },
            )
        except Exception:
            # Execution persistence is downstream of terminal cleanup.  A sink
            # failure must never leave a PTY descriptor or child process alive.
            pass

    def list(self, owner: str | None = None) -> list[dict[str, object]]:
        if self.backend == "tmux":
            # One tmux exec describes every pane on the socket, so refreshing N
            # sessions no longer costs 2N blocking subprocess calls on the loop.
            states = self._tmux_pane_states(max_age=1.0)
            for session in tuple(self.sessions.values()):
                if session.active:
                    self._refresh_tmux_state(session, states)
        sessions = sorted(
            (
                session
                for session in self.sessions.values()
                if owner is None or session.owner == owner
            ),
            key=lambda item: item.created_at,
        )
        return [session.as_dict() for session in sessions]

    def get(self, session_id: str) -> TerminalSession | None:
        session = self.sessions.get(session_id)
        if session and self.backend == "tmux":
            states = self._tmux_pane_states(max_age=1.0)
            if session.active:
                self._refresh_tmux_state(session, states)
            if session.active and session.fd < 0 and self._tmux_session_alive(
                session.tmux_name or "", states
            ):
                self._spawn_tmux_client(session)
        return session

    def get_for_owner(self, session_id: str, owner: str) -> TerminalSession | None:
        session = self.get(session_id)
        if not session or session.owner != owner:
            return None
        return session

    def _prepare_managed_launch(
        self,
        *,
        session_id: str | None,
        launch_id: str | None,
        owner: str | None,
        device_id: str | None,
        managed_env: Mapping[str, str] | None,
    ) -> tuple[str, str, str, dict[str, str]]:
        resolved_session_id = _managed_identifier(session_id, "session_id")
        if resolved_session_id in self.sessions:
            raise ValueError(f"Terminal session already exists: {resolved_session_id}")
        resolved_launch_id = _managed_identifier(launch_id, "launch_id")
        if any(
            session.launch_id == resolved_launch_id
            for session in self.sessions.values()
            if session.launch_id
        ):
            raise ValueError(f"Terminal launch already exists: {resolved_launch_id}")
        resolved_owner = owner if owner is not None else self.default_owner
        environment = _managed_environment(
            session_id=resolved_session_id,
            launch_id=resolved_launch_id,
            owner=resolved_owner,
            device_id=device_id,
            managed_env=managed_env,
        )
        return resolved_session_id, resolved_launch_id, resolved_owner, environment

    def _tmux_pane_pid(self, session: TerminalSession) -> int | None:
        if not session.tmux_name:
            return None
        result = self._tmux_run(
            "display-message",
            "-p",
            "-t",
            session.tmux_name,
            "#{pane_pid}",
            check=False,
        )
        if result.returncode != 0:
            return None
        value = str(result.stdout or "").strip().splitlines()
        if not value or not value[0].isdigit():
            return None
        pid = int(value[0])
        return pid if pid > 0 else None

    def _bind_control_launch(self, session: TerminalSession) -> None:
        callback = self.control_binding_callback
        if (
            callback is None
            or not session.managed
            or session.kind != "local"
            or not session.owner
            or not session.launch_id
        ):
            return
        if self.backend == "tmux":
            session.control_pid = self._tmux_pane_pid(session)
        elif session.pid > 0:
            session.control_pid = session.pid
        if session.control_pid is None:
            raise RuntimeError("managed terminal control process is unavailable")
        callback(session)

    @staticmethod
    def _discard_failed_fork(pid: int, descriptor: int) -> None:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)

    def _fork_exec(
        self,
        launch: Callable[[], None],
        *,
        failure_prefix: str,
    ) -> tuple[int, int]:
        """Fork one PTY child and synchronously verify that exec succeeded.

        The status pipe's write end is close-on-exec.  EOF therefore means the
        requested program replaced the Python child, while bytes contain the
        bounded pre-exec exception.  This distinguishes an immediate exec error
        from a successfully launched interactive program without waiting for
        terminal output or changing its stdin/stdout PTY contract.
        """
        status_reader, status_writer = os.pipe()
        os.set_inheritable(status_writer, False)
        try:
            pid, descriptor = pty.fork()
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(status_reader)
            with contextlib.suppress(OSError):
                os.close(status_writer)
            raise
        if pid == 0:
            with contextlib.suppress(OSError):
                os.close(status_reader)
            try:
                launch()
                raise RuntimeError("exec returned without replacing the child")
            except BaseException as error:
                detail = f"{type(error).__name__}: {error}".encode(
                    "utf-8", errors="replace"
                )[:EXEC_ERROR_BYTES]
                with contextlib.suppress(OSError):
                    os.write(status_writer, detail)
                with contextlib.suppress(OSError):
                    os.write(2, failure_prefix.encode() + b": " + detail + b"\r\n")
                os._exit(127)

        with contextlib.suppress(OSError):
            os.close(status_writer)
        failure = bytearray()
        deadline = time.monotonic() + EXEC_HANDSHAKE_TIMEOUT
        timed_out = False
        try:
            while len(failure) < EXEC_ERROR_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    readable, _writable, _exceptional = select.select(
                        [status_reader], [], [], remaining
                    )
                except InterruptedError:
                    continue
                if not readable:
                    timed_out = True
                    break
                try:
                    chunk = os.read(status_reader, EXEC_ERROR_BYTES - len(failure))
                except InterruptedError:
                    continue
                if not chunk:
                    break
                failure.extend(chunk)
        finally:
            with contextlib.suppress(OSError):
                os.close(status_reader)

        if timed_out or failure:
            self._discard_failed_fork(pid, descriptor)
            detail = (
                "exec handshake timed out"
                if timed_out
                else failure.decode("utf-8", errors="replace")
            )
            raise RuntimeError(f"{failure_prefix}: {detail}")
        return pid, descriptor

    def create(
        self,
        name: str | None = None,
        cols: int = 120,
        rows: int = 32,
        *,
        owner: str | None = None,
        workspace_root: str | None = None,
        agent_hint: str | None = None,
        session_id: str | None = None,
        launch_id: str | None = None,
        managed_env: Mapping[str, str] | None = None,
    ) -> TerminalSession:
        if not Path(self.cwd).is_dir():
            raise ValueError(f"TERMINAL_CWD does not exist: {self.cwd}")
        if not Path(self.shell).is_file() or not os.access(self.shell, os.X_OK):
            raise ValueError(f"TERMINAL_SHELL is not executable: {self.shell}")
        workspace_directory = str(
            Path(workspace_root or self.cwd).expanduser().resolve()
        )
        if not Path(workspace_directory).is_dir():
            raise ValueError(f"Workspace directory does not exist: {workspace_directory}")
        session_id, launch_id, resolved_owner, launch_environment = (
            self._prepare_managed_launch(
                session_id=session_id,
                launch_id=launch_id,
                owner=owner,
                device_id=None,
                managed_env=managed_env,
            )
        )

        if self.backend == "tmux":
            return self._create_tmux_session(
                name=name or "Terminal",
                command=shlex.join([self.shell, "-l"]),
                cols=cols,
                rows=rows,
                initial_input=self.command.strip() or None,
                owner=resolved_owner,
                workspace_root=workspace_directory,
                agent_hint=agent_hint,
                session_id=session_id,
                launch_id=launch_id,
                managed_environment=launch_environment,
            )

        display_name = (name or "Terminal").strip()[:80] or "Terminal"

        def launch_terminal() -> None:
            os.chdir(workspace_directory)
            environment = _child_environment(launch_environment)
            if self.proxy:
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                    environment[key] = self.proxy
            environment["SHELL"] = self.shell
            login_argv0 = f"-{Path(self.shell).name}"
            os.execvpe(self.shell, [login_argv0], environment)

        pid, fd = self._fork_exec(
            launch_terminal, failure_prefix="Unable to start terminal"
        )

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._owned_pids.add(pid)
        session = TerminalSession(
            id=session_id,
            name=display_name,
            pid=pid,
            fd=fd,
            command=self.command,
            cwd=workspace_directory,
            owner=resolved_owner,
            workspace_kind="local",
            workspace_root=workspace_directory,
            workspace_platform="posix",
            workspace_current_path=workspace_directory,
            launch_id=launch_id,
            control_pid=pid,
            managed=True,
            origin=MANAGED_ORIGIN,
        )
        try:
            self._bind_control_launch(session)
        except BaseException:
            self._owned_pids.discard(pid)
            self._discard_failed_fork(pid, fd)
            raise
        self.sessions[session_id] = session
        self._apply_agent_hint(session, agent_hint)
        self.resize(session_id, cols, rows)
        self.loop.add_reader(fd, self._read_ready, session_id)
        if self.command.strip():
            initial_input = f"{self.command}\r".encode("utf-8")
            self.loop.call_later(0.05, self.write, session_id, initial_input)
        self._notify_state_change()
        return session

    def create_process(
        self,
        *,
        name: str,
        argv: list[str],
        cols: int = 120,
        rows: int = 32,
        device_id: str | None = None,
        device_name: str | None = None,
        remote_port: int | None = None,
        owner: str | None = None,
        workspace_root: str = ".",
        workspace_platform: str = "posix",
        agent_hint: str | None = None,
        session_id: str | None = None,
        launch_id: str | None = None,
        managed_env: Mapping[str, str] | None = None,
    ) -> TerminalSession:
        if not argv:
            raise ValueError("Process command is empty")
        executable = shutil.which(argv[0])
        if not executable:
            raise ValueError(f"Executable not found: {argv[0]}")
        if not Path(self.cwd).is_dir():
            raise ValueError(f"TERMINAL_CWD does not exist: {self.cwd}")
        session_id, launch_id, resolved_owner, launch_environment = (
            self._prepare_managed_launch(
                session_id=session_id,
                launch_id=launch_id,
                owner=owner,
                device_id=device_id,
                managed_env=managed_env,
            )
        )
        process_argv = _inject_ssh_managed_environment(
            [executable, *argv[1:]], launch_environment, workspace_platform
        )

        if self.backend == "tmux":
            return self._create_tmux_session(
                name=name,
                command=shlex.join(process_argv),
                cols=cols,
                rows=rows,
                kind="ssh",
                device_id=device_id,
                device_name=device_name,
                remote_port=remote_port,
                owner=resolved_owner,
                workspace_root=workspace_root,
                workspace_platform=workspace_platform,
                agent_hint=agent_hint,
                session_id=session_id,
                launch_id=launch_id,
                managed_environment=launch_environment,
            )

        def launch_process() -> None:
            os.chdir(self.cwd)
            environment = _child_environment(launch_environment)
            os.execvpe(process_argv[0], process_argv, environment)

        pid, fd = self._fork_exec(
            launch_process, failure_prefix="Unable to start process"
        )

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._owned_pids.add(pid)
        session = TerminalSession(
            id=session_id,
            name=name.strip()[:80] or "SSH Terminal",
            pid=pid,
            fd=fd,
            command=shlex.join(process_argv),
            cwd=self.cwd,
            kind="ssh",
            device_id=device_id,
            device_name=device_name,
            remote_port=remote_port,
            owner=resolved_owner,
            workspace_kind="sftp",
            workspace_root=workspace_root,
            workspace_platform=workspace_platform,
            workspace_current_path=(workspace_root if workspace_root == "." else None),
            launch_id=launch_id,
            control_pid=pid,
            managed=True,
            origin=MANAGED_ORIGIN,
        )
        try:
            self._bind_control_launch(session)
        except BaseException:
            self._owned_pids.discard(pid)
            self._discard_failed_fork(pid, fd)
            raise
        self.sessions[session_id] = session
        self._apply_agent_hint(session, agent_hint)
        self.resize(session_id, cols, rows)
        self.loop.add_reader(fd, self._read_ready, session_id)
        self._notify_state_change()
        return session

    def _tmux_command(self, *arguments: str) -> list[str]:
        if self.tmux_socket is None:
            raise RuntimeError("tmux socket is not configured")
        return [self.tmux_binary, "-S", str(self.tmux_socket), *arguments]

    def _tmux_run(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if arguments and arguments[0] in {"new-session", "kill-session", "kill-server"}:
            # The batched pane snapshot is only valid while the set of sessions
            # is unchanged. Invalidating here means no caller can forget to.
            self._tmux_states_cache = None
        environment = os.environ.copy()
        for key in _managed_scrub_keys(environment):
            environment.pop(key, None)
        result = subprocess.run(
            self._tmux_command(*arguments),
            capture_output=True,
            text=True,
            env=environment,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(detail or f"tmux command failed: {arguments[0]}")
        return result

    def _tmux_session_exists(self, tmux_name: str) -> bool:
        if not tmux_name:
            return False
        result = self._tmux_run("has-session", "-t", tmux_name, check=False)
        return result.returncode == 0

    def _tmux_pane_states(
        self, *, max_age: float = 0.0
    ) -> dict[str, tuple[bool, str, bool]] | None:
        """Describe every pane on the socket with a single tmux exec.

        Maps each tmux session name to (pane_dead, pane_dead_status, pane_pipe).
        Returns None when the query itself failed; callers must then fall back to
        the per-session path, because an unreachable tmux server would otherwise
        be indistinguishable from "every session has died".
        """
        cached = self._tmux_states_cache
        if max_age > 0 and cached and time.monotonic() - cached[0] <= max_age:
            return cached[1]
        result = self._tmux_run(
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_pipe}",
            check=False,
        )
        if result.returncode != 0:
            return None
        states: dict[str, tuple[bool, str, bool]] = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 4 or not fields[0]:
                continue
            # Every agentserver session owns exactly one pane; if that ever
            # changes, the first pane still decides the session's liveness.
            states.setdefault(fields[0], (fields[1] == "1", fields[2], fields[3] == "1"))
        self._tmux_states_cache = (time.monotonic(), states)
        return states

    def _tmux_session_alive(
        self, tmux_name: str, states: dict[str, tuple[bool, str, bool]] | None
    ) -> bool:
        if states is None:
            return self._tmux_session_exists(tmux_name)
        return bool(tmux_name) and tmux_name in states

    def _configure_tmux_server(self) -> None:
        if self.tmux_socket is None:
            return
        self.tmux_socket.parent.mkdir(parents=True, exist_ok=True)
        self.tmux_socket.parent.chmod(0o700)
        if self.tmux_socket.exists():
            mode = self.tmux_socket.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"TMUX_SOCKET is not a Unix socket: {self.tmux_socket}")
        result = self._tmux_run(
            "set-option", "-s", "exit-empty", "off", check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip()
                or "Persistent tmux server is unavailable; start agentserver-tmux.service"
            )
        # The production service account intentionally uses /usr/sbin/nologin.
        # tmux otherwise uses that passwd shell to wrap every pane command,
        # causing newly created sessions to exit immediately.
        self._tmux_run("set-option", "-g", "default-shell", self.shell)
        # Keep tmux's mouse capture off: the xterm.js frontend handles
        # drag-to-select and wheel scrolling natively, and tmux mouse mode
        # would steal drags into copy-mode and clear them on mouseup.
        self._tmux_run("set-option", "-g", "mouse", "off")
        # This is a dedicated tmux server whose clients are xterm.js panes.
        # Keep those outer clients on the normal screen so xterm.js can retain
        # scrollback; programs inside tmux may still use tmux's alternate screen.
        self._tmux_run(
            "set-option",
            "-s",
            "-g",
            "terminal-overrides",
            "xterm-256color:smcup@:rmcup@",
        )
        if self.proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                self._tmux_run("set-environment", "-g", key, self.proxy)

    def _capture_tmux_history(self, tmux_name: str) -> bytes:
        result = self._tmux_run(
            "capture-pane", "-ep", "-S", "-10000", "-t", tmux_name, check=False
        )
        if result.returncode != 0 or not result.stdout:
            return b""
        return result.stdout.replace("\r\n", "\n").replace("\n", "\r\n").encode()

    def _artifact_pipe_path(self, session: TerminalSession) -> Path:
        directory = self._artifact_pipe_directory
        if directory is None:
            raise RuntimeError("tmux artifact pipe directory is not configured")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        # Persisted IDs are data, not path components. A deterministic UUID keeps
        # recovery able to replace a stale FIFO without trusting database text.
        pipe_name = uuid.uuid5(uuid.NAMESPACE_OID, session.id).hex
        return directory / f"{pipe_name}.fifo"

    @staticmethod
    def _unlink_artifact_pipe(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISFIFO(info.st_mode):
            path.unlink()

    def _start_tmux_artifact_capture(self, session: TerminalSession) -> None:
        """Route raw pane bytes to a private FIFO, separate from screen redraws."""
        if (
            self.backend != "tmux"
            or self.artifact_callback is None
            or not session.tmux_name
            or session.artifact_fd >= 0
        ):
            return

        path = self._artifact_pipe_path(session)
        self._unlink_artifact_pipe(path)
        try:
            os.mkfifo(path, 0o600)
            path.chmod(0o600)
            flags = os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(path, flags)
        except BaseException:
            self._unlink_artifact_pipe(path)
            raise

        try:
            # pipe-pane receives pane output before tmux turns it into redraws.
            # The FIFO is read internally only; bytes are never appended to the
            # terminal scrollback or broadcast to browser subscribers a second time.
            pipe_command = f"exec cat > {shlex.quote(str(path))}"
            self._tmux_run("pipe-pane", "-t", session.tmux_name, pipe_command)
            session.artifact_fd = descriptor
            session.artifact_pipe_path = str(path)
            self.loop.add_reader(
                descriptor,
                self._read_tmux_artifacts,
                session.id,
                descriptor,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                self._tmux_run("pipe-pane", "-t", session.tmux_name, check=False)
            with contextlib.suppress(OSError):
                os.close(descriptor)
            self._unlink_artifact_pipe(path)
            raise

    def _stop_tmux_artifact_capture(
        self, session: TerminalSession, *, stop_pipe: bool = True
    ) -> None:
        descriptor = session.artifact_fd
        raw_path = session.artifact_pipe_path
        if descriptor < 0 and not raw_path:
            return
        if stop_pipe and session.tmux_name:
            with contextlib.suppress(Exception):
                self._tmux_run("pipe-pane", "-t", session.tmux_name, check=False)
        if descriptor >= 0:
            self.loop.remove_reader(descriptor)
            # pipe-pane has been stopped, so drain bytes already accepted by the
            # kernel before closing the FIFO. This lets application shutdown hand
            # the final events to the higher-level ingest queue before it joins.
            for _ in range(256):
                try:
                    chunk = os.read(descriptor, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                self._discover_artifacts(session, chunk)
            with contextlib.suppress(OSError):
                os.close(descriptor)
        session.artifact_fd = -1
        session.artifact_pipe_path = ""
        if raw_path:
            self._unlink_artifact_pipe(Path(raw_path))

    def _read_tmux_artifacts(self, session_id: str, descriptor: int) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.artifact_fd != descriptor:
            self.loop.remove_reader(descriptor)
            with contextlib.suppress(OSError):
                os.close(descriptor)
            return
        try:
            chunk = os.read(descriptor, 65_536)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            self._stop_tmux_artifact_capture(session)
            return
        if chunk:
            self._discover_artifacts(session, chunk)
            self._discover_agent(session, chunk)

    def _restore_tmux_sessions(self) -> None:
        if self.store is None:
            return
        self._configure_tmux_server()
        for record in self.store.list():
            legacy_workspace = not bool(record["workspace_root"])
            workspace_root = str(
                record["workspace_root"]
                or ("." if str(record["kind"]) == "ssh" else record["cwd"])
            )
            workspace_kind = (
                "sftp"
                if legacy_workspace and str(record["kind"]) == "ssh"
                else str(record["workspace_kind"] or "local")
            )
            session = TerminalSession(
                id=str(record["id"]),
                name=str(record["name"]),
                pid=-1,
                fd=-1,
                command=str(record["command"]),
                cwd=str(record["cwd"]),
                owner=str(record["owner"] or self.default_owner),
                workspace_kind=workspace_kind,
                workspace_root=workspace_root,
                workspace_platform=str(record["workspace_platform"] or "posix"),
                workspace_current_path=(
                    workspace_root
                    if workspace_kind == "local" or workspace_root == "."
                    else None
                ),
                kind=str(record["kind"]),
                device_id=record["device_id"] and str(record["device_id"]),
                device_name=record["device_name"] and str(record["device_name"]),
                remote_port=(
                    int(record["remote_port"])
                    if record["remote_port"] is not None
                    else None
                ),
                tmux_name=str(record["tmux_name"]),
                launch_id=str(record["launch_id"] or ""),
                managed=bool(record["managed"]),
                origin=str(record["origin"] or MANAGED_ORIGIN),
                created_at=float(record["created_at"]),
                agent_hint=(
                    str(record["agent_hint"]) if record["agent_hint"] else None
                ),
            )
            if self._tmux_session_exists(session.tmux_name):
                self._bind_control_launch(session)
                history = self._capture_tmux_history(session.tmux_name)
                if history:
                    self._append(session, history)
                self.sessions[session.id] = session
                self._refresh_tmux_state(session)
                if session.active:
                    self._start_tmux_artifact_capture(session)
                    self._spawn_tmux_client(session)
            else:
                session.exited_at = time.time()
                session.return_code = -1
                self._append(
                    session,
                    b"\r\n\x1b[90m[tmux session unavailable after host restart]\x1b[0m\r\n",
                )
                self.sessions[session.id] = session
                self._finalize_session_exit(session)

    def _create_tmux_session(
        self,
        *,
        name: str,
        command: str,
        cols: int,
        rows: int,
        initial_input: str | None = None,
        kind: str = "local",
        device_id: str | None = None,
        device_name: str | None = None,
        remote_port: int | None = None,
        owner: str | None = None,
        workspace_root: str | None = None,
        workspace_platform: str = "posix",
        agent_hint: str | None = None,
        session_id: str,
        launch_id: str,
        managed_environment: Mapping[str, str],
    ) -> TerminalSession:
        if self.store is None:
            raise RuntimeError("Persistent terminal store is not configured")
        working_directory = self.cwd
        if kind == "local" and workspace_root:
            working_directory = str(Path(workspace_root).expanduser().resolve())
            if not Path(working_directory).is_dir():
                raise ValueError(
                    f"Workspace directory does not exist: {working_directory}"
                )
        tmux_name = f"agentserver-{session_id}"
        cols = min(max(cols, 2), 500)
        rows = min(max(rows, 1), 300)
        launch_command = _tmux_managed_command(command, managed_environment)
        self._tmux_run(
            "new-session",
            "-d",
            "-s",
            tmux_name,
            "-x",
            str(cols),
            "-y",
            str(rows),
            "-c",
            working_directory,
            launch_command,
        )
        try:
            self._tmux_run("set-option", "-t", tmux_name, "status", "off")
            self._tmux_run(
                "set-option", "-w", "-t", tmux_name, "remain-on-exit", "on"
            )
            self._tmux_run(
                "set-option", "-w", "-t", tmux_name, "history-limit", "10000"
            )
            session = TerminalSession(
                id=session_id,
                name=name.strip()[:80] or ("SSH Terminal" if kind == "ssh" else "Terminal"),
                pid=-1,
                fd=-1,
                command=self.command if kind == "local" else command,
                cwd=working_directory,
                owner=owner if owner is not None else self.default_owner,
                workspace_kind="sftp" if kind == "ssh" else "local",
                workspace_root=workspace_root or ("." if kind == "ssh" else self.cwd),
                workspace_platform=workspace_platform,
                workspace_current_path=(
                    working_directory
                    if kind == "local"
                    else "."
                    if (workspace_root or ".") == "."
                    else None
                ),
                kind=kind,
                device_id=device_id,
                device_name=device_name,
                remote_port=remote_port,
                tmux_name=tmux_name,
                launch_id=launch_id,
                managed=True,
                origin=MANAGED_ORIGIN,
            )
            self._apply_agent_hint(session, agent_hint)
            self._bind_control_launch(session)
            self.store.save(session)
        except BaseException:
            self._tmux_run("kill-session", "-t", tmux_name, check=False)
            raise
        self.sessions[session.id] = session
        try:
            self._start_tmux_artifact_capture(session)
            self._spawn_tmux_client(session)
            if initial_input:
                self._tmux_run("send-keys", "-t", tmux_name, "-l", initial_input)
                self._tmux_run("send-keys", "-t", tmux_name, "Enter")
        except BaseException:
            self._stop_tmux_artifact_capture(session)
            self._close_client(session)
            self.sessions.pop(session.id, None)
            self.store.delete(session.id)
            self._tmux_run("kill-session", "-t", tmux_name, check=False)
            raise
        self._notify_state_change()
        return session

    def _spawn_tmux_client(self, session: TerminalSession) -> None:
        if session.fd >= 0 or not session.tmux_name:
            return
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(session.cwd if Path(session.cwd).is_dir() else self.cwd)
                # This is only the outer tmux client. The pane already owns its
                # persisted launch context, so merely scrub any server-side
                # task/token variables before attaching.
                environment = _child_environment({})
                environment.pop("TMUX", None)
                argv = self._tmux_command("attach-session", "-t", session.tmux_name)
                os.execvpe(argv[0], argv, environment)
            except BaseException as exc:
                os.write(2, f"Unable to attach tmux session: {exc}\r\n".encode())
                os._exit(127)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._owned_pids.add(pid)
        session.pid = pid
        session.fd = fd
        self.loop.add_reader(fd, self._read_ready, session.id)

    def _refresh_tmux_state(
        self,
        session: TerminalSession,
        states: dict[str, tuple[bool, str, bool]] | None = None,
    ) -> None:
        tmux_name = session.tmux_name or ""
        was_active = session.active
        if not self._tmux_session_alive(tmux_name, states):
            session.exited_at = session.exited_at or time.time()
            session.return_code = session.return_code if session.return_code is not None else -1
            self._stop_tmux_artifact_capture(session, stop_pipe=False)
            if was_active:
                self._notify_state_change()
            self._finalize_session_exit(session)
            return
        if states is not None:
            queried = True
            dead, dead_status, piped = states[tmux_name]
        else:
            result = self._tmux_run(
                "display-message",
                "-p",
                "-t",
                tmux_name,
                "#{pane_dead}:#{pane_dead_status}:#{pane_pipe}",
                check=False,
            )
            fields = result.stdout.strip().split(":", 2)
            queried = result.returncode == 0
            dead = fields[0] == "1"
            dead_status = fields[1] if len(fields) > 1 else ""
            piped = len(fields) > 2 and fields[2] == "1"
        if queried and dead:
            session.exited_at = session.exited_at or time.time()
            try:
                session.return_code = int(dead_status)
            except (TypeError, ValueError):
                session.return_code = None
            self._stop_tmux_artifact_capture(session)
            if was_active:
                self._notify_state_change()
            self._finalize_session_exit(session)
        elif queried:
            session.exited_at = None
            session.return_code = None
            if not was_active:
                self._notify_state_change()
            if self.artifact_callback is not None and (
                session.artifact_fd < 0 or not piped
            ):
                self._stop_tmux_artifact_capture(session, stop_pipe=False)
            self._start_tmux_artifact_capture(session)

    def _close_client(self, session: TerminalSession) -> None:
        if session.fd >= 0:
            self.loop.remove_reader(session.fd)
            self.loop.remove_writer(session.fd)
            try:
                os.close(session.fd)
            except OSError:
                pass
        if session.pid > 0:
            self._signal_session(session.pid, signal.SIGTERM)
            try:
                waited_pid, _status = os.waitpid(session.pid, os.WNOHANG)
                if waited_pid:
                    self._owned_pids.discard(session.pid)
            except ChildProcessError:
                self._owned_pids.discard(session.pid)
            self._owned_pids.discard(session.pid)
        session.fd = -1
        session.pid = -1
        session.pending_input.clear()

    def _read_ready(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session or not session.active:
            return
        try:
            chunk = os.read(session.fd, 65_536)
            if chunk:
                if self.backend != "tmux":
                    self._discover_artifacts(session, chunk)
                self._append(session, chunk)
                self._report_terminal_ready(session)
                self._broadcast(session, chunk)
                return
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            if exc.errno != errno.EIO:
                self._append(session, f"\r\n[terminal read error: {exc}]\r\n".encode())
        self._mark_exited(session)

    def _append(self, session: TerminalSession, chunk: bytes) -> None:
        session.chunks.append(chunk)
        session.buffer_size += len(chunk)
        session.last_activity_at = time.time()
        while session.buffer_size > self.scrollback_bytes and session.chunks:
            removed = session.chunks.popleft()
            session.buffer_size -= len(removed)
        self._discover_services(session, chunk)
        if self.backend != "tmux":
            # For tmux, _append sees redrawn screen bytes where banners are
            # unreliable; raw pane output flows through the pipe-pane FIFO
            # (_read_tmux_artifacts) instead, matching _discover_artifacts.
            self._discover_agent(session, chunk)

    def _discover_artifacts(self, session: TerminalSession, chunk: bytes) -> None:
        """Decode bounded, terminal-originated artifact announcements.

        Agents without HTTP credentials can emit OSC 633 with a URL-safe
        base64 JSON payload. Announcements are only metadata; later file reads
        still pass through the owner-bound workspace gateway.
        """
        if self.artifact_callback is None:
            return
        text = session.artifact_tail + chunk.decode("utf-8", errors="ignore")
        matches = sorted(
            [
                *((match, "terminal-osc") for match in ARTIFACT_OSC.finditer(text)),
                *((match, "terminal-marker") for match in ARTIFACT_LINE.finditer(text)),
            ],
            key=lambda item: item[0].start(),
        )
        last_complete_end = 0
        for match, source in matches:
            last_complete_end = match.end()
            encoded = match.group("payload")
            try:
                padding = "=" * (-len(encoded) % 4)
                payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
                if not isinstance(payload, dict):
                    continue
                event = {
                    str(key): value
                    for key, value in payload.items()
                    if key
                    in {
                        "type",
                        "path",
                        "name",
                        "media_type",
                        "size",
                        "kind",
                        "version",
                        "message",
                    }
                }
                if not isinstance(event.get("path"), str):
                    continue
                event["source"] = source
                try:
                    self.artifact_callback(session, event)
                except Exception:
                    # Event reporting must never terminate or stall a PTY.
                    pass
            except Exception:
                continue
        # Retain only enough text to complete one split OSC sequence. Ordinary
        # terminal scrollback and secrets must never accumulate in this parser.
        remainder = text[last_complete_end:]
        prefixes = ("\x1b]633;artifact;", ARTIFACT_LINE_PREFIX)
        start = max(remainder.rfind(prefix) for prefix in prefixes)
        session.artifact_tail = (
            remainder[start:][-9000:]
            if start >= 0
            else remainder[-(max(map(len, prefixes)) - 1) :]
        )

    def _discover_services(self, session: TerminalSession, chunk: bytes) -> None:
        if session.kind != "ssh" or not session.device_id:
            return
        text = chunk.decode("utf-8", errors="ignore")
        combined = ANSI_ESCAPE.sub("", session.discovery_tail + text).replace("\r", "\n")
        lines = combined.split("\n")
        session.discovery_tail = lines[-1][-512:]
        for line in lines[:-1]:
            self._discover_services_in_line(session, line, complete=True)
        # Many CLIs redraw a URL without a trailing newline. Parse the current
        # tail as well; the port-keyed map keeps this idempotent.
        self._discover_services_in_line(session, session.discovery_tail, complete=False)

    def _discover_services_in_line(
        self,
        session: TerminalSession, line: str, *, complete: bool
    ) -> None:
        now = time.time()
        product = service_product(line)
        if product:
            session.discovery_label_hint = product
            session.discovery_label_lines_left = 2
        hint = (
            session.discovery_label_hint
            if session.discovery_label_lines_left > 0
            else ""
        )
        has_service_context = bool(product or hint or SERVICE_URL_CONTEXT.search(line))
        is_reference = bool(NON_SERVICE_URL_CONTEXT.search(line))
        candidates: dict[int, str] = {}
        # Follow VS Code's output discovery behavior for local URLs: a URL is
        # itself a candidate. Obvious command/documentation references remain
        # excluded unless the line also contains a positive service signal.
        if has_service_context or not is_reference:
            for match in LOCAL_SERVICE_URL.finditer(line):
                port = int(match.group("port"))
                if 1 <= port <= 65535:
                    candidates[port] = match.group("url").rstrip(".,);]")
        for match in LOCAL_SERVICE_PORT.finditer(line):
            port = int(match.group("port"))
            if 1 <= port <= 65535:
                candidates.setdefault(port, f"http://localhost:{port}/")
        for port, url in candidates.items():
            label = product or hint or service_label(line, port)
            existing = session.services.get(port)
            if existing:
                existing.url = url
                existing.last_seen_at = now
                if existing.label.startswith("Web 服务"):
                    existing.label = label
                if existing.status == "offline" and now >= existing.retry_after:
                    existing.status = "checking"
                    existing.failure_count = 0
                    existing.error = ""
                    self.service_discovery_event.set()
                continue
            if len(session.services) >= MAX_DETECTED_SERVICES:
                removable = sorted(
                    (
                        service
                        for service in session.services.values()
                        if service.status != "online"
                    ),
                    key=lambda service: service.last_seen_at,
                )
                if not removable:
                    continue
                session.services.pop(removable[0].port, None)
            session.services[port] = DetectedService(
                port=port,
                url=url,
                label=label,
                detected_at=now,
                last_seen_at=now,
            )
            self.service_discovery_event.set()
        if complete and line.strip() and not product and session.discovery_label_lines_left:
            session.discovery_label_lines_left -= 1
            if session.discovery_label_lines_left == 0:
                session.discovery_label_hint = ""

    def _set_agent(
        self,
        session: TerminalSession,
        kind: str | None,
        cwd: str = "",
        source: str = "",
        pid: int | None = None,
        observation_confidence: float | None = None,
        return_code: int | None = None,
    ) -> None:
        """Apply one detector conclusion; notify only on real transitions.

        Detectors disagree at times, so conclusions carry a source priority
        (process > output): a weaker detector may enrich the current state but
        never contradict a stronger one. A creation-time hint is launch intent,
        not runtime evidence, and therefore never enters this state machine.
        Process evidence is the only source allowed to confirm an exit
        (kind=None), because banner text has no reliable "back to shell" signal.
        """
        if kind is not None and kind not in KNOWN_AGENT_KINDS:
            return
        previous = {
            "kind": session.agent_kind,
            "cwd": session.agent_cwd,
            "source": session.agent_source,
            "pid": session.agent_pid,
        }
        if kind == session.agent_kind:
            if kind is None:
                return
            # Same agent re-observed: enrich cwd/pid/source without resetting
            # `since`, so repeated banners never spam the state push.
            changed = False
            if _AGENT_SOURCE_PRIORITY.get(source, 0) >= _AGENT_SOURCE_PRIORITY.get(
                session.agent_source, 0
            ) and source != session.agent_source:
                session.agent_source = source
                changed = True
            if pid is not None and pid != session.agent_pid:
                session.agent_pid = pid
                changed = True
            if cwd and cwd != session.agent_cwd:
                session.agent_cwd = cwd
                changed = True
            if changed:
                self._notify_state_change()
                self._emit_agent_observation(
                    session,
                    previous,
                    confidence=observation_confidence,
                    return_code=return_code,
                )
            elif (
                source == "process"
                and session.agent_source == "process"
                and pid is not None
                and pid == session.agent_pid
            ):
                # A stable process is still a freshness sample. Refresh the
                # evidence TTL without emitting a compatibility-session update.
                self._emit_agent_observation(
                    session,
                    previous,
                    confidence=observation_confidence,
                    return_code=return_code,
                )
            return
        if kind is not None and _AGENT_SOURCE_PRIORITY.get(
            source, 0
        ) < _AGENT_SOURCE_PRIORITY.get(session.agent_source, 0):
            return
        session.agent_kind = kind
        session.agent_cwd = cwd
        session.agent_source = source if kind else ""
        session.agent_since = time.time() if kind else 0
        session.agent_pid = pid if kind else None
        self._notify_state_change()
        self._emit_agent_observation(
            session,
            previous,
            confidence=observation_confidence,
            return_code=return_code,
        )

    def _emit_agent_observation(
        self,
        session: TerminalSession,
        previous: dict[str, object],
        *,
        confidence: float | None = None,
        return_code: int | None = None,
    ) -> None:
        callback = self.agent_observation_callback
        if callback is None:
            return
        current_kind = session.agent_kind
        previous_kind = previous.get("kind")
        events: list[dict[str, object]] = []
        process_incarnation_changed = (
            previous.get("pid") is not None
            and previous.get("pid") != session.agent_pid
        )
        if previous_kind and (
            previous_kind != current_kind or process_incarnation_changed
        ):
            events.append(
                {
                    "type": "observation.process.exited",
                    "agent_kind": previous_kind,
                    "cwd": previous.get("cwd") or "",
                    "source": previous.get("source") or "process",
                    "pid": previous.get("pid"),
                    "return_code": return_code,
                    "confidence": (
                        0.95 if previous.get("source") == "process" else 0.7
                    ),
                }
            )
        if current_kind:
            events.append(
                {
                    "type": (
                        "observation.process.started"
                        if session.agent_source == "process"
                        else "observation.pty.signature"
                    ),
                    "agent_kind": current_kind,
                    "cwd": session.agent_cwd,
                    "source": session.agent_source,
                    "pid": session.agent_pid,
                    "confidence": (
                        confidence
                        if confidence is not None
                        else 0.95 if session.agent_source == "process" else 0.7
                    ),
                }
            )
        for event in events:
            try:
                callback(session, event)
            except Exception:
                # Observation persistence is diagnostic and must never break a
                # PTY reader or process reconciliation cycle.
                pass

    def _apply_agent_hint(
        self, session: TerminalSession, agent_hint: str | None
    ) -> None:
        """Store launch intent without presenting it as observed runtime state."""
        hint = (agent_hint or "").strip().lower()[:32]
        session.agent_hint = hint or None

    def _discover_agent(self, session: TerminalSession, chunk: bytes) -> None:
        """Recognize agent CLI startup banners in raw terminal output.

        Mirrors _discover_services (ANSI strip + rolling 512-char tail) but
        deliberately without its ssh-kind guard: local terminals run agents
        at least as often — TERMINAL_CMD defaults to codex.
        """
        text = chunk.decode("utf-8", errors="ignore")
        combined = ANSI_ESCAPE.sub("", session.agent_scan_tail + text).replace(
            "\r", "\n"
        )
        lines = combined.split("\n")
        session.agent_scan_tail = lines[-1][-512:]
        for line in lines:
            kind = agent_signature(line)
            if kind and kind != session.agent_kind:
                self._set_agent(session, kind, source="output")

    def service_candidates(self) -> list[tuple[TerminalSession, DetectedService]]:
        return [
            (session, service)
            for session in self.sessions.values()
            if session.active and session.device_id
            for service in session.services.values()
            if service.status != "offline"
        ]

    def sync_process_listeners(
        self,
        device_id: str,
        listeners: list[ListeningProcess],
        *,
        missing_threshold: int = 2,
    ) -> list[tuple[str, int]]:
        """Merge one device listener snapshot into terminal-owned services."""
        sessions = [
            session
            for session in self.sessions.values()
            if session.active and session.kind == "ssh" and session.device_id == device_id
        ]
        if not sessions:
            return []
        sessions.sort(key=lambda item: (item.last_activity_at, item.created_at), reverse=True)
        listener_by_port = {listener.port: listener for listener in listeners}
        known_by_port: dict[int, tuple[TerminalSession, DetectedService]] = {}
        duplicate_services: list[tuple[TerminalSession, DetectedService]] = []
        for session in sessions:
            for service in session.services.values():
                known = known_by_port.get(service.port)
                if not known or (
                    service.source != "process",
                    service.last_seen_at,
                    session.last_activity_at,
                ) > (
                    known[1].source != "process",
                    known[1].last_seen_at,
                    known[0].last_activity_at,
                ):
                    if known:
                        duplicate_services.append(known)
                    known_by_port[service.port] = (session, service)
                else:
                    duplicate_services.append((session, service))

        now = time.time()
        removed: list[tuple[str, int]] = []
        for session, service in duplicate_services:
            session.services.pop(service.port, None)
            if service.status == "online":
                removed.append((session.id, service.port))
        for port, listener in listener_by_port.items():
            known = known_by_port.get(port)
            if known:
                _session, service = known
                previous_pid = service.process_pid
                process_returned = service.process_seen_at is None
                if service.source == "output":
                    service.source = "hybrid"
                service.process_pid = listener.pid
                service.process_detail = listener.command
                service.process_seen_at = now
                service.process_missing_count = 0
                if service.label.startswith("Web 服务"):
                    service.label = process_service_label(listener.command, port)
                process_restarted = (
                    previous_pid is not None
                    and listener.pid is not None
                    and previous_pid != listener.pid
                )
                if service.status == "offline" and (
                    process_restarted or process_returned or now >= service.retry_after
                ):
                    service.status = "checking"
                    service.failure_count = 0
                    service.retry_after = 0
                    service.error = ""
                    self.service_discovery_event.set()
                continue

            owner = sessions[0]
            if len(owner.services) >= MAX_PROCESS_SERVICE_CANDIDATES:
                removable = sorted(
                    (service for service in owner.services.values() if service.status != "online"),
                    key=lambda service: service.last_seen_at,
                )
                if not removable:
                    continue
                owner.services.pop(removable[0].port, None)
            owner.services[port] = DetectedService(
                port=port,
                url=f"http://localhost:{port}/",
                label=process_service_label(listener.command, port),
                detected_at=now,
                last_seen_at=now,
                source="process",
                process_pid=listener.pid,
                process_detail=listener.command,
                process_seen_at=now,
            )
            self.service_discovery_event.set()

        for session in sessions:
            for service in tuple(session.services.values()):
                if service.source not in {"process", "hybrid"}:
                    continue
                if service.port in listener_by_port:
                    continue
                service.process_missing_count += 1
                if service.process_missing_count < max(1, missing_threshold):
                    continue
                was_visible = service.status == "online"
                service.status = "offline"
                service.error = "监听进程已停止"
                service.failure_count = 0
                service.retry_after = now + SERVICE_REDISCOVERY_COOLDOWN
                service.process_seen_at = None
                service.process_missing_count = 0
                if was_visible:
                    removed.append((session.id, service.port))
        if removed:
            self._notify_state_change()
        return removed

    def sync_device_agents(self, device_id: str, agents: list[RemoteAgent]) -> None:
        """Reconcile one *complete* remote Agent process snapshot.

        A device-wide process table does not identify which managed SSH shell
        owns an arbitrary process. Preserve exact evidence (a previously seen
        PID or one terminal's matching output banner), and only infer an owner
        when one remaining process and one remaining terminal make the mapping
        unambiguous. Ambiguous processes stay unassigned rather than being
        attached to the most recently active terminal.

        The caller must not invoke this method for an unsupported/failed probe;
        an empty list deliberately means "scan succeeded and no Agent exists"
        and therefore clears stale output and process observations.
        """
        observed = [agent for agent in agents if agent.kind in KNOWN_AGENT_KINDS]
        sessions = [
            session
            for session in self.sessions.values()
            if session.active and session.kind == "ssh" and session.device_id == device_id
        ]
        if not sessions:
            self._publish_unattributed_agents(device_id, observed, set())
            return
        assignments: dict[str, tuple[int, float]] = {}
        used_agents: set[int] = set()

        # A PID retained from the preceding snapshot is the strongest mapping
        # available without a device-side reporter.
        by_pid = {
            agent.pid: index
            for index, agent in enumerate(observed)
            if agent.pid is not None
        }
        for session in sessions:
            index = by_pid.get(session.agent_pid)
            if (
                session.agent_source == "process"
                and index is not None
                and index not in used_agents
                and observed[index].kind == session.agent_kind
            ):
                assignments[session.id] = (index, 0.95)
                used_agents.add(index)

        # A terminal-local banner can correlate one uniquely matching process.
        for kind in KNOWN_AGENT_KINDS:
            matching_sessions = [
                session
                for session in sessions
                if session.id not in assignments
                and session.agent_source == "output"
                and session.agent_kind == kind
            ]
            matching_agents = [
                index
                for index, agent in enumerate(observed)
                if index not in used_agents and agent.kind == kind
            ]
            if len(matching_sessions) == 1 and len(matching_agents) == 1:
                assignments[matching_sessions[0].id] = (matching_agents[0], 0.8)
                used_agents.add(matching_agents[0])

        # With exactly one candidate on each side there is no arbitrary
        # ``agents[0]`` choice. Any larger cardinality requires an explicit
        # terminal/run identifier and remains unassigned in Phase 0.
        remaining_sessions = [
            session for session in sessions if session.id not in assignments
        ]
        remaining_agents = [
            index for index in range(len(observed)) if index not in used_agents
        ]
        if len(remaining_sessions) == 1 and len(remaining_agents) == 1:
            assignments[remaining_sessions[0].id] = (remaining_agents[0], 0.6)
            used_agents.add(remaining_agents[0])

        unassigned_kinds = {
            agent.kind
            for index, agent in enumerate(observed)
            if index not in used_agents
        }
        self._publish_unattributed_agents(device_id, observed, used_agents)
        for session in sessions:
            assignment = assignments.get(session.id)
            if assignment is not None:
                index, confidence = assignment
                agent = observed[index]
                # Remote process scans do not currently provide cwd.
                self._set_agent(
                    session,
                    agent.kind,
                    source="process",
                    pid=agent.pid,
                    observation_confidence=confidence,
                )
                continue
            if session.agent_source == "process":
                self._set_agent(session, None, source="process")
            elif session.agent_source == "output" and (
                not observed or session.agent_kind not in unassigned_kinds
            ):
                self._set_agent(session, None, source="process")

    def _publish_unattributed_agents(
        self,
        device_id: str,
        observed: list[RemoteAgent],
        used_agents: set[int],
    ) -> None:
        callback = self.agent_observation_callback
        current = {
            int(agent.pid): agent
            for index, agent in enumerate(observed)
            if index not in used_agents and agent.pid is not None
        }
        previous = self._unattributed_agents.get(device_id, {})
        self._unattributed_agents[device_id] = current
        if callback is None:
            return
        for pid, agent in previous.items():
            if pid in current and current[pid].kind == agent.kind:
                continue
            try:
                callback(
                    None,
                    {
                        "type": "observation.process.exited",
                        "device_id": device_id,
                        "agent_kind": agent.kind,
                        "pid": pid,
                        "source": "process",
                        "confidence": 0.8,
                        "unattributed": True,
                    },
                )
            except Exception:
                pass
        for pid, agent in current.items():
            if pid in previous and previous[pid].kind == agent.kind:
                continue
            try:
                callback(
                    None,
                    {
                        "type": "observation.process.started",
                        "device_id": device_id,
                        "agent_kind": agent.kind,
                        "pid": pid,
                        "source": "process",
                        "confidence": 0.6,
                        "unattributed": True,
                    },
                )
            except Exception:
                pass

    def update_service_status(
        self,
        session_id: str,
        port: int,
        *,
        online: bool,
        error: str = "",
        failure_threshold: int = 2,
    ) -> tuple[DetectedService | None, bool]:
        session = self.sessions.get(session_id)
        service = session and session.services.get(port)
        if not service:
            return None, False
        previous = service.status
        service.last_checked_at = time.time()
        if online:
            service.status = "online"
            service.error = ""
            service.failure_count = 0
        else:
            service.failure_count += 1
            service.error = error or "服务端口当前不可访问"
            if service.failure_count >= max(1, failure_threshold):
                service.status = "offline"
            elif service.status != "online":
                service.status = "checking"
        became_offline = previous != "offline" and service.status == "offline"
        if became_offline:
            service.retry_after = time.time() + SERVICE_REDISCOVERY_COOLDOWN
        if previous != service.status:
            self._notify_state_change()
        return service, became_offline

    def _broadcast(self, session: TerminalSession, chunk: bytes) -> None:
        for queue in tuple(session.subscribers):
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                # Dropping a queued chunk would punch a hole into the middle of a
                # raw VT stream: truncated UTF-8 or CSI/OSC sequences, and lost
                # mode changes (alt screen, SGR, DECSTBM) that leave the emulator
                # wrong for the rest of the connection — with nothing able to
                # detect it. Discard everything pending for this one subscriber
                # and hand its sender an explicit gap marker to resync from.
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(STREAM_GAP)

    def _mark_exited(self, session: TerminalSession) -> None:
        if self.backend == "tmux":
            self._close_client(session)
            self._refresh_tmux_state(session)
            return
        if not session.active:
            return
        self.loop.remove_reader(session.fd)
        self.loop.remove_writer(session.fd)
        try:
            os.close(session.fd)
        except OSError:
            pass
        try:
            waited_pid, status = os.waitpid(session.pid, os.WNOHANG)
            session.return_code = os.waitstatus_to_exitcode(status) if waited_pid else None
            if waited_pid:
                self._owned_pids.discard(session.pid)
        except ChildProcessError:
            session.return_code = None
            self._owned_pids.discard(session.pid)
        session.exited_at = time.time()
        self._notify_state_change()
        marker = b"\r\n\x1b[90m[process exited]\x1b[0m\r\n"
        self._append(session, marker)
        self._broadcast(session, marker)
        if session.return_code is None:
            self.loop.call_later(0.05, self._reap_child, session, 0)
        else:
            self._finalize_session_exit(session)

    def _reap_child(self, session: TerminalSession, attempt: int) -> None:
        if session.return_code is not None:
            return
        try:
            waited_pid, status = os.waitpid(session.pid, os.WNOHANG)
        except ChildProcessError:
            self._owned_pids.discard(session.pid)
            return
        if waited_pid:
            session.return_code = os.waitstatus_to_exitcode(status)
            self._owned_pids.discard(session.pid)
            self._notify_state_change()
            self._finalize_session_exit(session)
        elif attempt < 20:
            self.loop.call_later(0.05, self._reap_child, session, attempt + 1)

    def attach(self, session_id: str) -> tuple[bytes, asyncio.Queue[bytes | StreamGap]]:
        session = self.sessions[session_id]
        if self.backend == "tmux" and session.active and session.fd < 0:
            self._spawn_tmux_client(session)
        queue: asyncio.Queue[bytes | StreamGap] = asyncio.Queue(maxsize=1024)
        session.subscribers.add(queue)
        return b"".join(session.chunks), queue

    def detach(self, session_id: str, queue: asyncio.Queue[bytes | StreamGap]) -> None:
        session = self.sessions.get(session_id)
        if session:
            session.subscribers.discard(queue)

    def write(self, session_id: str, data: bytes) -> None:
        session = self.sessions.get(session_id)
        if not session or not session.active or not data:
            return
        session.pending_input.extend(data)
        self._flush_input(session_id)

    def _flush_input(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session or not session.active:
            return
        while session.pending_input:
            try:
                written = os.write(session.fd, session.pending_input)
                if written == 0:
                    break
                del session.pending_input[:written]
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self.loop.add_writer(session.fd, self._flush_input, session_id)
                    return
                if exc.errno == errno.EIO:
                    session.pending_input.clear()
                    return
                raise
        self.loop.remove_writer(session.fd)

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        session = self.sessions.get(session_id)
        if not session or not session.active or session.fd < 0:
            return
        cols = min(max(cols, 2), 500)
        rows = min(max(rows, 1), 300)
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(session.fd, termios.TIOCSWINSZ, size)
        try:
            foreground_pgid = os.tcgetpgrp(session.fd)
            os.killpg(foreground_pgid, signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def _session_pids(self, leader_pid: int) -> list[int]:
        # kill(2) assigns broadcast semantics to non-positive PIDs. PTY child
        # leaders are always greater than 1, so reject sentinel values early.
        if leader_pid <= 1:
            return []
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,ppid="],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return [leader_pid]

        children: dict[int, list[int]] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            try:
                process_pid, parent_pid = map(int, fields)
            except ValueError:
                continue
            children.setdefault(parent_pid, []).append(process_pid)

        pids = [leader_pid]
        index = 0
        while index < len(pids):
            pids.extend(children.get(pids[index], ()))
            index += 1
        return pids

    def _probe_session_agent(
        self, session: TerminalSession
    ) -> tuple[str, int, str] | None:
        """Scan one session's process tree for a known agent process.

        Returns (kind, pid, cwd) on a hit, ("", 0, "") when the tree was
        scanned and no agent was found, or None when the tree could not be
        inspected (no live leader, tmux query failed) — callers must treat
        None as "unknown", never as "agent exited".
        """
        leader_pid = session.pid
        if session.tmux_name:
            # The PTY leader is the local `tmux attach` client; the real
            # shell lives inside the tmux server, so resolve its pane pid.
            result = self._tmux_run(
                "display-message",
                "-p",
                "-t",
                session.tmux_name,
                "#{pane_pid}",
                check=False,
            )
            try:
                leader_pid = int(result.stdout.strip())
            except ValueError:
                return None
        if leader_pid <= 1:
            return None
        for pid in self._session_pids(leader_pid):
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                continue
            try:
                raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                raw_cmdline = b""
            args = raw_cmdline.replace(b"\0", b" ").decode(
                "utf-8", errors="ignore"
            ).strip()
            kind = agent_kind_from_process(comm, args)
            if not kind:
                continue
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = ""
            return kind, pid, cwd
        return "", 0, ""

    def local_agent_probe_targets(self) -> tuple[TerminalSession, ...]:
        """Snapshot local sessions on the event-loop thread before scanning."""
        return tuple(
            session
            for session in self.sessions.values()
            if session.active and session.kind == "local"
        )

    def scan_local_agents(
        self, targets: tuple[TerminalSession, ...]
    ) -> list[tuple[str, tuple[str, int, str] | None]]:
        """Collect process evidence without mutating event-loop-owned state."""
        observations: list[tuple[str, tuple[str, int, str] | None]] = []
        for session in targets:
            try:
                probe = self._probe_session_agent(session)
            except Exception:
                # One unreadable /proc entry must not skip the other sessions.
                probe = None
            observations.append((session.id, probe))
        return observations

    def apply_local_agent_probes(
        self, observations: list[tuple[str, tuple[str, int, str] | None]]
    ) -> None:
        """Apply collected evidence from the event-loop thread."""
        for session_id, probe in observations:
            session = self.sessions.get(session_id)
            if not session or not session.active or session.kind != "local":
                continue
            if probe is None:
                continue
            kind, pid, cwd = probe
            if kind:
                self._set_agent(
                    session,
                    kind,
                    cwd,
                    source="process",
                    pid=pid,
                    observation_confidence=0.99,
                )
            elif session.agent_source in {"output", "process"}:
                self._set_agent(session, None, source="process")

    def probe_local_agents(self) -> None:
        """Synchronously collect and apply local process evidence.

        This compatibility entry point is useful to tests and explicit probes.
        The periodic service loop uses ``scan_local_agents`` in a worker and
        calls ``apply_local_agent_probes`` back on the event loop.
        """
        targets = self.local_agent_probe_targets()
        self.apply_local_agent_probes(self.scan_local_agents(targets))

    def _signal_session(
        self, leader_pid: int, sig: signal.Signals
    ) -> int | None:
        if leader_pid <= 1 or leader_pid not in self._owned_pids:
            return None
        # A PTY leader is always this server's child. Verify that relationship
        # before signaling so stale or fabricated PIDs cannot affect unrelated
        # processes owned by the same operating-system user.
        try:
            waited_pid, status = os.waitpid(leader_pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if waited_pid:
            self._owned_pids.discard(leader_pid)
            return os.waitstatus_to_exitcode(status)
        pids = [pid for pid in self._session_pids(leader_pid) if pid > 1]
        pids.sort(key=lambda pid: pid == leader_pid)
        for process_pid in pids:
            try:
                os.kill(process_pid, sig)
            except ProcessLookupError:
                pass
        return None

    async def delete(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if not session:
            return False
        if self.backend == "tmux":
            self._stop_tmux_artifact_capture(session)
            self._close_client(session)
            if session.tmux_name:
                self._tmux_run("kill-session", "-t", session.tmux_name, check=False)
            if self.store:
                self.store.delete(session_id)
            session.exited_at = session.exited_at or time.time()
            self._finalize_session_exit(session)
            session.subscribers.clear()
            self.sessions.pop(session_id, None)
            self._notify_state_change()
            return True
        if session.active:
            if session.pid > 1:
                startup_grace = 0.5 - (time.time() - session.created_at)
                if startup_grace > 0:
                    await asyncio.sleep(startup_grace)
                reaped_return_code = self._signal_session(
                    session.pid, signal.SIGTERM
                )
                if reaped_return_code is not None:
                    session.return_code = reaped_return_code
                    self._owned_pids.discard(session.pid)
                else:
                    for _ in range(10):
                        try:
                            waited_pid, status = os.waitpid(session.pid, os.WNOHANG)
                        except ChildProcessError:
                            waited_pid = session.pid
                            status = 0
                        if waited_pid:
                            session.return_code = os.waitstatus_to_exitcode(status)
                            self._owned_pids.discard(session.pid)
                            break
                        await asyncio.sleep(0.05)
                    else:
                        try:
                            self._signal_session(session.pid, signal.SIGKILL)
                            os.waitpid(session.pid, 0)
                        except (ProcessLookupError, ChildProcessError):
                            pass
                        self._owned_pids.discard(session.pid)
                        session.return_code = -signal.SIGKILL
            if session.fd >= 0:
                self.loop.remove_reader(session.fd)
                self.loop.remove_writer(session.fd)
                try:
                    os.close(session.fd)
                except OSError:
                    pass
            session.exited_at = time.time()
        self._finalize_session_exit(session)
        session.subscribers.clear()
        self.sessions.pop(session_id, None)
        self._notify_state_change()
        return True

    async def close(self) -> None:
        if self.backend == "tmux":
            for session in tuple(self.sessions.values()):
                self._stop_tmux_artifact_capture(session)
                self._close_client(session)
                session.subscribers.clear()
            return
        for session_id in tuple(self.sessions):
            await self.delete(session_id)
