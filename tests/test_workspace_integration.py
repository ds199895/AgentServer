from __future__ import annotations

import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ADMIN_PASSWORD", "test-only-password")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="agentserver-workspace-api-test-"))

from fastapi import HTTPException
from PIL import Image

from app.artifacts import ArtifactEventStore, AttachmentStore
from app.main import (
    ArtifactBody,
    ReadImageBody,
    ResolveFileBody,
    artifact_socket,
    create_artifact,
    list_artifacts,
    list_workspace,
    read_attachment,
    read_image_tool,
    read_workspace_file,
    resolve_workspace_file,
    signer,
)
from app.terminal import TerminalSession
from app.workspace import WorkspaceService


class _TerminalManager:
    def __init__(self, session: TerminalSession) -> None:
        self.session = session

    def get_for_owner(self, session_id: str, owner: str) -> TerminalSession | None:
        if session_id == self.session.id and owner == self.session.owner:
            return self.session
        return None


class WorkspaceApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "docs").mkdir()
        (root / "docs" / "note.txt").write_text("0123456789", encoding="utf-8")
        (root / "docs" / "empty.txt").write_bytes(b"")
        image = Image.new("RGB", (4, 3), (12, 34, 56))
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.image_data = output.getvalue()
        (root / "result.png").write_bytes(self.image_data)

        self.session = TerminalSession(
            id="terminal-a",
            name="Workspace",
            pid=-1,
            fd=-1,
            command="shell",
            cwd=str(root),
            owner="alice",
            workspace_kind="local",
            workspace_root=str(root),
        )
        self.manager = _TerminalManager(self.session)
        self.workspaces = WorkspaceService(max_read_bytes=8 * 1024 * 1024)
        self.artifacts = ArtifactEventStore(root / "events.db")
        self.attachments = AttachmentStore(root / "attachments")
        state = SimpleNamespace(
            workspaces=self.workspaces,
            artifacts=self.artifacts,
            attachments=self.attachments,
        )
        self.application = SimpleNamespace(state=state)
        self.request = SimpleNamespace(app=self.application, headers={})

    async def asyncTearDown(self) -> None:
        self.workspaces.close()
        self.directory.cleanup()

    async def test_listing_grant_range_and_scope_form_one_safe_flow(self) -> None:
        listing = await list_workspace(
            self.session.id,
            self.request,
            path="docs",
            manager=self.manager,
            service=self.workspaces,
            username="alice",
        )
        self.assertEqual("docs", listing["path"])
        self.assertEqual(
            {"empty.txt", "note.txt"},
            {entry["name"] for entry in listing["entries"]},
        )
        self.assertEqual("", listing["parent_path"])

        grant = await resolve_workspace_file(
            self.session.id,
            ResolveFileBody(path="docs/note.txt"),
            self.request,
            manager=self.manager,
            service=self.workspaces,
            username="alice",
        )
        self.assertEqual(self.session.id, grant["terminal_id"])
        self.assertEqual("text", grant["preview_mode"])

        ranged_request = SimpleNamespace(
            app=self.application,
            headers={"range": "bytes=2-5"},
        )
        response = await read_workspace_file(
            str(grant["id"]),
            self.session.id,
            ranged_request,
            manager=self.manager,
            service=self.workspaces,
            username="alice",
        )
        self.assertEqual(206, response.status_code)
        self.assertEqual(b"2345", response.body)
        self.assertEqual("bytes 2-5/10", response.headers["content-range"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])

        invalid_request = SimpleNamespace(
            app=self.application,
            headers={"range": "bytes=99-100"},
        )
        with self.assertRaises(HTTPException) as invalid:
            await read_workspace_file(
                str(grant["id"]),
                self.session.id,
                invalid_request,
                manager=self.manager,
                service=self.workspaces,
                username="alice",
            )
        self.assertEqual(416, invalid.exception.status_code)
        self.assertEqual("bytes */10", invalid.exception.headers["Content-Range"])

        with self.assertRaises(HTTPException) as denied:
            await resolve_workspace_file(
                self.session.id,
                ResolveFileBody(path="docs/note.txt"),
                self.request,
                manager=self.manager,
                service=self.workspaces,
                username="bob",
            )
        self.assertEqual(404, denied.exception.status_code)

    async def test_read_image_creates_replayable_event_and_authorized_attachment(self) -> None:
        result = await read_image_tool(
            self.session.id,
            ReadImageBody(path="result.png"),
            self.request,
            manager=self.manager,
            service=self.workspaces,
            username="alice",
        )
        attachment = result["attachment"]
        self.assertTrue(str(attachment["id"]).startswith("sha256:"))
        self.assertEqual(["text", "image"], [item["type"] for item in result["content"]])
        self.assertEqual("openai-responses", result["model_content_format"])
        self.assertEqual(
            ["input_text", "input_image"],
            [item["type"] for item in result["model_content"]],
        )
        encoded = str(result["model_content"][1]["image_url"]).split(",", 1)[1]
        self.assertEqual(self.image_data, base64.b64decode(encoded, validate=True))
        self.assertEqual("read_image", result["event"]["type"])

        response = await read_attachment(
            self.session.id,
            str(attachment["id"]),
            self.request,
            manager=self.manager,
            username="alice",
        )
        self.assertEqual(self.image_data, response.body)
        self.assertEqual("image/png", response.media_type)

    async def test_agent_api_events_share_the_snapshot_contract(self) -> None:
        created = await create_artifact(
            self.session.id,
            ArtifactBody(type="created", path="docs/note.txt", source="test-adapter"),
            self.request,
            manager=self.manager,
            username="alice",
        )
        snapshot = await list_artifacts(
            self.session.id,
            self.request,
            manager=self.manager,
            username="alice",
        )
        self.assertEqual([created["id"]], [event["id"] for event in snapshot])
        self.assertEqual("docs/note.txt", snapshot[0]["path"])

    async def test_artifact_websocket_disconnect_releases_subscription(self) -> None:
        class FakeWebSocket:
            def __init__(self, outer: "WorkspaceApiIntegrationTests") -> None:
                self.cookies = {"agentserver_session": signer.issue("alice")}
                self.app = SimpleNamespace(
                    state=SimpleNamespace(
                        terminals=outer.manager,
                        artifacts=outer.artifacts,
                    )
                )
                self.accepted = False
                self.sent: list[object] = []

            async def accept(self) -> None:
                self.accepted = True

            async def send_json(self, value: object) -> None:
                self.sent.append(value)

            async def receive(self) -> dict[str, object]:
                return {"type": "websocket.disconnect", "code": 1000}

            async def close(self, code: int) -> None:
                raise AssertionError(f"unexpected close: {code}")

        websocket = FakeWebSocket(self)
        await artifact_socket(websocket, self.session.id)  # type: ignore[arg-type]

        self.assertTrue(websocket.accepted)
        self.assertEqual({}, self.artifacts._subscribers)


if __name__ == "__main__":
    unittest.main()
