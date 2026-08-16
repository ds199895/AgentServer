from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shlex
import sqlite3
import socket
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as websocket_connect

from .artifacts import (
    ArtifactEventStore,
    AttachmentAccessDenied,
    AttachmentIntegrityError,
    AttachmentStore,
    ImageValidationError,
    ImageSupportUnavailable,
    WorkspaceFileRef as ArtifactFileRef,
    build_openai_responses_image_content,
    build_read_image_result,
)
from .auth import SessionSigner, UserStore, load_or_create_secret
from .devices import DeviceStore, FrpMonitor, probe_ssh
from .preview import (
    PREVIEW_COOKIE_NAME,
    PreviewHostMiddleware,
    PreviewManager,
    preview_id_from_host,
    preview_public_url,
    rewrite_frame_ancestors,
    rewrite_set_cookie,
    upstream_cookie,
)
from .terminal import (
    LISTENER_SCAN_MARKER,
    ListeningProcess,
    RESIZE_MESSAGE,
    SNAPSHOT_COMPLETE_MESSAGE,
    STREAM_GAP,
    TerminalManager,
    parse_listener_scan,
    remote_shell_command,
)
from .version import resolve_build_sha, verify_release_pair
from .workspace import (
    FileGrant,
    LocalWorkspaceProvider,
    SftpWorkspaceProvider,
    WorkspaceAccessDenied,
    WorkspaceConfigurationError,
    WorkspaceError,
    WorkspaceFileChanged,
    WorkspaceGrantNotFound,
    WorkspaceInvalidRange,
    WorkspaceNotDirectory,
    WorkspaceNotFile,
    WorkspaceNotFound,
    WorkspaceService,
    WorkspaceTooLarge,
    WorkspaceUnavailable,
)


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data")).expanduser()
COOKIE_NAME = "agentserver_session"
BUILD_SHA = resolve_build_sha(ROOT)

users = UserStore(DATA_DIR / "agent_server.db")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
if len(admin_password) < 8:
    raise RuntimeError("ADMIN_PASSWORD must be explicitly set to at least 8 characters")
users.ensure_user(ADMIN_USERNAME, admin_password)
session_secret = load_or_create_secret(DATA_DIR)
signer = SessionSigner(session_secret)
preview_signer = SessionSigner(session_secret, max_age=120)
preview_access_signer = SessionSigner(session_secret, max_age=24 * 60 * 60)
devices = DeviceStore(DATA_DIR / "agent_server.db")


DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
# Terminal scrollback replays are sliced to this size so a single attach cannot
# hand the event loop a multi-megabyte frame in one go.
SNAPSHOT_CHUNK_BYTES = 64 * 1024
# Session-state pushes are coalesced over this window so a burst of transitions
# (a device reconnecting, several services going offline) sends one snapshot.
SESSION_PUSH_DEBOUNCE_SECONDS = 0.25
DOWNLOAD_FILES = {
    "install-frpc-ssh.sh": (ROOT / "scripts" / "install_frpc_ssh.sh", "text/x-shellscript"),
    "install-frpc-ssh.ps1": (ROOT / "scripts" / "install_frpc_ssh.ps1", "text/plain"),
    "frpc.example.toml": (ROOT / "frpc.example.toml", "application/toml"),
    "agentserver-ssh-key.pub": (
        ROOT / "deploy" / "agentserver_ssh_key.pub",
        "text/plain",
    ),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    artifact_events = ArtifactEventStore(DATA_DIR / "agent_server.db")
    attachments = AttachmentStore(
        DATA_DIR / "attachments",
        max_image_bytes=int(
            os.getenv("MAX_IMAGE_ATTACHMENT_BYTES", str(5 * 1024 * 1024))
        ),
        max_image_pixels=int(os.getenv("MAX_IMAGE_ATTACHMENT_PIXELS", "40000000")),
    )
    artifact_ingest_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
        maxsize=max(1, int(os.getenv("ARTIFACT_INGEST_QUEUE_SIZE", "1024")))
    )
    artifact_rate_windows: dict[str, deque[float]] = {}
    artifact_ingest_stats = {"dropped": 0, "failed": 0}

    async def artifact_ingest_worker() -> None:
        while True:
            values = await artifact_ingest_queue.get()
            try:
                await asyncio.to_thread(artifact_events.append, **values)
            except Exception:
                artifact_ingest_stats["failed"] += 1
            finally:
                artifact_ingest_queue.task_done()

    def record_terminal_artifact(session, payload: dict[str, object]) -> None:
        if not session.owner:
            return
        path = payload.get("path")
        if not isinstance(path, str) or not path or len(path) > 4096:
            return
        size = payload.get("size")
        now = asyncio.get_running_loop().time()
        window = artifact_rate_windows.setdefault(session.id, deque())
        while window and window[0] <= now - 1:
            window.popleft()
        if len(window) >= 20 or artifact_ingest_queue.full():
            artifact_ingest_stats["dropped"] += 1
            return
        window.append(now)
        artifact_ingest_queue.put_nowait(
            {
                "owner": session.owner,
                "terminal_id": session.id,
                "event_type": str(payload.get("type") or "created")[:80],
                "file": ArtifactFileRef(
                    path=path,
                    name=str(payload.get("name") or "")[:255],
                    media_type=(
                        str(payload["media_type"])[:255]
                        if payload.get("media_type")
                        else None
                    ),
                    size=size if isinstance(size, int) and size >= 0 else None,
                    kind=str(payload.get("kind") or "file")[:40],
                ),
                "source": str(payload.get("source") or "terminal-output")[:80],
                "version": str(payload.get("version") or "")[:255],
            }
        )

    app.state.artifacts = artifact_events
    app.state.attachments = attachments
    app.state.artifact_ingest_queue = artifact_ingest_queue
    app.state.artifact_ingest_stats = artifact_ingest_stats
    app.state.artifact_rate_windows = artifact_rate_windows
    app.state.workspaces = WorkspaceService(
        grant_ttl=float(os.getenv("FILE_GRANT_TTL", "120")),
        max_file_bytes=int(
            os.getenv("MAX_WORKSPACE_FILE_BYTES", str(32 * 1024 * 1024))
        ),
        max_read_bytes=int(
            os.getenv("MAX_WORKSPACE_READ_BYTES", str(32 * 1024 * 1024))
        ),
        max_list_entries=int(os.getenv("MAX_WORKSPACE_LIST_ENTRIES", "1000")),
        max_image_pixels=int(os.getenv("MAX_IMAGE_ATTACHMENT_PIXELS", "40000000")),
    )
    app.state.terminals = TerminalManager(
        command=os.getenv("TERMINAL_CMD", "codex"),
        cwd=os.getenv("TERMINAL_CWD", str(ROOT)),
        shell=os.getenv("TERMINAL_SHELL") or None,
        proxy=os.getenv("TERMINAL_PROXY") or None,
        scrollback_bytes=int(os.getenv("TERMINAL_SCROLLBACK_BYTES", str(2 * 1024 * 1024))),
        backend=os.getenv(
            "TERMINAL_BACKEND",
            "tmux" if os.getenv("ENVIRONMENT") == "production" else "direct",
        ),
        database_path=DATA_DIR / "agent_server.db",
        tmux_binary=os.getenv("TMUX_BINARY", "tmux"),
        tmux_socket=Path(
            os.getenv("TMUX_SOCKET", str(DATA_DIR / "tmux" / "agentserver.sock"))
        ),
        default_owner=ADMIN_USERNAME,
        artifact_callback=record_terminal_artifact,
    )
    app.state.devices = devices
    for session in tuple(app.state.terminals.sessions.values()):
        if not session.owner:
            continue
        try:
            await bind_terminal_workspace(app, session)
        except WorkspaceError:
            # A stale device or root must not prevent terminal recovery. The
            # workspace endpoint will return the precise configuration error.
            pass
    app.state.artifact_ingest_task = asyncio.create_task(artifact_ingest_worker())
    app.state.previews = PreviewManager(
        idle_timeout=float(os.getenv("PREVIEW_IDLE_TIMEOUT", "1800"))
    )
    app.state.previews.start()
    app.state.service_monitor_task = asyncio.create_task(service_monitor_loop(app))
    dashboard_url = os.getenv("FRPS_DASHBOARD_URL", "").strip()
    app.state.frp_monitor = None
    app.state.frp_monitor_task = None
    if dashboard_url:
        monitor = FrpMonitor(
            devices,
            dashboard_url,
            os.getenv("FRPS_DASHBOARD_USER", ""),
            os.getenv("FRPS_DASHBOARD_PASSWORD", ""),
            interval=float(os.getenv("FRPS_SYNC_INTERVAL", "15")),
            proxy_host=os.getenv("FRP_PROXY_HOST", "127.0.0.1"),
            auto_discover=os.getenv("FRPS_AUTO_DISCOVER", "1") == "1",
        )
        app.state.frp_monitor = monitor
        try:
            await monitor.sync_once()
        except Exception:
            pass
        app.state.frp_monitor_task = asyncio.create_task(monitor.run())
    yield
    if app.state.frp_monitor_task:
        app.state.frp_monitor_task.cancel()
        await asyncio.gather(app.state.frp_monitor_task, return_exceptions=True)
    app.state.service_monitor_task.cancel()
    await asyncio.gather(app.state.service_monitor_task, return_exceptions=True)
    await app.state.previews.close()
    await asyncio.to_thread(app.state.workspaces.close)
    await app.state.terminals.close()
    try:
        await asyncio.wait_for(artifact_ingest_queue.join(), timeout=2)
    except asyncio.TimeoutError:
        pass
    app.state.artifact_ingest_task.cancel()
    await asyncio.gather(app.state.artifact_ingest_task, return_exceptions=True)


