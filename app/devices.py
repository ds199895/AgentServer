from __future__ import annotations

import asyncio
import base64
import json
import socket
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FrpProxy:
    name: str
    remote_port: int
    online: bool
    client_version: str = ""
    last_start_time: str = ""
    last_close_time: str = ""
    client_id: str = ""
    ssh_user: str = "root"


class DeviceStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    proxy_name TEXT NOT NULL UNIQUE,
                    remote_port INTEGER NOT NULL UNIQUE,
                    ssh_user TEXT NOT NULL DEFAULT 'root',
                    remote_shell TEXT NOT NULL DEFAULT 'system',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_seen_at INTEGER,
                    frp_online INTEGER NOT NULL DEFAULT 0,
                    ssh_available INTEGER NOT NULL DEFAULT 0,
                    client_version TEXT NOT NULL DEFAULT '',
                    last_start_time TEXT NOT NULL DEFAULT '',
                    last_close_time TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    discovered INTEGER NOT NULL DEFAULT 0,
                    client_id TEXT NOT NULL DEFAULT '',
                    hostname TEXT NOT NULL DEFAULT '',
                    client_ip TEXT NOT NULL DEFAULT '',
                    wire_protocol TEXT NOT NULL DEFAULT '',
                    first_connected_at INTEGER
                )
                """
            )
            existing = {
                row[1] for row in connection.execute("PRAGMA table_info(devices)").fetchall()
            }
            migrations = {
                "client_id": "TEXT NOT NULL DEFAULT ''",
                "hostname": "TEXT NOT NULL DEFAULT ''",
                "client_ip": "TEXT NOT NULL DEFAULT ''",
                "wire_protocol": "TEXT NOT NULL DEFAULT ''",
                "first_connected_at": "INTEGER",
                "remote_shell": "TEXT NOT NULL DEFAULT 'system'",
            }
            for column, definition in migrations.items():
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE devices ADD COLUMN {column} {definition}"
                    )

    @staticmethod
    def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("frp_online", "ssh_available", "discovered"):
            result[field] = bool(result[field])
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM devices
                ORDER BY frp_online DESC, ssh_available DESC, name COLLATE NOCASE
                """
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def get(self, device_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return self._as_dict(row) if row else None

    def create(
        self,
        *,
        device_id: str,
        name: str,
        proxy_name: str,
        remote_port: int,
        ssh_user: str,
        remote_shell: str = "system",
        notes: str = "",
        discovered: bool = False,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO devices(
                    id, name, proxy_name, remote_port, ssh_user, remote_shell, notes,
                    created_at, updated_at, discovered
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    name,
                    proxy_name,
                    remote_port,
                    ssh_user,
                    remote_shell,
                    notes,
                    now,
                    now,
                    int(discovered),
                ),
            )
        return self.get(device_id) or {}

    def update(self, device_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "name",
            "proxy_name",
            "remote_port",
            "ssh_user",
            "remote_shell",
            "notes",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return self.get(device_id)
        updates["updated_at"] = int(time.time())
        updates["discovered"] = 0
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE devices SET {assignments} WHERE id = ?",
                (*updates.values(), device_id),
            )
        return self.get(device_id) if cursor.rowcount else None

    def delete(self, device_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        return cursor.rowcount == 1

    def sync_proxies(
        self,
        proxies: list[FrpProxy],
        clients: dict[str, dict[str, Any]] | None = None,
        auto_discover: bool = True,
    ) -> None:
        now = int(time.time())
        clients = clients or {}
        by_name = {proxy.name: proxy for proxy in proxies}
        with self._connect() as connection:
            if auto_discover:
                existing_names = {
                    row[0]
                    for row in connection.execute("SELECT proxy_name FROM devices").fetchall()
                }
                existing_ports = {
                    int(row[0])
                    for row in connection.execute("SELECT remote_port FROM devices").fetchall()
                }
                for proxy in proxies:
                    if proxy.name in existing_names or proxy.remote_port in existing_ports:
                        continue
                    device_id = uuid.uuid5(
                        uuid.NAMESPACE_URL, f"frp-proxy:{proxy.name}"
                    ).hex[:16]
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO devices(
                            id, name, proxy_name, remote_port, ssh_user, notes,
                            created_at, updated_at, discovered
                        ) VALUES (?, ?, ?, ?, ?, '', ?, ?, 1)
                        """,
                        (
                            device_id,
                            proxy.name,
                            proxy.name,
                            proxy.remote_port,
                            proxy.ssh_user,
                            now,
                            now,
                        ),
                    )

            rows = connection.execute("SELECT id, proxy_name FROM devices").fetchall()
            for row in rows:
                proxy = by_name.get(row["proxy_name"])
                online = bool(proxy and proxy.online)
                client = clients.get(proxy.client_id, {}) if proxy else {}
                connection.execute(
                    """
                    UPDATE devices
                    SET frp_online = ?,
                        last_seen_at = CASE WHEN ? THEN ? ELSE last_seen_at END,
                        client_version = ?,
                        last_start_time = ?,
                        last_close_time = ?,
                        client_id = ?,
                        hostname = ?,
                        client_ip = ?,
                        wire_protocol = ?,
                        first_connected_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(online),
                        int(online),
                        now,
                        (
                            proxy.client_version
                            or str(client.get("version") or "")
                        )
                        if proxy
                        else "",
                        proxy.last_start_time if proxy else "",
                        proxy.last_close_time if proxy else "",
                        proxy.client_id if proxy else "",
                        str(client.get("hostname") or ""),
                        str(client.get("clientIP") or ""),
                        str(client.get("wireProtocol") or ""),
                        int(client["firstConnectedAt"])
                        if client.get("firstConnectedAt")
                        else None,
                        now,
                        row["id"],
                    ),
                )

    def update_probe(self, device_id: str, available: bool, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE devices
                SET ssh_available = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(available), error[:500], int(time.time()), device_id),
            )


