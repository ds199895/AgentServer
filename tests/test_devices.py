import tempfile
import unittest
import sqlite3
from pathlib import Path

from app.devices import DeviceStore, FrpMonitor, FrpProxy


class DeviceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = DeviceStore(Path(self.directory.name) / "devices.db")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_device_lifecycle(self) -> None:
        created = self.store.create(
            device_id="device-001",
            name="Device 001",
            proxy_name="device-001.ssh",
            remote_port=20001,
            ssh_user="operator",
            remote_shell="powershell",
        )
        self.assertEqual("device-001", created["id"])
        self.assertFalse(created["frp_online"])
        self.assertEqual("powershell", created["remote_shell"])

        updated = self.store.update(
            "device-001", {"name": "Renamed", "remote_shell": "cmd"}
        )
        self.assertIsNotNone(updated)
        self.assertEqual("Renamed", updated["name"])
        self.assertEqual("cmd", updated["remote_shell"])
        self.assertTrue(self.store.delete("device-001"))
        self.assertIsNone(self.store.get("device-001"))

    def test_existing_database_adds_remote_shell_with_system_default(self) -> None:
        database_path = Path(self.directory.name) / "legacy.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    proxy_name TEXT NOT NULL UNIQUE,
                    remote_port INTEGER NOT NULL UNIQUE,
                    ssh_user TEXT NOT NULL DEFAULT 'root',
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
                    discovered INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                INSERT INTO devices(
                    id, name, proxy_name, remote_port, ssh_user,
                    created_at, updated_at
                ) VALUES ('legacy', 'Legacy', 'legacy.ssh', 20009, 'operator', 1, 1)
                """
            )

        migrated = DeviceStore(database_path).get("legacy")
        self.assertIsNotNone(migrated)
        self.assertEqual("system", migrated["remote_shell"])

    def test_proxy_sync_tracks_online_and_offline(self) -> None:
        self.store.create(
            device_id="device-002",
            name="Device 002",
            proxy_name="device-002.ssh",
            remote_port=20002,
            ssh_user="operator",
        )
        self.store.sync_proxies(
            [FrpProxy("device-002.ssh", 20002, True, client_version="0.69.0")]
        )
        online = self.store.get("device-002")
        self.assertTrue(online["frp_online"])
        self.assertEqual("0.69.0", online["client_version"])
        self.assertIsNotNone(online["last_seen_at"])

        self.store.sync_proxies([])
        offline = self.store.get("device-002")
        self.assertFalse(offline["frp_online"])
        self.assertEqual(online["last_seen_at"], offline["last_seen_at"])

    def test_unknown_proxy_is_discovered(self) -> None:
        self.store.sync_proxies([FrpProxy("legacy-web", 18080, True)])
        devices = self.store.list()
        self.assertEqual(1, len(devices))
        self.assertEqual("legacy-web", devices[0]["proxy_name"])
        self.assertTrue(devices[0]["discovered"])

    def test_client_registry_enriches_proxy_metadata(self) -> None:
        self.store.sync_proxies(
            [FrpProxy("device-003.ssh", 20003, True, client_id="client-003")],
            {
                "client-003": {
                    "version": "0.69.0",
                    "hostname": "device-003",
                    "clientIP": "192.0.2.3",
                    "wireProtocol": "v2",
                    "firstConnectedAt": 1234,
                }
            },
        )
        device = self.store.list()[0]
        self.assertEqual("0.69.0", device["client_version"])
        self.assertEqual("device-003", device["hostname"])
        self.assertEqual("v2", device["wire_protocol"])


class FrpPayloadTests(unittest.TestCase):
    def test_proxy_payload_parser(self) -> None:
        proxies = FrpMonitor._parse_proxies(
            {
                "proxies": [
                    {
                        "name": "device.ssh",
                        "status": "online",
                        "clientVersion": "0.69.0",
                        "conf": {
                            "remotePort": 20010,
                            "annotations": {"ssh_user": "operator"},
                        },
                    }
                ]
            }
        )
        self.assertEqual(1, len(proxies))
        self.assertTrue(proxies[0].online)
        self.assertEqual(20010, proxies[0].remote_port)
        self.assertEqual("operator", proxies[0].ssh_user)


if __name__ == "__main__":
    unittest.main()
