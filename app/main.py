from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sqlite3
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as websocket_connect

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
    RESIZE_MESSAGE,
    SNAPSHOT_COMPLETE_MESSAGE,
    TerminalManager,
    remote_shell_command,
)


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data")).expanduser()
COOKIE_NAME = "agentserver_session"

users = UserStore(DATA_DIR / "agent_server.db")
admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
if len(admin_password) < 8:
    raise RuntimeError("ADMIN_PASSWORD must be explicitly set to at least 8 characters")
users.ensure_user(os.getenv("ADMIN_USERNAME", "admin"), admin_password)
session_secret = load_or_create_secret(DATA_DIR)
signer = SessionSigner(session_secret)
preview_signer = SessionSigner(session_secret, max_age=120)
preview_access_signer = SessionSigner(session_secret, max_age=24 * 60 * 60)
devices = DeviceStore(DATA_DIR / "agent_server.db")


DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
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
    )
    app.state.devices = devices
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
    await app.state.terminals.close()


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


class CreatePreviewBody(BaseModel):
    port: int = Field(ge=1, le=65535)
    label: str | None = Field(default=None, max_length=80)
    terminal_id: str | None = Field(default=None, max_length=80)


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


@app.get("/api/health")
async def health(request: Request) -> dict[str, object]:
    monitor: FrpMonitor | None = request.app.state.frp_monitor
    return {
        "status": "ok",
        "frp": monitor.status() if monitor else {"configured": False},
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
    _username: str = Depends(current_user),
) -> list[dict[str, object]]:
    return manager.list()


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


def reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def probe_device_service(
    device: dict[str, object], target_port: int, scheme: str
) -> tuple[bool, str]:
    """Verify a discovered HTTP service through a short-lived SSH forward."""
    local_port = reserve_loopback_port()
    process = await asyncio.create_subprocess_exec(
        *preview_tunnel_command(device, target_port, local_port),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    deadline = asyncio.get_running_loop().time() + float(
        os.getenv("SERVICE_PROBE_TIMEOUT", "6")
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
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def service_monitor_loop(app: FastAPI) -> None:
    interval = max(2.0, float(os.getenv("SERVICE_PROBE_INTERVAL", "10")))
    threshold = max(1, int(os.getenv("SERVICE_PROBE_FAILURES", "2")))
    semaphore = asyncio.Semaphore(max(1, int(os.getenv("SERVICE_PROBE_CONCURRENCY", "3"))))

    async def check(session_id: str, device_id: str, port: int, url: str) -> None:
        async with semaphore:
            device = await asyncio.to_thread(devices.get, device_id)
            if not device or not device["frp_online"] or not device["ssh_available"]:
                online, error = False, "设备 SSH 当前不可用"
            else:
                try:
                    online, error = await probe_device_service(
                        device, port, "https" if url.lower().startswith("https://") else "http"
                    )
                except (OSError, RuntimeError, ValueError, HTTPException) as exc:
                    online, error = False, str(exc)
            _service, became_offline = app.state.terminals.update_service_status(
                session_id,
                port,
                online=online,
                error=error,
                failure_threshold=threshold,
            )
            if became_offline:
                await app.state.previews.delete_for_service(session_id, port)

    while True:
        candidates = [
            (session.id, str(session.device_id), service.port, service.url)
            for session, service in app.state.terminals.service_candidates()
        ]
        if candidates:
            await asyncio.gather(*(check(*candidate) for candidate in candidates))
        await asyncio.sleep(interval)


@app.post("/api/devices/{device_id}/terminals", status_code=201)
async def create_device_terminal(
    device_id: str,
    body: CreateTerminalBody,
    manager: TerminalManager = Depends(terminal_manager),
    _username: str = Depends(current_user),
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
        )
        return session.as_dict()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/terminals", status_code=201)
async def create_terminal(
    body: CreateTerminalBody,
    manager: TerminalManager = Depends(terminal_manager),
    _username: str = Depends(current_user),
) -> dict[str, object]:
    if os.getenv("ENABLE_LOCAL_TERMINALS", "1") != "1":
        raise HTTPException(status_code=403, detail="服务器已禁用本地终端")
    try:
        return manager.create(body.name, body.cols, body.rows).as_dict()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/terminals/{session_id}")
async def delete_terminal(
    session_id: str,
    request: Request,
    manager: TerminalManager = Depends(terminal_manager),
    _username: str = Depends(current_user),
) -> dict[str, bool]:
    await request.app.state.previews.delete_for_terminal(session_id)
    if not await manager.delete(session_id):
        raise HTTPException(status_code=404, detail="Terminal not found")
    return {"ok": True}


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
    _username: str = Depends(current_user),
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
        terminal = terminal_manager.get(body.terminal_id)
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


@app.websocket("/ws/terminal/{session_id}")
async def terminal_socket(websocket: WebSocket, session_id: str) -> None:
    username = signer.verify(websocket.cookies.get(COOKIE_NAME))
    if not username:
        await websocket.close(code=4401)
        return

    manager: TerminalManager = websocket.app.state.terminals
    if not manager.get(session_id):
        await websocket.close(code=4404)
        return

    await websocket.accept()
    snapshot, queue = manager.attach(session_id)
    if snapshot:
        await websocket.send_bytes(snapshot)
    # xterm may answer control-sequence queries while parsing a scrollback replay.
    # Tell the browser exactly where the replay ends so those generated replies
    # are not mistaken for fresh keyboard input and written back to the PTY.
    await websocket.send_text(SNAPSHOT_COMPLETE_MESSAGE)

    async def send_output() -> None:
        while True:
            await websocket.send_bytes(await queue.get())

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


FRONTEND_DIST = Path(os.getenv("WEB_DIST", ROOT / "web_dist")).expanduser()
if not FRONTEND_DIST.is_dir():
    FRONTEND_DIST = ROOT / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        requested = (FRONTEND_DIST / path).resolve()
        if path and requested.is_file() and FRONTEND_DIST in requested.parents:
            return FileResponse(requested)
        return FileResponse(FRONTEND_DIST / "index.html")