def probe_ssh(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            banner = connection.recv(255)
        if banner.startswith(b"SSH-"):
            return True, ""
        preview = banner[:80].decode("utf-8", errors="replace").strip()
        return False, f"端口可连接，但不是 SSH 服务{': ' + preview if preview else ''}"
    except (OSError, TimeoutError) as exc:
        return False, str(exc)


class FrpMonitor:
    def __init__(
        self,
        store: DeviceStore,
        dashboard_url: str,
        username: str = "",
        password: str = "",
        *,
        interval: float = 15.0,
        proxy_host: str = "127.0.0.1",
        auto_discover: bool = True,
    ) -> None:
        self.store = store
        self.dashboard_url = dashboard_url.rstrip("/")
        self.username = username
        self.password = password
        self.interval = max(interval, 5.0)
        self.proxy_host = proxy_host
        self.auto_discover = auto_discover
        self.last_sync_at: int | None = None
        self.last_error = ""
        self.server_info: dict[str, Any] = {}

    def _fetch_json(self, path: str) -> Any:
        request = urllib.request.Request(f"{self.dashboard_url}{path}")
        if self.username or self.password:
            credentials = base64.b64encode(
                f"{self.username}:{self.password}".encode("utf-8")
            ).decode("ascii")
            request.add_header("Authorization", f"Basic {credentials}")
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def _fetch_clients(self) -> list[dict[str, Any]]:
        try:
            payload = self._fetch_json("/api/clients")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            raise
        return payload if isinstance(payload, list) else payload.get("clients", [])

    @staticmethod
    def _parse_proxies(payload: dict[str, Any]) -> list[FrpProxy]:
        proxies: list[FrpProxy] = []
        for item in payload.get("proxies", []):
            config = item.get("conf") or {}
            annotations = config.get("annotations") or item.get("annotations") or {}
            try:
                remote_port = int(config.get("remotePort") or item.get("remotePort") or 0)
            except (TypeError, ValueError):
                continue
            name = str(item.get("name") or config.get("name") or "").strip()
            if not name or remote_port < 1:
                continue
            proxies.append(
                FrpProxy(
                    name=name,
                    remote_port=remote_port,
                    online=str(item.get("status", "")).lower() == "online",
                    client_version=str(item.get("clientVersion") or ""),
                    last_start_time=str(item.get("lastStartTime") or ""),
                    last_close_time=str(item.get("lastCloseTime") or ""),
                    client_id=str(item.get("clientID") or ""),
                    ssh_user=str(annotations.get("ssh_user") or "root"),
                )
            )
        return proxies

    async def sync_once(self) -> dict[str, Any]:
        try:
            proxy_payload, server_info, client_payload = await asyncio.gather(
                asyncio.to_thread(self._fetch_json, "/api/proxy/tcp"),
                asyncio.to_thread(self._fetch_json, "/api/serverinfo"),
                asyncio.to_thread(self._fetch_clients),
            )
            proxies = self._parse_proxies(proxy_payload)
            clients = {
                str(client.get("clientID") or client.get("runID") or ""): client
                for client in client_payload
            }
            await asyncio.to_thread(
                self.store.sync_proxies, proxies, clients, self.auto_discover
            )
            devices = await asyncio.to_thread(self.store.list)
            for device in devices:
                if not device["frp_online"]:
                    await asyncio.to_thread(
                        self.store.update_probe, device["id"], False, "FRP 隧道离线"
                    )
                    continue
                available, error = await asyncio.to_thread(
                    probe_ssh,
                    self.proxy_host,
                    int(device["remote_port"]),
                )
                await asyncio.to_thread(
                    self.store.update_probe, device["id"], available, error
                )
            self.server_info = server_info
            self.last_sync_at = int(time.time())
            self.last_error = ""
            return self.status()
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            self.last_error = str(exc)
            raise

    async def run(self) -> None:
        while True:
            try:
                await self.sync_once()
            except Exception:
                # The status endpoint exposes the latest error. A transient dashboard
                # failure must not stop the application lifespan task.
                pass
            await asyncio.sleep(self.interval)

    def status(self) -> dict[str, Any]:
        return {
            "dashboard_url": self.dashboard_url,
            "last_sync_at": self.last_sync_at,
            "last_error": self.last_error,
            "server": self.server_info,
        }