app = FastAPI(title="AgentServer Terminal", lifespan=lifespan)
preview_origin = os.getenv("PREVIEW_PUBLIC_ORIGIN", "").strip()
if preview_origin:
    preview_hostname = urlsplit(preview_origin).hostname
    if not preview_hostname:
        raise RuntimeError("PREVIEW_PUBLIC_ORIGIN must be an http(s) origin")
    app.add_middleware(PreviewHostMiddleware, base_domain=preview_hostname)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class CreateTerminalBody(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    cols: int = Field(default=120, ge=2, le=500)
    rows: int = Field(default=32, ge=1, le=300)
    workspace_root: str | None = Field(default=None, max_length=2048)


class CreatePreviewBody(BaseModel):
    port: int = Field(ge=1, le=65535)
    label: str | None = Field(default=None, max_length=80)
    terminal_id: str | None = Field(default=None, max_length=80)


class ArtifactBody(BaseModel):
    type: str = Field(default="created", min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(default="", max_length=255)
    media_type: str | None = Field(default=None, max_length=255)
    size: int | None = Field(default=None, ge=0)
    kind: str = Field(default="file", min_length=1, max_length=40)
    version: str = Field(default="", max_length=255)
    source: str = Field(default="agent-api", min_length=1, max_length=80)


class ReadImageBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class ResolveFileBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class DeviceCreateBody(BaseModel):
    id: str | None = Field(default=None, min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    proxy_name: str = Field(min_length=1, max_length=160)
    remote_port: int = Field(ge=1, le=65535)
    ssh_user: str = Field(default="root", min_length=1, max_length=80)
    remote_shell: Literal["system", "powershell", "cmd"] = "system"
    notes: str = Field(default="", max_length=1000)


class DeviceUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    proxy_name: str | None = Field(default=None, min_length=1, max_length=160)
    remote_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, min_length=1, max_length=80)
    remote_shell: Literal["system", "powershell", "cmd"] | None = None
    notes: str | None = Field(default=None, max_length=1000)


def current_user(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> str:
    username = signer.verify(session)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def terminal_manager(request: Request) -> TerminalManager:
    return request.app.state.terminals


def preview_manager(request: Request) -> PreviewManager:
    return request.app.state.previews


def workspace_service(request: Request) -> WorkspaceService:
    return request.app.state.workspaces


async def bind_terminal_workspace(application: FastAPI, session):
    service: WorkspaceService = application.state.workspaces
    try:
        return service.binding(session.owner, session.id)
    except WorkspaceNotFound:
        pass

    try:
        if session.workspace_kind == "local":
            provider = await asyncio.to_thread(
                LocalWorkspaceProvider,
                session.workspace_root or session.cwd,
            )
        elif session.workspace_kind == "sftp":
            if not session.device_id:
                raise WorkspaceConfigurationError("SSH terminal has no device binding")
            device = await asyncio.to_thread(devices.get, session.device_id)
            if not device:
                raise WorkspaceConfigurationError("terminal device no longer exists")
            provider = SftpWorkspaceProvider.from_device(
                session.workspace_root or ".",
                device,
                data_dir=DATA_DIR,
            )
        else:
            raise WorkspaceConfigurationError(
                f"unsupported workspace provider: {session.workspace_kind}"
            )
    except WorkspaceError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise WorkspaceConfigurationError(
            f"workspace provider configuration failed: {error}"
        ) from error
    try:
        return await asyncio.to_thread(service.bind, session.owner, session.id, provider)
    except BaseException:
        await asyncio.to_thread(provider.close)
        raise


def workspace_http_error(error: WorkspaceError) -> HTTPException:
    headers: dict[str, str] | None = None
    if isinstance(error, (WorkspaceNotFound, WorkspaceGrantNotFound)):
        status_code = 404
    elif isinstance(error, WorkspaceAccessDenied):
        status_code = 403
    elif isinstance(error, WorkspaceInvalidRange):
        status_code = 416
        if error.total is not None:
            headers = {"Content-Range": f"bytes */{error.total}"}
    elif isinstance(error, (WorkspaceNotDirectory, WorkspaceNotFile)):
        status_code = 422
    elif isinstance(error, WorkspaceTooLarge):
        status_code = 413
    elif isinstance(error, WorkspaceFileChanged):
        status_code = 409
    elif isinstance(error, (WorkspaceConfigurationError, WorkspaceUnavailable)):
        status_code = 503
    else:
        status_code = 500
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": str(error)},
        headers=headers,
    )


def file_grant_payload(grant: FileGrant) -> dict[str, object]:
    return {
        "id": grant.id,
        "terminal_id": grant.terminal_id,
        "path": grant.path,
        "name": grant.name,
        "media_type": grant.media_type,
        "size": grant.size,
        "kind": "file",
        "version": grant.etag,
        "etag": grant.etag,
        "preview_mode": grant.preview_kind,
        "inline_safe": grant.inline_safe,
        "modified_at": grant.modified_at,
        "expires_at": grant.expires_at,
        "image_width": grant.image_width,
        "image_height": grant.image_height,
    }


@app.get("/api/health")
async def health(request: Request) -> dict[str, object]:
    monitor: FrpMonitor | None = request.app.state.frp_monitor
    artifact_queue = getattr(request.app.state, "artifact_ingest_queue", None)
    artifact_stats = getattr(request.app.state, "artifact_ingest_stats", {})
    return {
        "status": "ok",
        "frp": monitor.status() if monitor else {"configured": False},
        "artifacts": {
            "queued": artifact_queue.qsize() if artifact_queue else 0,
            "dropped": int(artifact_stats.get("dropped", 0)),
            "failed": int(artifact_stats.get("failed", 0)),
        },
    }


@app.post("/api/auth/login")
async def login(body: LoginBody, response: Response) -> dict[str, str]:
    if not users.authenticate(body.username, body.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    response.set_cookie(
        COOKIE_NAME,
        signer.issue(body.username),
        max_age=signer.max_age,
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "0") == "1",
        path="/",
    )
    return {"username": body.username}


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(username: str = Depends(current_user)) -> dict[str, str]:
    return {"username": username}


@app.get("/api/version")
async def version() -> dict[str, str]:
    return {"build_sha": BUILD_SHA}


@app.post("/api/auth/password")
async def change_password(
    body: PasswordBody, username: str = Depends(current_user)
) -> dict[str, bool]:
    if not users.authenticate(username, body.current_password):
        raise HTTPException(status_code=400, detail="当前密码错误")
    users.update_password(username, body.new_password)
    return {"ok": True}


@app.get("/downloads/{filename}", include_in_schema=False)
async def download_client_file(
    filename: str,
    _username: str = Depends(current_user),
) -> FileResponse:
    download = DOWNLOAD_FILES.get(filename)
    if not download or not download[0].is_file():
        raise HTTPException(status_code=404, detail="下载文件不存在")
    path, media_type = download
    return FileResponse(path, filename=filename, media_type=media_type)


@app.get("/api/terminals")
async def list_terminals(
    manager: TerminalManager = Depends(terminal_manager),
    username: str = Depends(current_user),
) -> list[dict[str, object]]:
    return manager.list(username)


@app.get("/api/devices")
async def list_devices(
    _username: str = Depends(current_user),
) -> list[dict[str, object]]:
    return await asyncio.to_thread(devices.list)


@app.post("/api/devices", status_code=201)
async def create_device(
    body: DeviceCreateBody,
    _username: str = Depends(current_user),
) -> dict[str, object]:
    device_id = body.id or re.sub(r"[^A-Za-z0-9_.-]+", "-", body.name).strip("-.")
    if not device_id or not DEVICE_ID.fullmatch(device_id):
        raise HTTPException(
            status_code=422,
            detail="设备 ID 需为 2-64 位字母、数字、点、下划线或连字符",
        )
    try:
        return await asyncio.to_thread(
            devices.create,
            device_id=device_id,
            name=body.name.strip(),
            proxy_name=body.proxy_name.strip(),
            remote_port=body.remote_port,
            ssh_user=body.ssh_user.strip(),
            remote_shell=body.remote_shell,
            notes=body.notes.strip(),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="设备 ID、代理名称或远端端口已存在"
        ) from exc


@app.put("/api/devices/{device_id}")
async def update_device(
    device_id: str,
    body: DeviceUpdateBody,
    _username: str = Depends(current_user),
) -> dict[str, object]:
    values = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in body.model_dump(exclude_none=True).items()
    }
    try:
        result = await asyncio.to_thread(devices.update, device_id, values)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="代理名称或远端端口已被其他设备使用"
        ) from exc
    if not result:
        raise HTTPException(status_code=404, detail="设备不存在")
    return result


