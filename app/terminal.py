from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import re
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


RESIZE_MESSAGE = re.compile(r"^\x01\[(\d+),(\d+)\]$")
SNAPSHOT_COMPLETE_MESSAGE = "\x01[snapshot-complete]"
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
REMOTE_SHELL_COMMANDS = {
    "system": [],
    "powershell": ["powershell.exe", "-NoLogo", "-NoExit"],
    "cmd": ["cmd.exe", "/Q"],
}


def remote_shell_command(remote_shell: str) -> list[str]:
    """Return the SSH remote command for a validated device shell choice."""
    return list(REMOTE_SHELL_COMMANDS.get(remote_shell, ()))


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
    kind: str = "local"
    device_id: str | None = None
    device_name: str | None = None
    remote_port: int | None = None
    tmux_name: str | None = None
    created_at: float = field(default_factory=time.time)
    exited_at: float | None = None
    return_code: int | None = None
    chunks: deque[bytes] = field(default_factory=deque)
    buffer_size: int = 0
    subscribers: set[asyncio.Queue[bytes]] = field(default_factory=set)
    pending_input: bytearray = field(default_factory=bytearray)
    services: dict[int, DetectedService] = field(default_factory=dict)
    discovery_tail: str = ""
    discovery_label_hint: str = ""
    discovery_label_lines_left: int = 0
    last_activity_at: float = field(default_factory=time.time)

    @property
    def active(self) -> bool:
        return self.exited_at is None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "cwd": self.cwd,
            "kind": self.kind,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "remote_port": self.remote_port,
            "created_at": self.created_at,
            "active": self.active,
            "return_code": self.return_code,
            "services": [
                service.as_dict()
                for service in sorted(self.services.values(), key=lambda item: item.port)
                if service.status == "online"
            ],
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
                    remote_port, tmux_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self.store: TerminalStore | None = None
        self.sessions: dict[str, TerminalSession] = {}
        self._owned_pids: set[int] = set()
        self.loop = asyncio.get_running_loop()
        self.service_discovery_event = asyncio.Event()
        if self.backend == "tmux":
            if not shutil.which(self.tmux_binary):
                raise RuntimeError(f"tmux executable not found: {self.tmux_binary}")
            if database_path is None or tmux_socket is None:
                raise ValueError("tmux backend requires database_path and tmux_socket")
            self.tmux_socket = Path(tmux_socket).expanduser().resolve()
            self.store = TerminalStore(Path(database_path).expanduser().resolve())
            self._restore_tmux_sessions()

    def list(self) -> list[dict[str, object]]:
        if self.backend == "tmux":
            for session in tuple(self.sessions.values()):
                if session.active:
                    self._refresh_tmux_state(session)
        sessions = sorted(self.sessions.values(), key=lambda item: item.created_at)
        return [session.as_dict() for session in sessions]

    def get(self, session_id: str) -> TerminalSession | None:
        session = self.sessions.get(session_id)
        if session and self.backend == "tmux":
            if session.active:
                self._refresh_tmux_state(session)
            if (
                session.active
                and session.fd < 0
                and self._tmux_session_exists(session.tmux_name or "")
            ):
                self._spawn_tmux_client(session)
        return session

    def create(self, name: str | None = None, cols: int = 120, rows: int = 32) -> TerminalSession:
        if not Path(self.cwd).is_dir():
            raise ValueError(f"TERMINAL_CWD does not exist: {self.cwd}")
        if not Path(self.shell).is_file() or not os.access(self.shell, os.X_OK):
            raise ValueError(f"TERMINAL_SHELL is not executable: {self.shell}")

        if self.backend == "tmux":
            return self._create_tmux_session(
                name=name or "Terminal",
                command=shlex.join([self.shell, "-l"]),
                cols=cols,
                rows=rows,
                initial_input=self.command.strip() or None,
            )

        session_id = uuid.uuid4().hex
        display_name = (name or "Terminal").strip()[:80] or "Terminal"
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.cwd)
                environment = os.environ.copy()
                environment.setdefault("TERM", "xterm-256color")
                environment.setdefault("COLORTERM", "truecolor")
                if self.proxy:
                    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                        environment[key] = self.proxy
                environment["SHELL"] = self.shell
                login_argv0 = f"-{Path(self.shell).name}"
                os.execvpe(self.shell, [login_argv0], environment)
            except BaseException as exc:
                os.write(2, f"Unable to start terminal: {exc}\r\n".encode())
                os._exit(127)

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._owned_pids.add(pid)
        session = TerminalSession(
            id=session_id,
            name=display_name,
            pid=pid,
            fd=fd,
            command=self.command,
            cwd=self.cwd,
        )
        self.sessions[session_id] = session
        self.resize(session_id, cols, rows)
        self.loop.add_reader(fd, self._read_ready, session_id)
        if self.command.strip():
            initial_input = f"{self.command}\r".encode("utf-8")
            self.loop.call_later(0.05, self.write, session_id, initial_input)
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
    ) -> TerminalSession:
        if not argv:
            raise ValueError("Process command is empty")
        executable = shutil.which(argv[0])
        if not executable:
            raise ValueError(f"Executable not found: {argv[0]}")
        if not Path(self.cwd).is_dir():
            raise ValueError(f"TERMINAL_CWD does not exist: {self.cwd}")

        if self.backend == "tmux":
            return self._create_tmux_session(
                name=name,
                command=shlex.join([executable, *argv[1:]]),
                cols=cols,
                rows=rows,
                kind="ssh",
                device_id=device_id,
                device_name=device_name,
                remote_port=remote_port,
            )

        session_id = uuid.uuid4().hex
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.cwd)
                environment = os.environ.copy()
                environment.setdefault("TERM", "xterm-256color")
                environment.setdefault("COLORTERM", "truecolor")
                os.execvpe(executable, [executable, *argv[1:]], environment)
            except BaseException as exc:
                os.write(2, f"Unable to start process: {exc}\r\n".encode())
                os._exit(127)

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._owned_pids.add(pid)
        session = TerminalSession(
            id=session_id,
            name=name.strip()[:80] or "SSH Terminal",
            pid=pid,
            fd=fd,
            command=shlex.join(argv),
            cwd=self.cwd,
            kind="ssh",
            device_id=device_id,
            device_name=device_name,
            remote_port=remote_port,
        )
        self.sessions[session_id] = session
        self.resize(session_id, cols, rows)
        self.loop.add_reader(fd, self._read_ready, session_id)
        return session

    def _tmux_command(self, *arguments: str) -> list[str]:
        if self.tmux_socket is None:
            raise RuntimeError("tmux socket is not configured")
        return [self.tmux_binary, "-S", str(self.tmux_socket), *arguments]

    def _tmux_run(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            self._tmux_command(*arguments), capture_output=True, text=True
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

    def _restore_tmux_sessions(self) -> None:
        if self.store is None:
            return
        self._configure_tmux_server()
        for record in self.store.list():
            session = TerminalSession(
                id=str(record["id"]),
                name=str(record["name"]),
                pid=-1,
                fd=-1,
                command=str(record["command"]),
                cwd=str(record["cwd"]),
                kind=str(record["kind"]),
                device_id=record["device_id"] and str(record["device_id"]),
                device_name=record["device_name"] and str(record["device_name"]),
                remote_port=(
                    int(record["remote_port"])
                    if record["remote_port"] is not None
                    else None
                ),
                tmux_name=str(record["tmux_name"]),
                created_at=float(record["created_at"]),
            )
            if self._tmux_session_exists(session.tmux_name):
                history = self._capture_tmux_history(session.tmux_name)
                if history:
                    self._append(session, history)
                self.sessions[session.id] = session
                self._refresh_tmux_state(session)
                if session.active:
                    self._spawn_tmux_client(session)
            else:
                session.exited_at = time.time()
                session.return_code = -1
                self._append(
                    session,
                    b"\r\n\x1b[90m[tmux session unavailable after host restart]\x1b[0m\r\n",
                )
                self.sessions[session.id] = session

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
    ) -> TerminalSession:
        if self.store is None:
            raise RuntimeError("Persistent terminal store is not configured")
        session_id = uuid.uuid4().hex
        tmux_name = f"agentserver-{session_id}"
        cols = min(max(cols, 2), 500)
        rows = min(max(rows, 1), 300)
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
            self.cwd,
            command,
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
                cwd=self.cwd,
                kind=kind,
                device_id=device_id,
                device_name=device_name,
                remote_port=remote_port,
                tmux_name=tmux_name,
            )
            self.store.save(session)
        except BaseException:
            self._tmux_run("kill-session", "-t", tmux_name, check=False)
            raise
        self.sessions[session.id] = session
        self._spawn_tmux_client(session)
        if initial_input:
            self._tmux_run("send-keys", "-t", tmux_name, "-l", initial_input)
            self._tmux_run("send-keys", "-t", tmux_name, "Enter")
        return session

    def _spawn_tmux_client(self, session: TerminalSession) -> None:
        if session.fd >= 0 or not session.tmux_name:
            return
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(session.cwd if Path(session.cwd).is_dir() else self.cwd)
                environment = os.environ.copy()
                environment.setdefault("TERM", "xterm-256color")
                environment.setdefault("COLORTERM", "truecolor")
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

    def _refresh_tmux_state(self, session: TerminalSession) -> None:
        if not session.tmux_name or not self._tmux_session_exists(session.tmux_name):
            session.exited_at = session.exited_at or time.time()
            session.return_code = session.return_code if session.return_code is not None else -1
            return
        result = self._tmux_run(
            "display-message",
            "-p",
            "-t",
            session.tmux_name,
            "#{pane_dead}:#{pane_dead_status}",
            check=False,
        )
        fields = result.stdout.strip().split(":", 1)
        if result.returncode == 0 and fields[0] == "1":
            session.exited_at = session.exited_at or time.time()
            try:
                session.return_code = int(fields[1])
            except (IndexError, ValueError):
                session.return_code = None
        elif result.returncode == 0:
            session.exited_at = None
            session.return_code = None

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
                self._append(session, chunk)
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
        return removed

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
        return service, became_offline

    def _broadcast(self, session: TerminalSession, chunk: bytes) -> None:
        for queue in tuple(session.subscribers):
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(chunk)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

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
        marker = b"\r\n\x1b[90m[process exited]\x1b[0m\r\n"
        self._append(session, marker)
        self._broadcast(session, marker)
        if session.return_code is None:
            self.loop.call_later(0.05, self._reap_child, session, 0)

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
        elif attempt < 20:
            self.loop.call_later(0.05, self._reap_child, session, attempt + 1)

    def attach(self, session_id: str) -> tuple[bytes, asyncio.Queue[bytes]]:
        session = self.sessions[session_id]
        if self.backend == "tmux" and session.active and session.fd < 0:
            self._spawn_tmux_client(session)
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        session.subscribers.add(queue)
        return b"".join(session.chunks), queue

    def detach(self, session_id: str, queue: asyncio.Queue[bytes]) -> None:
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

    def _signal_session(self, leader_pid: int, sig: signal.Signals) -> None:
        if leader_pid <= 1 or leader_pid not in self._owned_pids:
            return
        # A PTY leader is always this server's child. Verify that relationship
        # before signaling so stale or fabricated PIDs cannot affect unrelated
        # processes owned by the same operating-system user.
        try:
            waited_pid, _status = os.waitpid(leader_pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited_pid:
            self._owned_pids.discard(leader_pid)
            return
        pids = [pid for pid in self._session_pids(leader_pid) if pid > 1]
        pids.sort(key=lambda pid: pid == leader_pid)
        for process_pid in pids:
            try:
                os.kill(process_pid, sig)
            except ProcessLookupError:
                pass

    async def delete(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if not session:
            return False
        if self.backend == "tmux":
            self._close_client(session)
            if session.tmux_name:
                self._tmux_run("kill-session", "-t", session.tmux_name, check=False)
            if self.store:
                self.store.delete(session_id)
            session.exited_at = time.time()
            session.subscribers.clear()
            self.sessions.pop(session_id, None)
            return True
        if session.active:
            if session.pid > 1:
                startup_grace = 0.5 - (time.time() - session.created_at)
                if startup_grace > 0:
                    await asyncio.sleep(startup_grace)
                self._signal_session(session.pid, signal.SIGTERM)
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
        session.subscribers.clear()
        self.sessions.pop(session_id, None)
        return True

    async def close(self) -> None:
        if self.backend == "tmux":
            for session in tuple(self.sessions.values()):
                self._close_client(session)
                session.subscribers.clear()
            return
        for session_id in tuple(self.sessions):
            await self.delete(session_id)
