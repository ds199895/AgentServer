from __future__ import annotations

import asyncio
import contextlib
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit


TunnelCommand = Callable[[int], Sequence[str]]
ProcessLauncher = Callable[..., Awaitable[asyncio.subprocess.Process]]
PREVIEW_COOKIE_NAME = "agentserver_preview"


def upstream_cookie(cookie_header: str) -> str:
    """Remove the gateway credential before forwarding application cookies."""
    return "; ".join(
        item.strip()
        for item in cookie_header.split(";")
        if item.strip() and not item.strip().startswith(f"{PREVIEW_COOKIE_NAME}=")
    )


def rewrite_set_cookie(value: str) -> str:
    """Keep upstream cookies host-only on this specific preview origin."""
    parts = [part.strip() for part in value.split(";")]
    return "; ".join(
        part for part in parts if not part.lower().startswith("domain=")
    )


def rewrite_frame_ancestors(value: str) -> str:
    """Allow the isolated preview origin to render inside AgentServer."""
    directives = [item.strip() for item in value.split(";") if item.strip()]
    return "; ".join(
        directive
        for directive in directives
        if not directive.lower().startswith("frame-ancestors")
    )


def preview_id_from_host(host: str, base_domain: str) -> str | None:
    """Return the preview id encoded in ``<id>.<base_domain>``."""
    hostname = host.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".").lower()
    suffix = f".{base_domain.rstrip('.').lower()}"
    if not hostname.endswith(suffix):
        return None
    preview_id = hostname[: -len(suffix)]
    if not preview_id or "." in preview_id:
        return None
    try:
        uuid.UUID(preview_id)
    except ValueError:
        return None
    return preview_id


def preview_public_url(preview_id: str, public_origin: str) -> str:
    """Build an isolated public origin for a preview."""
    parsed = urlsplit(public_origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("PREVIEW_PUBLIC_ORIGIN must be an http(s) origin")
    hostname = f"{preview_id}.{parsed.hostname}"
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, "/", "", ""))


@dataclass(eq=False)
class PreviewSession:
    id: str
    device_id: str
    device_name: str
    target_port: int
    local_port: int
    label: str
    process: asyncio.subprocess.Process
    terminal_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_access_at: float = field(default_factory=time.time)
    error: str = ""

    @property
    def active(self) -> bool:
        return self.process.returncode is None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "terminal_id": self.terminal_id,
            "target_port": self.target_port,
            "label": self.label,
            "created_at": self.created_at,
            "last_access_at": self.last_access_at,
            "active": self.active,
            "error": self.error,
        }


class PreviewManager:
    """Own short-lived SSH local-forward processes for development previews."""

    def __init__(
        self,
        idle_timeout: float = 30 * 60,
        launcher: ProcessLauncher = asyncio.create_subprocess_exec,
    ) -> None:
        self.idle_timeout = max(60.0, idle_timeout)
        self.launcher = launcher
        self.sessions: dict[str, PreviewSession] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    @staticmethod
    def _reserve_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    async def _wait_until_ready(
        self, process: asyncio.subprocess.Process, port: int, timeout: float = 10
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise RuntimeError(f"SSH tunnel exited with status {process.returncode}")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise RuntimeError("SSH tunnel did not become ready")

    async def create(
        self,
        *,
        device_id: str,
        device_name: str,
        target_port: int,
        label: str,
        tunnel_command: TunnelCommand,
        terminal_id: str | None = None,
    ) -> PreviewSession:
        local_port = self._reserve_port()
        command = list(tunnel_command(local_port))
        process = await self.launcher(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._wait_until_ready(process, local_port)
        except Exception:
            await self._stop_process(process)
            raise
        preview = PreviewSession(
            id=str(uuid.uuid4()),
            device_id=device_id,
            device_name=device_name,
            target_port=target_port,
            local_port=local_port,
            label=label or f"localhost:{target_port}",
            process=process,
            terminal_id=terminal_id,
        )
        self.sessions[preview.id] = preview
        return preview

    def get(self, preview_id: str, *, touch: bool = False) -> PreviewSession | None:
        preview = self.sessions.get(preview_id)
        if preview and touch:
            preview.last_access_at = time.time()
        return preview

    def list(self) -> list[dict[str, object]]:
        return [
            preview.as_dict()
            for preview in sorted(
                self.sessions.values(), key=lambda item: item.created_at, reverse=True
            )
        ]

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def delete(self, preview_id: str) -> bool:
        preview = self.sessions.pop(preview_id, None)
        if not preview:
            return False
        await self._stop_process(preview.process)
        return True

    async def delete_for_device(self, device_id: str) -> None:
        for preview in tuple(self.sessions.values()):
            if preview.device_id == device_id:
                await self.delete(preview.id)

    async def cleanup_idle(self, now: float | None = None) -> int:
        cutoff = (now or time.time()) - self.idle_timeout
        stale = [
            preview.id
            for preview in self.sessions.values()
            if preview.last_access_at < cutoff or not preview.active
        ]
        for preview_id in stale:
            await self.delete(preview_id)
        return len(stale)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self.idle_timeout / 2))
            await self.cleanup_idle()

    async def close(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
        for preview_id in tuple(self.sessions):
            await self.delete(preview_id)


class PreviewHostMiddleware:
    """Map isolated preview subdomains onto the internal preview routes."""

    def __init__(self, app, base_domain: str) -> None:
        self.app = app
        self.base_domain = base_domain

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in {"http", "websocket"}:
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            host = headers.get(b"host", b"").decode("latin-1")
            preview_id = preview_id_from_host(host, self.base_domain)
            if preview_id:
                path = scope.get("path", "/")
                scope = dict(scope)
                scope["path"] = f"/preview/{preview_id}{path}"
                scope["raw_path"] = scope["path"].encode("utf-8")
        await self.app(scope, receive, send)