@app.delete("/api/devices/{device_id}")
async def delete_device(
    device_id: str,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    _username: str = Depends(current_user),
) -> dict[str, bool]:
    for session in tuple(manager.sessions.values()):
        if session.device_id == device_id:
            await manager.delete(session.id)
    await request.app.state.previews.delete_for_device(device_id)
    if not await asyncio.to_thread(devices.delete, device_id):
        raise HTTPException(status_code=404, detail="设备不存在")
    return {"ok": True}


@app.post("/api/devices/sync")
async def sync_devices(
    request: Request,
    _username: str = Depends(current_user),
) -> dict[str, object]:
    monitor: FrpMonitor | None = request.app.state.frp_monitor
    if not monitor:
        raise HTTPException(status_code=409, detail="尚未配置 FRP Dashboard")
    try:
        return await monitor.sync_once()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FRP 同步失败：{exc}") from exc


@app.post("/api/devices/{device_id}/probe")
async def probe_device(
    device_id: str,
    _username: str = Depends(current_user),
) -> dict[str, object]:
    device = await asyncio.to_thread(devices.get, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    host = os.getenv("FRP_PROXY_HOST", "127.0.0.1")
    available, error = await asyncio.to_thread(
        probe_ssh, host, int(device["remote_port"])
    )
    await asyncio.to_thread(devices.update_probe, device_id, available, error)
    return {"available": available, "error": error}


def ssh_base_command(device: dict[str, object]) -> list[str]:
    private_key = Path(os.getenv("SSH_PRIVATE_KEY", "")).expanduser()
    if not str(private_key) or not private_key.is_file():
        raise HTTPException(status_code=409, detail="服务器尚未配置 SSH_PRIVATE_KEY")
    known_hosts = Path(
        os.getenv("SSH_KNOWN_HOSTS", str(DATA_DIR / "ssh_known_hosts"))
    ).expanduser()
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(mode=0o600, exist_ok=True)
    known_hosts.chmod(0o600)
    strict_mode = os.getenv("SSH_STRICT_HOST_KEY", "accept-new")
    proxy_host = os.getenv("FRP_PROXY_HOST", "127.0.0.1")
    return [
        os.getenv("SSH_BINARY", "ssh"),
        "-p",
        str(device["remote_port"]),
        "-i",
        str(private_key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"StrictHostKeyChecking={strict_mode}",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        f"{device['ssh_user']}@{proxy_host}",
    ]


def ssh_command(device: dict[str, object]) -> list[str]:
    command = ssh_base_command(device)
    command.insert(1, "-tt")
    command.extend(remote_shell_command(str(device.get("remote_shell") or "system")))
    return command


def preview_tunnel_command(
    device: dict[str, object], target_port: int, local_port: int
) -> list[str]:
    command = ssh_base_command(device)
    # A preview tunnel never allocates a remote shell. It only exposes one
    # loopback service through the already authenticated device SSH route.
    command[1:1] = [
        "-N",
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "LogLevel=ERROR",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{target_port}",
    ]
    return command


def listener_scan_command(device: dict[str, object]) -> list[str]:
    """Build a read-only remote command that reports TCP listening processes."""
    command = ssh_base_command(device)
    remote_shell = str(device.get("remote_shell") or "system")
    if remote_shell in {"powershell", "cmd"}:
        script = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { "
            f"Write-Output '{LISTENER_SCAN_MARKER}:unsupported'; exit 0 }}; "
            f"Write-Output '{LISTENER_SCAN_MARKER}:records'; "
            "Get-NetTCPConnection -State Listen -ErrorAction Stop | ForEach-Object { "
            "$p=Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; "
            "Write-Output ('__AGENTSERVER_LISTENER__|{0}|{1}|{2}' -f "
            "$_.LocalPort,$_.OwningProcess,$p.ProcessName) }"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command.append(
            f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"
        )
        return command
    script = (
        "if command -v ss >/dev/null 2>&1; then "
        f"printf '{LISTENER_SCAN_MARKER}:ss\\n'; ss -H -ltnp 2>/dev/null || ss -H -ltn 2>/dev/null; "
        "elif command -v lsof >/dev/null 2>&1; then "
        f"printf '{LISTENER_SCAN_MARKER}:lsof\\n'; lsof -nP -iTCP -sTCP:LISTEN -Fpcn 2>/dev/null; "
        "elif command -v netstat >/dev/null 2>&1; then "
        f"printf '{LISTENER_SCAN_MARKER}:netstat\\n'; netstat -lntp 2>/dev/null || netstat -an 2>/dev/null; "
        f"else printf '{LISTENER_SCAN_MARKER}:unsupported\\n'; fi"
    )
    command.append(f"sh -lc {shlex.quote(script)}")
    return command


async def scan_device_listeners(
    device: dict[str, object],
) -> tuple[list[ListeningProcess] | None, str]:
    """Read one remote listener snapshot; None means the snapshot is unusable."""
    timeout = max(1.0, float(os.getenv("SERVICE_PROCESS_SCAN_TIMEOUT", "5")))
    process = await asyncio.create_subprocess_exec(
        *listener_scan_command(device),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return None, "远端监听端口扫描超时"
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.CancelledError):
                await process.wait()
    output = stdout.decode(errors="replace")
    detail = stderr.decode(errors="replace").strip()
    if process.returncode != 0:
        return None, detail or f"远端监听端口扫描退出 ({process.returncode})"
    marker = re.search(rf"^{re.escape(LISTENER_SCAN_MARKER)}:([^\r\n]+)", output, re.MULTILINE)
    if not marker or marker.group(1).strip().lower() == "unsupported":
        return None, "远端缺少 ss、lsof 或 netstat"
    minimum_port = max(1, int(os.getenv("SERVICE_PROCESS_MIN_PORT", "1024")))
    listeners = [
        listener for listener in parse_listener_scan(output) if listener.port >= minimum_port
    ]
    return listeners, ""


def reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def probe_device_service(
    device: dict[str, object], target_port: int, scheme: str, *, timeout: float | None = None
) -> tuple[bool, str]:
    """Verify a discovered HTTP service through a short-lived SSH forward."""
    local_port = reserve_loopback_port()
    process = await asyncio.create_subprocess_exec(
        *preview_tunnel_command(device, target_port, local_port),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    deadline = asyncio.get_running_loop().time() + (
        timeout if timeout is not None else float(os.getenv("SERVICE_PROBE_TIMEOUT", "6"))
    )
    error = "服务端口当前不可访问"
    try:
        async with httpx.AsyncClient(
            verify=False,
            follow_redirects=False,
            timeout=httpx.Timeout(1.5),
        ) as client:
            while asyncio.get_running_loop().time() < deadline:
                if process.returncode is not None:
                    detail = (await process.stderr.read()).decode(errors="replace").strip()
                    return False, detail or f"SSH 探测已退出 ({process.returncode})"
                try:
                    async with client.stream(
                        "GET",
                        f"{scheme}://127.0.0.1:{local_port}/",
                        headers={"host": f"localhost:{target_port}"},
                    ):
                        return True, ""
                except (httpx.HTTPError, OSError) as exc:
                    error = str(exc)
                    await asyncio.sleep(0.25)
        return False, error
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


async def service_monitor_loop(app: FastAPI) -> None:
    interval = max(2.0, float(os.getenv("SERVICE_PROBE_INTERVAL", "10")))
    process_interval = max(
        interval, float(os.getenv("SERVICE_PROCESS_SCAN_INTERVAL", "10"))
    )
    process_missing_threshold = max(
        1, int(os.getenv("SERVICE_PROCESS_MISSING_SCANS", "2"))
    )
    threshold = max(1, int(os.getenv("SERVICE_PROBE_FAILURES", "2")))
    semaphore = asyncio.Semaphore(max(1, int(os.getenv("SERVICE_PROBE_CONCURRENCY", "3"))))
    scan_semaphore = asyncio.Semaphore(
        max(1, int(os.getenv("SERVICE_PROCESS_SCAN_CONCURRENCY", "3")))
    )
    discovery_event = app.state.terminals.service_discovery_event
    next_process_scan = 0.0

    async def check(
        session_id: str, device_id: str, port: int, url: str, source: str
    ) -> None:
        async with semaphore:
            try:
                device = await asyncio.to_thread(devices.get, device_id)
                if not device or not device["frp_online"] or not device["ssh_available"]:
                    online, error = False, "设备 SSH 当前不可用"
                else:
                    scheme = "https" if url.lower().startswith("https://") else "http"
                    probe_timeout = (
                        max(1.0, float(os.getenv("SERVICE_PROCESS_PROBE_TIMEOUT", "2")))
                        if source == "process"
                        else None
                    )
                    online, error = await probe_device_service(
                        device, port, scheme, timeout=probe_timeout
                    )
                    if not online and source == "process" and scheme == "http":
                        online, error = await probe_device_service(
                            device, port, "https", timeout=probe_timeout
                        )
                        if online:
                            session = app.state.terminals.get(session_id)
                            service = session and session.services.get(port)
                            if service:
                                service.url = f"https://localhost:{port}/"
            except Exception as exc:
                # One stale or malformed service must not terminate monitoring
                # for all current and future terminal services.
                online, error = False, str(exc)
            _service, became_offline = app.state.terminals.update_service_status(
                session_id,
                port,
                online=online,
                error=error,
                failure_threshold=threshold,
            )
            if became_offline:
                with contextlib.suppress(Exception):
                    await app.state.previews.delete_for_service(session_id, port)

    while True:
        # New terminal-output candidates should be checked immediately instead
        # of waiting for the next periodic lifecycle probe.
        discovery_event.clear()
        now = asyncio.get_running_loop().time()
        if now >= next_process_scan:
            device_ids = sorted(
                {
                    str(session.device_id)
                    for session in app.state.terminals.sessions.values()
                    if session.active and session.kind == "ssh" and session.device_id
                }
            )

            async def scan(device_id: str) -> tuple[str, list[ListeningProcess] | None]:
                async with scan_semaphore:
                    try:
                        device = await asyncio.to_thread(devices.get, device_id)
                        if not device or not device["frp_online"] or not device["ssh_available"]:
                            return device_id, None
                        listeners, _error = await scan_device_listeners(device)
                        return device_id, listeners
                    except Exception:
                        return device_id, None

            scan_results = await asyncio.gather(
                *(scan(device_id) for device_id in device_ids),
                return_exceptions=False,
            )
            for device_id, listeners in scan_results:
                if listeners is None:
                    continue
                removed = app.state.terminals.sync_process_listeners(
                    device_id,
                    listeners,
                    missing_threshold=process_missing_threshold,
                )
                for session_id, port in removed:
                    with contextlib.suppress(Exception):
                        await app.state.previews.delete_for_service(session_id, port)
            next_process_scan = asyncio.get_running_loop().time() + process_interval
        candidates = [
            (
                session.id,
                str(session.device_id),
                service.port,
                service.url,
                service.source,
            )
            for session, service in app.state.terminals.service_candidates()
        ]
        if candidates:
            await asyncio.gather(
                *(check(*candidate) for candidate in candidates),
                return_exceptions=True,
            )
        try:
            await asyncio.wait_for(discovery_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


@app.post("/api/devices/{device_id}/terminals", status_code=201)
async def create_device_terminal(
    device_id: str,
    body: CreateTerminalBody,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    username: str = Depends(current_user),
) -> dict[str, object]:
    device = await asyncio.to_thread(devices.get, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not device["frp_online"]:
        raise HTTPException(status_code=409, detail="设备 FRP 隧道当前离线")
    if not device["ssh_available"]:
        raise HTTPException(status_code=409, detail="设备 SSH 服务当前不可用")
    try:
        session = manager.create_process(
            name=body.name or str(device["name"]),
            argv=ssh_command(device),
            cols=body.cols,
            rows=body.rows,
            device_id=device_id,
            device_name=str(device["name"]),
            remote_port=int(device["remote_port"]),
            owner=username,
            workspace_root=(body.workspace_root or ".").strip() or ".",
            workspace_platform=(
                "windows"
                if str(device.get("remote_shell") or "system") in {"powershell", "cmd"}
                else "posix"
            ),
        )
        payload = session.as_dict()
        try:
            binding = await bind_terminal_workspace(request.app, session)
            payload["workspace"] = {
                **dict(payload["workspace"]),
                "binding_id": binding.id,
                "available": True,
            }
        except WorkspaceError as error:
            payload["workspace"] = {
                **dict(payload["workspace"]),
                "available": False,
                "error": str(error),
            }
        return payload
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/terminals", status_code=201)
async def create_terminal(
    body: CreateTerminalBody,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    username: str = Depends(current_user),
) -> dict[str, object]:
    if os.getenv("ENABLE_LOCAL_TERMINALS", "1") != "1":
        raise HTTPException(status_code=403, detail="服务器已禁用本地终端")
    try:
        workspace_root = manager.cwd
        if body.workspace_root:
            candidate = Path(body.workspace_root).expanduser()
            if not candidate.is_absolute():
                candidate = Path(manager.cwd) / candidate
            candidate = candidate.resolve()
            try:
                candidate.relative_to(Path(manager.cwd))
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="本地工作区必须位于 TERMINAL_CWD 内",
                ) from exc
            if not candidate.is_dir():
                raise HTTPException(
                    status_code=422,
                    detail="本地工作区目录不存在",
                )
            workspace_root = str(candidate)
        session = manager.create(
            body.name,
            body.cols,
            body.rows,
            owner=username,
            workspace_root=workspace_root,
        )
        payload = session.as_dict()
        try:
            binding = await bind_terminal_workspace(request.app, session)
            payload["workspace"] = {
                **dict(payload["workspace"]),
                "binding_id": binding.id,
                "available": True,
            }
        except WorkspaceError as error:
            payload["workspace"] = {
                **dict(payload["workspace"]),
                "available": False,
                "error": str(error),
            }
        return payload
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/terminals/{session_id}")
async def delete_terminal(
    session_id: str,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    username: str = Depends(current_user),
) -> dict[str, bool]:
    if not manager.get_for_owner(session_id, username):
        raise HTTPException(status_code=404, detail="Terminal not found")
    await request.app.state.previews.delete_for_terminal(session_id)
    await asyncio.to_thread(request.app.state.workspaces.unbind, username, session_id)
    if not await manager.delete(session_id):
        raise HTTPException(status_code=404, detail="Terminal not found")
    request.app.state.artifact_rate_windows.pop(session_id, None)
    return {"ok": True}


@app.get("/api/terminals/{session_id}/workspace")
async def list_workspace(
    session_id: str,
    request: Request,
    path: str = "",
    cursor: str | None = None,
    revision: str | None = None,
    limit: int = 200,
    manager: TerminalManager = Depends(terminal_manager),
    service: WorkspaceService = Depends(workspace_service),
    username: str = Depends(current_user),
) -> dict[str, object]:
    session = manager.get_for_owner(session_id, username)
    if not session:
        raise HTTPException(status_code=404, detail="Terminal not found")
    try:
        binding = await bind_terminal_workspace(request.app, session)
        requested_path = path or "."
        page = await asyncio.to_thread(
            service.list_page,
            username,
            session_id,
            requested_path,
            cursor=cursor,
            limit=limit,
            expected_revision=revision,
        )
        directory = page.directory
        entries = page.entries
        # SFTP canonicalizes a configured root lazily on first I/O.
        binding = await asyncio.to_thread(service.binding, username, session_id)
    except WorkspaceError as error:
        raise workspace_http_error(error) from error

    relative_path = "" if directory.path == "." else directory.path
    parts = [part for part in relative_path.split("/") if part]
    breadcrumbs: list[dict[str, str]] = [{"name": "工作区", "path": ""}]
    for index, part in enumerate(parts):
        breadcrumbs.append(
            {"name": part, "path": "/".join(parts[: index + 1])}
        )
    parent_path = "/".join(parts[:-1]) if parts else None
    return {
        "path": relative_path,
        "workspace_id": binding.id,
        "root": binding.root,
        "provider": binding.provider_kind,
        "platform": session.workspace_platform,
        "current_path": session.workspace_current_path,
        "parent": parent_path,
        "parent_path": parent_path,
        "breadcrumbs": breadcrumbs,
        "revision": page.revision,
        "next_cursor": page.next_cursor,
        "truncated": page.next_cursor is not None,
        "capabilities": {
            "read": True,
            "write": False,
            "watch": True,
            "pagination": True,
        },
        "entries": [
            {
                "name": entry.name,
                "path": "" if entry.path == "." else entry.path,
                "kind": entry.kind,
                "size": entry.size,
                "modified_at": entry.modified_at,
                "version": entry.etag,
                "hidden": entry.name.startswith("."),
                "readonly": True,
            }
            for entry in entries
        ],
    }


@app.post("/api/terminals/{session_id}/files/resolve")
async def resolve_workspace_file(
    session_id: str,
    body: ResolveFileBody,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    service: WorkspaceService = Depends(workspace_service),
    username: str = Depends(current_user),
) -> dict[str, object]:
    session = manager.get_for_owner(session_id, username)
    if not session:
        raise HTTPException(status_code=404, detail="Terminal not found")
    try:
        await bind_terminal_workspace(request.app, session)
        grant = await asyncio.to_thread(
            service.grant, username, session_id, body.path
        )
    except WorkspaceError as error:
        raise workspace_http_error(error) from error
    return file_grant_payload(grant)


@app.get("/api/files/{grant_id}/content")
async def read_workspace_file(
    grant_id: str,
    terminal_id: str,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    service: WorkspaceService = Depends(workspace_service),
    username: str = Depends(current_user),
) -> Response:
    session = manager.get_for_owner(terminal_id, username)
    if not session:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        await bind_terminal_workspace(request.app, session)
        result = await asyncio.to_thread(
            service.read,
            grant_id,
            username,
            terminal_id,
            range_header=request.headers.get("range"),
            if_none_match=request.headers.get("if-none-match"),
        )
    except WorkspaceError as error:
        raise workspace_http_error(error) from error
    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=result.headers,
    )


@app.post("/api/terminals/{session_id}/read-image")
async def read_image_tool(
    session_id: str,
    body: ReadImageBody,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    service: WorkspaceService = Depends(workspace_service),
    username: str = Depends(current_user),
) -> dict[str, object]:
    session = manager.get_for_owner(session_id, username)
    if not session:
        raise HTTPException(status_code=404, detail="Terminal not found")
    try:
        await bind_terminal_workspace(request.app, session)
        grant = await asyncio.to_thread(
            service.grant, username, session_id, body.path
        )
        if not grant.media_type.startswith("image/"):
            raise WorkspaceNotFile("read_image only accepts raster image files")
        if grant.size > request.app.state.attachments.max_image_bytes:
            raise WorkspaceTooLarge(
                "image exceeds the durable attachment byte limit"
            )
        resolved = await asyncio.to_thread(
            service.read, grant.id, username, session_id
        )
    except WorkspaceError as error:
        raise workspace_http_error(error) from error
    try:
        attachment = await asyncio.to_thread(
            request.app.state.attachments.save_image,
            resolved.body,
            declared_media_type=grant.media_type,
            name=grant.name,
        )
    except ImageValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ImageSupportUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    file_ref = ArtifactFileRef(
        path=grant.path,
        name=grant.name,
        media_type=attachment.media_type,
        size=attachment.size,
    )
    event = await asyncio.to_thread(
        request.app.state.artifacts.append,
        owner=username,
        terminal_id=session_id,
        event_type="read_image",
        file=file_ref,
        source="read-image-tool",
        version=grant.etag,
        attachment=attachment,
    )
    attachment_url = (
        f"/api/terminals/{quote(session_id, safe='')}/attachments/"
        f"{quote(attachment.id, safe='')}"
    )
    content = build_read_image_result(file_ref, attachment)
    content[1]["url"] = attachment_url
    model_payload = await asyncio.to_thread(
        request.app.state.attachments.read_authorized,
        request.app.state.artifacts,
        owner=username,
        terminal_id=session_id,
        attachment_id=attachment.id,
    )
    return {
        "event": event.as_dict(),
        "file": file_ref.as_dict(),
        "attachment": {**attachment.as_dict(), "url": attachment_url},
        "content": content,
        "model_content_format": "openai-responses",
        "model_content": build_openai_responses_image_content(
            file_ref, model_payload
        ),
    }


@app.get("/api/terminals/{session_id}/artifacts")
async def list_artifacts(
    session_id: str,
    request: Request,
    after_sequence: int = 0,
    manager: TerminalManager = Depends(terminal_manager),
    username: str = Depends(current_user),
) -> list[dict[str, object]]:
    if not manager.get_for_owner(session_id, username):
        raise HTTPException(status_code=404, detail="Terminal not found")
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence must not be negative")
    if after_sequence:
        events = await asyncio.to_thread(
            request.app.state.artifacts.snapshot,
            owner=username,
            terminal_id=session_id,
            after_sequence=after_sequence,
            limit=500,
        )
    else:
        events = await asyncio.to_thread(
            request.app.state.artifacts.recent,
            owner=username,
            terminal_id=session_id,
            limit=500,
        )
    return [event.as_dict() for event in events]


@app.post("/api/terminals/{session_id}/artifacts", status_code=201)
async def create_artifact(
    session_id: str,
    body: ArtifactBody,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    username: str = Depends(current_user),
) -> dict[str, object]:
    if not manager.get_for_owner(session_id, username):
        raise HTTPException(status_code=404, detail="Terminal not found")
    event = await asyncio.to_thread(
        request.app.state.artifacts.append,
        owner=username,
        terminal_id=session_id,
        event_type=body.type,
        file=ArtifactFileRef(
            path=body.path,
            name=body.name,
            media_type=body.media_type,
            size=body.size,
            kind=body.kind,
        ),
        source=body.source,
        version=body.version,
    )
    return event.as_dict()


@app.get("/api/terminals/{session_id}/attachments/{attachment_id:path}")
async def read_attachment(
    session_id: str,
    attachment_id: str,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    username: str = Depends(current_user),
) -> Response:
    if not manager.get_for_owner(session_id, username):
        raise HTTPException(status_code=404, detail="Attachment not found")
    try:
        payload = await asyncio.to_thread(
            request.app.state.attachments.read_authorized,
            request.app.state.artifacts,
            owner=username,
            terminal_id=session_id,
            attachment_id=attachment_id,
        )
    except (AttachmentAccessDenied, AttachmentIntegrityError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    digest = payload.ref.id.removeprefix("sha256:")
    filename = payload.ref.name or f"image-{digest[:12]}"
    return Response(
        content=payload.data,
        media_type=payload.ref.media_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
            "ETag": f'"{digest}"',
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )


def preview_api_payload(preview) -> dict[str, object]:
    payload = preview.as_dict()
    payload["url"] = (
        preview_public_url(preview.id, preview_origin) if preview_origin else None
    )
    return payload


@app.get("/api/previews")
async def list_previews(
    manager: PreviewManager = Depends(preview_manager),
    _username: str = Depends(current_user),
) -> list[dict[str, object]]:
    return [preview_api_payload(item) for item in manager.sessions.values()]


@app.post("/api/devices/{device_id}/previews", status_code=201)
async def create_preview(
    device_id: str,
    body: CreatePreviewBody,
    manager: PreviewManager = Depends(preview_manager),
    terminal_manager: TerminalManager = Depends(terminal_manager),
    username: str = Depends(current_user),
) -> dict[str, object]:
    if not preview_origin:
        raise HTTPException(
            status_code=503,
            detail="服务器尚未配置 PREVIEW_PUBLIC_ORIGIN",
        )
    device = await asyncio.to_thread(devices.get, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not device["frp_online"] or not device["ssh_available"]:
        raise HTTPException(status_code=409, detail="设备 SSH 当前不可用")
    if body.terminal_id:
        terminal = terminal_manager.get_for_owner(body.terminal_id, username)
        if not terminal or terminal.device_id != device_id:
            raise HTTPException(status_code=422, detail="终端不属于所选设备")
        detected = terminal.services.get(body.port)
        if detected and detected.status == "offline":
            raise HTTPException(status_code=409, detail="检测到该开发服务已经停止")
        existing = manager.find_for_service(body.terminal_id, body.port)
        if existing:
            return preview_api_payload(existing)
    try:
        preview = await manager.create(
            device_id=device_id,
            device_name=str(device["name"]),
            target_port=body.port,
            label=(body.label or "").strip() or f"localhost:{body.port}",
            terminal_id=body.terminal_id,
            tunnel_command=lambda local_port: preview_tunnel_command(
                device, body.port, local_port
            ),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"无法建立预览隧道：{exc}") from exc
    return preview_api_payload(preview)


@app.post("/api/previews/{preview_id}/ticket")
async def create_preview_ticket(
    preview_id: str,
    manager: PreviewManager = Depends(preview_manager),
    username: str = Depends(current_user),
) -> dict[str, str]:
    preview = manager.get(preview_id)
    if not preview:
        raise HTTPException(status_code=404, detail="预览不存在或已过期")
    url = preview_public_url(preview.id, preview_origin)
    ticket = preview_signer.issue(f"ticket:{preview.id}:{username}")
    return {"url": f"{url}_agentserver/auth?ticket={ticket}"}


@app.delete("/api/previews/{preview_id}")
async def delete_preview(
    preview_id: str,
    manager: PreviewManager = Depends(preview_manager),
    _username: str = Depends(current_user),
) -> dict[str, bool]:
    if not await manager.delete(preview_id):
        raise HTTPException(status_code=404, detail="预览不存在")
    return {"ok": True}


def preview_access_allowed(preview_id: str, token: str | None) -> bool:
    subject = preview_access_signer.verify(token)
    return bool(subject and subject.startswith(f"access:{preview_id}:"))


def preview_host_allowed(preview_id: str, host: str) -> bool:
    return bool(
        preview_origin
        and preview_id_from_host(host, urlsplit(preview_origin).hostname or "")
        == preview_id
    )


@app.get("/preview/{preview_id}/_agentserver/auth", include_in_schema=False)
async def authorize_preview(
    preview_id: str,
    ticket: str,
    request: Request,
    manager: PreviewManager = Depends(preview_manager),
) -> RedirectResponse:
    if not preview_host_allowed(preview_id, request.headers.get("host", "")):
        raise HTTPException(status_code=404, detail="预览入口不存在")
    preview = manager.get(preview_id, touch=True)
    subject = preview_signer.verify(ticket)
    expected_prefix = f"ticket:{preview_id}:"
    if not preview or not subject or not subject.startswith(expected_prefix):
        raise HTTPException(status_code=401, detail="预览访问票据无效或已过期")
    username = subject[len(expected_prefix) :]
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        PREVIEW_COOKIE_NAME,
        preview_access_signer.issue(f"access:{preview_id}:{username}"),
        max_age=preview_access_signer.max_age,
        httponly=True,
        samesite="lax",
        secure=urlsplit(preview_origin).scheme == "https",
        path="/",
    )
    return response


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@app.api_route(
    "/preview/{preview_id}/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_preview_http(
    preview_id: str,
    path: str,
    request: Request,
    manager: PreviewManager = Depends(preview_manager),
):
    if not preview_host_allowed(preview_id, request.headers.get("host", "")):
        raise HTTPException(status_code=404, detail="预览入口不存在")
    if not preview_access_allowed(
        preview_id, request.cookies.get(PREVIEW_COOKIE_NAME)
    ):
        raise HTTPException(status_code=401, detail="预览未授权或授权已过期")
    preview = manager.get(preview_id, touch=True)
    if not preview or not preview.active:
        raise HTTPException(status_code=404, detail="预览不存在或隧道已关闭")
    target = f"http://127.0.0.1:{preview.local_port}/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower() not in {"host", "cookie", "content-length"}
    }
    headers["host"] = f"127.0.0.1:{preview.target_port}"
    headers["x-forwarded-host"] = request.headers.get("host", "")
    headers["x-forwarded-proto"] = request.url.scheme
    if cookie := upstream_cookie(request.headers.get("cookie", "")):
        headers["cookie"] = cookie
    local_origin = f"http://127.0.0.1:{preview.target_port}"
    if "origin" in headers:
        headers["origin"] = local_origin
    if "referer" in headers:
        parsed_referer = urlsplit(headers["referer"])
        headers["referer"] = parsed_referer._replace(
            scheme="http", netloc=f"127.0.0.1:{preview.target_port}"
        ).geturl()
    client = httpx.AsyncClient(follow_redirects=False, timeout=None, trust_env=False)
    request_body = await request.body()
    upstream_request = client.build_request(
        request.method,
        target,
        headers=headers,
        content=request_body,
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"设备上的 localhost:{preview.target_port} 当前不可访问：{exc}",
        ) from exc
    response_headers = []
    for key, value in upstream.headers.multi_items():
        lowered = key.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered in {
            "content-length",
            "x-frame-options",
        }:
            continue
        if lowered == "set-cookie":
            value = rewrite_set_cookie(value)
        elif lowered == "content-security-policy":
            value = rewrite_frame_ancestors(value)
            if not value:
                continue
        response_headers.append((key, value))
    location = next(
        (value for key, value in response_headers if key.lower() == "location"), None
    )
    if location:
        parsed_location = urlsplit(location)
        if parsed_location.hostname in {"127.0.0.1", "localhost", "::1"}:
            public = urlsplit(preview_public_url(preview.id, preview_origin))
            rewritten_location = parsed_location._replace(
                scheme=public.scheme,
                netloc=public.netloc,
            ).geturl()
            response_headers = [
                (key, rewritten_location if key.lower() == "location" else value)
                for key, value in response_headers
            ]
    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(_close_preview_response, upstream, client),
    )
    response.raw_headers = [
        (key.encode("latin-1"), value.encode("latin-1"))
        for key, value in response_headers
    ]
    return response


async def _close_preview_response(upstream, client) -> None:
    await upstream.aclose()
    await client.aclose()


@app.websocket("/preview/{preview_id}/{path:path}")
async def proxy_preview_websocket(
    websocket: WebSocket, preview_id: str, path: str
) -> None:
    if not preview_host_allowed(preview_id, websocket.headers.get("host", "")):
        await websocket.close(code=4404)
        return
    if not preview_access_allowed(
        preview_id, websocket.cookies.get(PREVIEW_COOKIE_NAME)
    ):
        await websocket.close(code=4401)
        return
    manager: PreviewManager = websocket.app.state.previews
    preview = manager.get(preview_id, touch=True)
    if not preview or not preview.active:
        await websocket.close(code=4404)
        return
    # Keep the URI authority equal to the device's original development port
    # for Host/Origin checks, while dialing the local SSH-forward socket.
    target = f"ws://127.0.0.1:{preview.target_port}/{path}"
    query = websocket.scope.get("query_string", b"").decode("latin-1")
    if query:
        target = f"{target}?{query}"
    protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    additional_headers = {}
    if cookie := upstream_cookie(websocket.headers.get("cookie", "")):
        additional_headers["cookie"] = cookie
    if authorization := websocket.headers.get("authorization"):
        additional_headers["authorization"] = authorization
    try:
        async with websocket_connect(
            target,
            origin=f"http://127.0.0.1:{preview.target_port}",
            subprotocols=protocols or None,
            additional_headers=additional_headers or None,
            compression=None,
            proxy=None,
            host="127.0.0.1",
            port=preview.local_port,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def browser_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def upstream_to_browser() -> None:
                async for message in upstream:
                    manager.get(preview_id, touch=True)
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = {
                asyncio.create_task(browser_to_upstream()),
                asyncio.create_task(upstream_to_browser()),
            }
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except Exception:
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=1011)


@app.websocket("/ws/events/{session_id}")
async def artifact_socket(websocket: WebSocket, session_id: str) -> None:
    username = signer.verify(websocket.cookies.get(COOKIE_NAME))
    if not username:
        await websocket.close(code=4401)
        return
    manager: TerminalManager = websocket.app.state.terminals
    if not manager.get_for_owner(session_id, username):
        await websocket.close(code=4404)
        return

    subscription = websocket.app.state.artifacts.subscribe(
        owner=username,
        terminal_id=session_id,
        snapshot_limit=500,
    )

    async def send_events() -> None:
        await websocket.send_json([event.as_dict() for event in subscription.snapshot])
        async for event in subscription:
            await websocket.send_json(event.as_dict())

    async def receive_until_disconnect() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))

    try:
        await websocket.accept()
        sender = asyncio.create_task(send_events())
        receiver = asyncio.create_task(receive_until_disconnect())
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    finally:
        await subscription.aclose()


@app.websocket("/ws/workspace/{session_id}")
async def workspace_socket(websocket: WebSocket, session_id: str) -> None:
    """Poll watched root-relative paths and emit bounded invalidations.

    Local and SFTP workspaces share this conservative transport. Directory
    metadata catches child additions/removals while explicitly watched files
    catch in-place content updates. Clients re-list affected nodes through the
    normal owner-scoped workspace API; websocket events never carry contents.
    """

    username = signer.verify(websocket.cookies.get(COOKIE_NAME))
    if not username:
        await websocket.close(code=4401)
        return
    manager: TerminalManager = websocket.app.state.terminals
    session = manager.get_for_owner(session_id, username)
    if not session:
        await websocket.close(code=4404)
        return
    service: WorkspaceService = websocket.app.state.workspaces
    try:
        await bind_terminal_workspace(websocket.app, session)
    except WorkspaceError:
        await websocket.close(code=4410)
        return

    watched_paths: set[str] = {""}
    signatures: dict[str, tuple[str, int, float, str] | tuple[str, str]] = {}
    interval = min(
        30.0,
        max(0.5, float(os.getenv("WORKSPACE_WATCH_INTERVAL", "2"))),
    )

    async def receive_watches() -> None:
        nonlocal watched_paths
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            raw = message.get("text")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("type") != "watch" or not isinstance(payload.get("paths"), list):
                continue
            values = [
                value
                for value in payload["paths"]
                if isinstance(value, str) and len(value) <= 4_096
            ][:64]
            watched_paths = set(values) | {""}
            for stale in tuple(signatures):
                if stale not in watched_paths:
                    signatures.pop(stale, None)

    async def poll_watches() -> None:
        while True:
            changed: list[str] = []
            current_paths = tuple(sorted(watched_paths))
            for path in current_paths:
                try:
                    entry = await asyncio.to_thread(
                        service.stat,
                        username,
                        session_id,
                        path or ".",
                    )
                    signature: tuple[str, int, float, str] | tuple[str, str] = (
                        entry.kind,
                        entry.size,
                        entry.modified_at,
                        entry.etag,
                    )
                except WorkspaceError as error:
                    signature = ("error", error.code)
                previous = signatures.get(path)
                signatures[path] = signature
                if previous is not None and previous != signature:
                    changed.append(path)
            if changed:
                await websocket.send_json({"type": "changed", "paths": changed})
            await asyncio.sleep(interval)

    await websocket.accept()
    await websocket.send_json({"type": "ready", "paths": [""]})
    receiver = asyncio.create_task(receive_watches())
    poller = asyncio.create_task(poll_watches())
    done, pending = await asyncio.wait(
        {receiver, poller}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


@app.websocket("/ws/sessions")
async def sessions_socket(websocket: WebSocket) -> None:
    """Push this owner's session list whenever it actually changes.

    Replaces the browser's periodic /api/terminals poll. That poll cost a tmux
    query per request per open tab regardless of whether anything had changed;
    here the server only speaks when a session or service really transitions.
    """
    username = signer.verify(websocket.cookies.get(COOKIE_NAME))
    if not username:
        await websocket.close(code=4401)
        return

    manager: TerminalManager = websocket.app.state.terminals
    await websocket.accept()
    waiter = manager.subscribe_state()

    async def send_snapshots() -> None:
        await websocket.send_json(manager.list(username))
        while True:
            await waiter.wait()
            # Coalesce a burst of transitions into one push, then clear again so
            # anything that landed during the pause is folded into this snapshot
            # rather than triggering a second, identical one.
            await asyncio.sleep(SESSION_PUSH_DEBOUNCE_SECONDS)
            waiter.clear()
            await websocket.send_json(manager.list(username))

    async def receive_until_disconnect() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))

    sender = asyncio.create_task(send_snapshots())
    receiver = asyncio.create_task(receive_until_disconnect())
    try:
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    finally:
        manager.unsubscribe_state(waiter)


