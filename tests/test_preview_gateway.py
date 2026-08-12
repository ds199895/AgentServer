import asyncio
import os
import tempfile
import unittest
from http.cookies import SimpleCookie

os.environ.setdefault("ADMIN_PASSWORD", "test-only-password")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agentserver-preview-test-"))
os.environ.setdefault("PREVIEW_PUBLIC_ORIGIN", "http://preview.test")

import httpx
from websockets.asyncio.server import serve

from app.main import PREVIEW_COOKIE_NAME, app, preview_access_signer
from app.preview import PreviewManager, PreviewSession


class FakeProcess:
    returncode = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return int(self.returncode or 0)


class PreviewGatewayTests(unittest.IsolatedAsyncioTestCase):
    preview_id = "dbe39a91-0676-4d99-ae37-90257df73c13"

    async def asyncSetUp(self) -> None:
        self.manager = PreviewManager()
        app.state.previews = self.manager

    def authorize_headers(self) -> dict[str, str]:
        token = preview_access_signer.issue(f"access:{self.preview_id}:admin")
        cookie = SimpleCookie()
        cookie[PREVIEW_COOKIE_NAME] = token
        return {
            "host": f"{self.preview_id}.preview.test",
            "cookie": cookie.output(header="").strip(),
        }

    async def test_http_preview_requires_cookie_and_streams_upstream(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            request = await reader.readuntil(b"\r\n\r\n")
            path = request.split(b" ", 2)[1]
            body = b"preview:" + path
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        self.manager.sessions[self.preview_id] = PreviewSession(
            id=self.preview_id,
            device_id="device-001",
            device_name="Test device",
            target_port=5173,
            local_port=port,
            label="Vite",
            process=FakeProcess(),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://preview.test") as client:
            unauthorized = await client.get(
                "/hello?hmr=1", headers={"host": f"{self.preview_id}.preview.test"}
            )
            self.assertEqual(401, unauthorized.status_code)
            wrong_origin = await client.get(
                f"/preview/{self.preview_id}/hello",
                headers={
                    **self.authorize_headers(),
                    "host": "agent.test",
                },
            )
            self.assertEqual(404, wrong_origin.status_code)
            response = await client.get("/hello?hmr=1", headers=self.authorize_headers())
        server.close()
        await server.wait_closed()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("preview:/hello?hmr=1", response.text)

    async def test_websocket_preview_relays_text_frames(self) -> None:
        async def echo(websocket) -> None:
            async for message in websocket:
                await websocket.send(f"echo:{message}")

        upstream = await serve(echo, "127.0.0.1", 0)
        port = int(upstream.sockets[0].getsockname()[1])
        self.manager.sessions[self.preview_id] = PreviewSession(
            id=self.preview_id,
            device_id="device-001",
            device_name="Test device",
            target_port=5173,
            local_port=port,
            label="Vite",
            process=FakeProcess(),
        )
        sent = []
        echoed = asyncio.Event()
        receives = 0

        async def receive():
            nonlocal receives
            receives += 1
            if receives == 1:
                return {"type": "websocket.connect"}
            if receives == 2:
                return {"type": "websocket.receive", "text": "hmr"}
            await echoed.wait()
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            sent.append(message)
            if message.get("text") == "echo:hmr":
                echoed.set()

        headers = self.authorize_headers()
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/socket",
            "raw_path": b"/socket",
            "query_string": b"",
            "root_path": "",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
            "client": ("127.0.0.1", 12345),
            "server": ("preview.test", 80),
            "subprotocols": [],
            "state": {},
            "app": app,
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        upstream.close()
        await upstream.wait_closed()
        self.assertTrue(any(message["type"] == "websocket.accept" for message in sent))
        self.assertTrue(any(message.get("text") == "echo:hmr" for message in sent))


if __name__ == "__main__":
    unittest.main()
