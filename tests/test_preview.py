import asyncio
import unittest

from app.preview import (
    PreviewManager,
    preview_id_from_host,
    preview_public_url,
    rewrite_frame_ancestors,
    rewrite_set_cookie,
    upstream_cookie,
)


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return int(self.returncode or 0)


class PreviewAddressTests(unittest.TestCase):
    def test_preview_id_is_extracted_from_isolated_subdomain(self) -> None:
        preview_id = "dbe39a91-0676-4d99-ae37-90257df73c13"
        self.assertEqual(
            preview_id,
            preview_id_from_host(
                f"{preview_id}.preview.example.com:443", "preview.example.com"
            ),
        )
        self.assertIsNone(
            preview_id_from_host("preview.example.com", "preview.example.com")
        )
        self.assertIsNone(
            preview_id_from_host(
                f"extra.{preview_id}.preview.example.com", "preview.example.com"
            )
        )

    def test_public_url_preserves_scheme_and_port(self) -> None:
        self.assertEqual(
            "https://abc.preview.example.com/",
            preview_public_url("abc", "https://preview.example.com"),
        )

    def test_proxy_header_rewrites_preserve_app_state_but_hide_gateway_cookie(self) -> None:
        self.assertEqual(
            "app_session=abc; theme=dark",
            upstream_cookie(
                "agentserver_preview=private; app_session=abc; theme=dark"
            ),
        )
        self.assertEqual(
            "app_session=abc; Path=/; HttpOnly",
            rewrite_set_cookie(
                "app_session=abc; Domain=localhost; Path=/; HttpOnly"
            ),
        )
        self.assertEqual(
            "default-src 'self'; connect-src 'self' ws:",
            rewrite_frame_ancestors(
                "default-src 'self'; frame-ancestors 'none'; connect-src 'self' ws:"
            ),
        )
        self.assertEqual(
            "http://abc.preview.test:18100/",
            preview_public_url("abc", "http://preview.test:18100"),
        )


class PreviewManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.process = FakeProcess()

        async def launcher(*_args, **_kwargs):
            return self.process

        self.manager = PreviewManager(idle_timeout=60, launcher=launcher)

        async def ready(_process, _port, timeout=10):
            return None

        self.manager._wait_until_ready = ready

    async def asyncTearDown(self) -> None:
        await self.manager.close()

    async def test_create_list_touch_and_delete(self) -> None:
        preview = await self.manager.create(
            device_id="device-001",
            device_name="Development Mac",
            target_port=5173,
            label="Vite",
            terminal_id="terminal-001",
            tunnel_command=lambda port: ["ssh", "-L", str(port)],
        )
        self.assertEqual(5173, preview.target_port)
        self.assertEqual("device-001", preview.device_id)
        self.assertEqual("terminal-001", preview.terminal_id)
        previous_access = preview.last_access_at
        await asyncio.sleep(0)
        self.assertIs(preview, self.manager.get(preview.id, touch=True))
        self.assertGreaterEqual(preview.last_access_at, previous_access)
        self.assertEqual(1, len(self.manager.list()))
        self.assertTrue(await self.manager.delete(preview.id))
        self.assertTrue(self.process.terminated)
        self.assertFalse(await self.manager.delete(preview.id))

    async def test_idle_preview_is_reclaimed(self) -> None:
        preview = await self.manager.create(
            device_id="device-001",
            device_name="Development Mac",
            target_port=3000,
            label="Next.js",
            tunnel_command=lambda port: ["ssh", "-L", str(port)],
        )
        preview.last_access_at = 100
        self.assertEqual(1, await self.manager.cleanup_idle(now=1000))
        self.assertNotIn(preview.id, self.manager.sessions)
        self.assertTrue(self.process.terminated)


if __name__ == "__main__":
    unittest.main()