@app.websocket("/ws/terminal/{session_id}")
async def terminal_socket(websocket: WebSocket, session_id: str) -> None:
    username = signer.verify(websocket.cookies.get(COOKIE_NAME))
    if not username:
        await websocket.close(code=4401)
        return

    manager: TerminalManager = websocket.app.state.terminals
    if not manager.get_for_owner(session_id, username):
        await websocket.close(code=4404)
        return

    await websocket.accept()
    snapshot, queue = manager.attach(session_id)
    # Scrollback can reach TERMINAL_SCROLLBACK_BYTES (2 MiB by default). Send it
    # in slices and yield between them so one attach cannot stall the event loop
    # — every other terminal's output is pumped from this same loop.
    for start in range(0, len(snapshot), SNAPSHOT_CHUNK_BYTES):
        await websocket.send_bytes(snapshot[start:start + SNAPSHOT_CHUNK_BYTES])
        await asyncio.sleep(0)
    # xterm may answer control-sequence queries while parsing a scrollback replay.
    # Tell the browser exactly where the replay ends so those generated replies
    # are not mistaken for fresh keyboard input and written back to the PTY.
    await websocket.send_text(SNAPSHOT_COMPLETE_MESSAGE)

    async def send_output() -> None:
        while True:
            payload = await queue.get()
            if payload is STREAM_GAP:
                # This client fell too far behind to be resynced in place. Close
                # so it reconnects and replays a coherent snapshot instead of
                # rendering a stream with a hole in it.
                await websocket.close(code=1011)
                return
            await websocket.send_bytes(payload)

    async def receive_input() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if data := message.get("bytes"):
                manager.write(session_id, data)
            elif (text := message.get("text")) is not None:
                resize = RESIZE_MESSAGE.fullmatch(text)
                if resize:
                    manager.resize(session_id, int(resize.group(1)), int(resize.group(2)))
                else:
                    manager.write(session_id, text.encode("utf-8"))

    sender = asyncio.create_task(send_output())
    receiver = asyncio.create_task(receive_input())
    try:
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    finally:
        manager.detach(session_id, queue)


if os.getenv("ENVIRONMENT") == "production" and BUILD_SHA != "development":
    # Versioned releases always serve their own frontend, even when a legacy
    # WEB_DIST value remains in the host environment.
    FRONTEND_DIST = ROOT / "web_dist"
else:
    FRONTEND_DIST = Path(os.getenv("WEB_DIST", ROOT / "web_dist")).expanduser()
if not FRONTEND_DIST.is_dir():
    FRONTEND_DIST = ROOT / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    verify_release_pair(
        BUILD_SHA,
        FRONTEND_DIST,
        production=(
            os.getenv("ENVIRONMENT") == "production"
            and BUILD_SHA != "development"
        ),
    )
    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        requested = (FRONTEND_DIST / path).resolve()
        if path and requested.is_file() and FRONTEND_DIST in requested.parents:
            # index.html 不带内容哈希，禁止启发式缓存；/assets 下的文件带哈希可缓存
            headers = {"Cache-Control": "no-cache"} if requested.name == "index.html" else None
            return FileResponse(requested, headers=headers)
        return FileResponse(FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})
