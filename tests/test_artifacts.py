import asyncio
import io
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image, features

from app.artifacts import (
    ArtifactEventStore,
    AttachmentAccessDenied,
    AttachmentIntegrityError,
    AttachmentStore,
    ImageValidationError,
    WorkspaceFileRef,
    AttachmentPayload,
    build_openai_responses_image_content,
    build_read_image_result,
)


def image_bytes(
    image_format: str = "PNG", *, size: tuple[int, int] = (3, 2)
) -> bytes:
    mode = "RGB" if image_format in {"JPEG", "WEBP"} else "RGBA"
    image = Image.new(mode, size, (12, 34, 56) if mode == "RGB" else (12, 34, 56, 255))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


class ArtifactEventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "events.db"
        self.store = ArtifactEventStore(self.database_path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_events_persist_all_reference_fields_and_are_scope_isolated(self) -> None:
        file = WorkspaceFileRef(
            path="src/screenshots/result.png",
            name="result.png",
            media_type="image/png",
            size=321,
        )
        event = self.store.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="read_image",
            file=file,
            source="agent-tool",
            version="sha256:file-version",
            created_at=1234.5,
            task_id="task-1",
            run_id="run-1",
            span_id="span-1",
        )

        reopened = ArtifactEventStore(self.database_path)
        self.assertEqual(
            [event], reopened.snapshot(owner="alice", terminal_id="terminal-a")
        )
        self.assertEqual([], reopened.snapshot(owner="bob", terminal_id="terminal-a"))
        self.assertEqual([], reopened.snapshot(owner="alice", terminal_id="terminal-b"))
        public = event.as_dict()
        self.assertEqual("read_image", public["type"])
        self.assertEqual("src/screenshots/result.png", public["path"])
        self.assertEqual("sha256:file-version", public["version"])
        self.assertEqual(1234.5, public["timestamp"])
        self.assertEqual("task-1", public["task_id"])
        self.assertEqual("run-1", public["run_id"])
        self.assertEqual("span-1", public["span_id"])

    def test_snapshot_cursor_is_strict_and_ordered(self) -> None:
        first = self.store.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="created",
            file=WorkspaceFileRef("one.txt"),
            source="tool",
        )
        second = self.store.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="modified",
            file=WorkspaceFileRef("two.txt"),
            source="tool",
        )
        self.assertEqual(
            [second],
            self.store.snapshot(
                owner="alice",
                terminal_id="terminal-a",
                after_sequence=first.sequence,
            ),
        )

    def test_recent_returns_the_newest_bounded_history_in_order(self) -> None:
        events = [
            self.store.append(
                owner="alice",
                terminal_id="terminal-a",
                event_type="created",
                file=WorkspaceFileRef(f"{index}.txt"),
                source="tool",
            )
            for index in range(4)
        ]

        self.assertEqual(
            events[-2:],
            self.store.recent(owner="alice", terminal_id="terminal-a", limit=2),
        )

    def test_snapshot_and_recent_filter_run_inside_terminal_scope(self) -> None:
        run_one = self.store.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="created",
            file=WorkspaceFileRef("run-one.txt"),
            source="tool",
            run_id="run-1",
        )
        self.store.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="created",
            file=WorkspaceFileRef("run-two.txt"),
            source="tool",
            run_id="run-2",
        )
        run_one_later = self.store.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="modified",
            file=WorkspaceFileRef("run-one-later.txt"),
            source="tool",
            run_id="run-1",
        )
        self.store.append(
            owner="bob",
            terminal_id="terminal-a",
            event_type="created",
            file=WorkspaceFileRef("wrong-owner.txt"),
            source="tool",
            run_id="run-1",
        )
        self.store.append(
            owner="alice",
            terminal_id="terminal-b",
            event_type="created",
            file=WorkspaceFileRef("wrong-terminal.txt"),
            source="tool",
            run_id="run-1",
        )

        self.assertEqual(
            [run_one, run_one_later],
            self.store.snapshot(
                owner="alice", terminal_id="terminal-a", run_id="run-1"
            ),
        )
        self.assertEqual(
            [run_one_later],
            self.store.snapshot(
                owner="alice",
                terminal_id="terminal-a",
                run_id="run-1",
                after_sequence=run_one.sequence,
            ),
        )
        self.assertEqual(
            [run_one_later],
            self.store.recent(
                owner="alice", terminal_id="terminal-a", run_id="run-1", limit=1
            ),
        )
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.store.snapshot(
                owner="alice", terminal_id="terminal-a", run_id=""
            )

    def test_existing_database_migrates_optional_execution_links(self) -> None:
        legacy_path = Path(self.directory.name) / "legacy-events.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE artifact_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    terminal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL DEFAULT '',
                    file_media_type TEXT,
                    file_size INTEGER,
                    file_kind TEXT NOT NULL DEFAULT 'file',
                    source TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    attachment_id TEXT,
                    attachment_media_type TEXT,
                    attachment_size INTEGER,
                    attachment_width INTEGER,
                    attachment_height INTEGER,
                    attachment_name TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO artifact_events(
                    id, owner, terminal_id, event_type, file_path, source, created_at
                ) VALUES ('legacy-event', 'alice', 'terminal-a', 'created',
                          'legacy.txt', 'tool', 1.0)
                """
            )

        migrated = ArtifactEventStore(legacy_path)
        [legacy] = migrated.snapshot(owner="alice", terminal_id="terminal-a")
        self.assertIsNone(legacy.task_id)
        self.assertIsNone(legacy.run_id)
        self.assertIsNone(legacy.span_id)
        linked = migrated.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="modified",
            file=WorkspaceFileRef("linked.txt"),
            source="tool",
            task_id="task-2",
            run_id="run-2",
            span_id="span-2",
        )

        reopened = ArtifactEventStore(legacy_path)
        self.assertEqual(
            [linked],
            reopened.snapshot(
                owner="alice", terminal_id="terminal-a", run_id="run-2"
            ),
        )


class ArtifactSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = ArtifactEventStore(Path(self.directory.name) / "events.db")

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_subscription_has_atomic_snapshot_then_scoped_events(self) -> None:
        initial = self.store.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="created",
            file=WorkspaceFileRef("initial.txt"),
            source="tool",
        )
        subscription = self.store.subscribe(owner="alice", terminal_id="terminal-a")
        self.assertEqual((initial,), subscription.snapshot)

        self.store.append(
            owner="alice",
            terminal_id="terminal-b",
            event_type="created",
            file=WorkspaceFileRef("wrong-terminal.txt"),
            source="tool",
        )
        self.store.append(
            owner="bob",
            terminal_id="terminal-a",
            event_type="created",
            file=WorkspaceFileRef("wrong-owner.txt"),
            source="tool",
        )
        expected = self.store.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="modified",
            file=WorkspaceFileRef("live.txt"),
            source="tool",
        )

        self.assertEqual(expected, await asyncio.wait_for(anext(subscription), 1))
        await subscription.aclose()
        self.assertNotIn(("alice", "terminal-a"), self.store._subscribers)

    async def test_subscription_snapshot_and_live_queue_are_bounded(self) -> None:
        bounded = ArtifactEventStore(
            Path(self.directory.name) / "bounded.db",
            max_subscription_queue=2,
        )
        history = [
            bounded.append(
                owner="alice",
                terminal_id="terminal-a",
                event_type="created",
                file=WorkspaceFileRef(f"history-{index}.txt"),
                source="tool",
            )
            for index in range(4)
        ]
        subscription = bounded.subscribe(
            owner="alice",
            terminal_id="terminal-a",
            snapshot_limit=2,
        )
        self.assertEqual(tuple(history[-2:]), subscription.snapshot)

        live = [
            bounded.append(
                owner="alice",
                terminal_id="terminal-a",
                event_type="modified",
                file=WorkspaceFileRef(f"live-{index}.txt"),
                source="tool",
            )
            for index in range(3)
        ]
        self.assertEqual(live[-2], await asyncio.wait_for(anext(subscription), 1))
        self.assertEqual(live[-1], await asyncio.wait_for(anext(subscription), 1))
        await subscription.aclose()


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.attachments = AttachmentStore(root / "attachments")
        self.events = ArtifactEventStore(root / "events.db")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_supported_formats_are_detected_from_bytes_not_names(self) -> None:
        cases = [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif")]
        if features.check("webp"):
            cases.append(("WEBP", "image/webp"))
        for image_format, media_type in cases:
            with self.subTest(image_format=image_format):
                ref = self.attachments.save_image(
                    image_bytes(image_format),
                    declared_media_type=media_type,
                    name="C:\\unsafe\\folder\\picture.not-the-real-extension",
                )
                self.assertEqual(media_type, ref.media_type)
                self.assertEqual((3, 2), (ref.width, ref.height))
                self.assertEqual("picture.not-the-real-extension", ref.name)

    def test_content_addressing_deduplicates_and_uses_private_object(self) -> None:
        data = image_bytes()
        first = self.attachments.save_image(data, name="first.png")
        second = self.attachments.save_image(data, name="second.png")
        self.assertEqual(first.id, second.id)
        objects = [
            path for path in self.attachments.objects.rglob("*") if path.is_file()
        ]
        self.assertEqual(1, len(objects))
        self.assertEqual(0o600, objects[0].stat().st_mode & 0o777)

    def test_invalid_type_byte_and_pixel_limits_fail_before_storage(self) -> None:
        with self.assertRaisesRegex(ImageValidationError, "valid supported image"):
            self.attachments.save_image(b"not really a png", name="fake.png")
        with self.assertRaisesRegex(ImageValidationError, "does not match"):
            self.attachments.save_image(
                image_bytes(), declared_media_type="image/jpeg"
            )
        with self.assertRaisesRegex(ImageValidationError, "byte limit"):
            AttachmentStore(
                Path(self.directory.name) / "tiny-bytes",
                max_image_bytes=len(image_bytes()) - 1,
            ).save_image(image_bytes())
        with self.assertRaisesRegex(ImageValidationError, "pixel limit"):
            AttachmentStore(
                Path(self.directory.name) / "tiny-pixels", max_image_pixels=5
            ).save_image(image_bytes(size=(3, 2)))

    def test_read_requires_exact_owner_and_terminal_event_reference(self) -> None:
        data = image_bytes()
        ref = self.attachments.save_image(data, name="result.png")
        file = WorkspaceFileRef(
            "output/result.png",
            media_type="image/png",
            size=len(data),
        )
        for owner, terminal_id in (
            ("alice", "terminal-a"),
            ("alice", "terminal-b"),
            ("bob", "terminal-a"),
        ):
            with self.subTest(owner=owner, terminal_id=terminal_id):
                with self.assertRaises(AttachmentAccessDenied):
                    self.attachments.read_authorized(
                        self.events,
                        owner=owner,
                        terminal_id=terminal_id,
                        attachment_id=ref.id,
                    )

        self.events.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="read_image",
            file=file,
            source="agent-tool",
            attachment=ref,
            task_id="task-1",
            run_id="run-1",
            span_id="span-1",
        )
        payload = self.attachments.read_authorized(
            self.events,
            owner="alice",
            terminal_id="terminal-a",
            attachment_id=ref.id,
        )
        self.assertEqual(ref, payload.ref)
        self.assertEqual(data, payload.data)
        for owner, terminal_id in (
            ("bob", "terminal-a"),
            ("alice", "terminal-b"),
        ):
            with self.subTest(owner=owner, terminal_id=terminal_id):
                with self.assertRaises(AttachmentAccessDenied):
                    self.attachments.read_authorized(
                        self.events,
                        owner=owner,
                        terminal_id=terminal_id,
                        attachment_id=ref.id,
                    )

    def test_read_detects_content_tampering(self) -> None:
        data = image_bytes()
        ref = self.attachments.save_image(data)
        self.events.append(
            owner="alice",
            terminal_id="terminal-a",
            event_type="read_image",
            file=WorkspaceFileRef(
                "result.png", media_type="image/png", size=len(data)
            ),
            source="agent-tool",
            attachment=ref,
        )
        self.attachments._path_for_id(ref.id).write_bytes(data + b"tampered")
        with self.assertRaisesRegex(AttachmentIntegrityError, "SHA-256"):
            self.attachments.read_authorized(
                self.events,
                owner="alice",
                terminal_id="terminal-a",
                attachment_id=ref.id,
            )


class ReadImageResultTests(unittest.TestCase):
    def test_result_has_text_and_durable_image_without_embedded_bytes(self) -> None:
        file = WorkspaceFileRef(
            "设计稿/首页.png", media_type="image/png", size=123
        )
        with tempfile.TemporaryDirectory() as directory:
            attachment = AttachmentStore(
                Path(directory) / "attachments"
            ).save_image(image_bytes(), name="首页.png")

        blocks = build_read_image_result(file, attachment)

        self.assertEqual(["text", "image"], [block["type"] for block in blocks])
        envelope = json.loads(str(blocks[0]["text"]))
        self.assertEqual("设计稿/首页.png", envelope["path"])
        self.assertEqual(attachment.id, envelope["image"]["id"])
        self.assertEqual(attachment.as_dict(), blocks[1]["attachment"])
        self.assertNotIn("data", blocks[1])
        self.assertNotIn("base64", blocks[0]["text"])

    def test_openai_bridge_embeds_verified_bytes_only_in_transient_content(self) -> None:
        data = image_bytes()
        file = WorkspaceFileRef("result.png", media_type="image/png", size=len(data))
        with tempfile.TemporaryDirectory() as directory:
            attachment = AttachmentStore(
                Path(directory) / "attachments"
            ).save_image(data, name="result.png")

        blocks = build_openai_responses_image_content(
            file, AttachmentPayload(ref=attachment, data=data)
        )

        self.assertEqual(["input_text", "input_image"], [b["type"] for b in blocks])
        prefix, encoded = str(blocks[1]["image_url"]).split(",", 1)
        self.assertEqual("data:image/png;base64", prefix)
        self.assertEqual(data, base64.b64decode(encoded, validate=True))

        with self.assertRaises(AttachmentIntegrityError):
            build_openai_responses_image_content(
                file, AttachmentPayload(ref=attachment, data=data + b"tampered")
            )


if __name__ == "__main__":
    unittest.main()
