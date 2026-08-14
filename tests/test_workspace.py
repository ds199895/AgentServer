from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.workspace import (
    LocalWorkspaceProvider,
    SftpWorkspaceProvider,
    WorkspaceAccessDenied,
    WorkspaceEntry,
    WorkspaceFileChanged,
    WorkspaceGrantNotFound,
    WorkspaceInvalidRange,
    WorkspaceNotFile,
    WorkspaceProvider,
    WorkspaceRead,
    WorkspaceService,
    WorkspaceTooLarge,
    WorkspaceUnavailable,
)


PNG_2X3 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x02"
    b"\x00\x00\x00\x03"
)


class LocalWorkspaceProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "beta.txt").write_text("beta", encoding="utf-8")
        (self.root / "Alpha.txt").write_text("0123456789", encoding="utf-8")
        (self.root / "folder").mkdir()
        (self.root / "folder" / "nested.txt").write_text("nested", encoding="utf-8")
        self.provider = LocalWorkspaceProvider(self.root)

    def tearDown(self) -> None:
        self.provider.close()
        self.directory.cleanup()

    def test_list_stat_and_ranges_are_stable_and_root_relative(self) -> None:
        listing = self.provider.list()
        self.assertEqual(["Alpha.txt", "beta.txt", "folder"], [item.name for item in listing])
        self.assertEqual(["Alpha.txt", "beta.txt", "folder"], [item.path for item in listing])
        self.assertEqual("file", listing[0].kind)
        self.assertEqual("directory", listing[2].kind)

        first = self.provider.stat("Alpha.txt")
        second = self.provider.stat("./Alpha.txt")
        self.assertEqual(first, second)
        self.assertTrue(first.etag.startswith('W/"'))

        window = self.provider.read_range(
            "Alpha.txt", start=2, end=7, max_bytes=5
        )
        self.assertEqual(b"23456", window.data)
        self.assertEqual((2, 7, 10), (window.start, window.end, window.total))
        self.assertEqual(first.etag, window.entry.etag)

    def test_traversal_absolute_paths_and_oversized_reads_are_rejected(self) -> None:
        for unsafe in ("../outside", "folder/../../outside", "/etc/passwd", "C:\\Windows\\win.ini"):
            with self.subTest(path=unsafe), self.assertRaises(WorkspaceAccessDenied):
                self.provider.stat(unsafe)
        with self.assertRaises(WorkspaceTooLarge):
            self.provider.read_range("Alpha.txt", max_bytes=9)
        with self.assertRaises(WorkspaceInvalidRange) as caught:
            self.provider.read_range("Alpha.txt", start=11, max_bytes=1)
        self.assertEqual(10, caught.exception.total)
        with self.assertRaises(WorkspaceNotFile):
            self.provider.read_range("folder", max_bytes=10)

    def test_leading_and_trailing_spaces_are_part_of_a_valid_filename(self) -> None:
        name = " leading and trailing "
        (self.root / name).write_bytes(b"spaced")

        entry = self.provider.stat(name)
        result = self.provider.read_range(name, max_bytes=6)

        self.assertEqual(name, entry.name)
        self.assertEqual(name, entry.path)
        self.assertEqual(b"spaced", result.data)
        self.assertIn(name, [item.name for item in self.provider.list()])

    def test_local_etag_includes_change_time_even_if_size_and_mtime_match(self) -> None:
        target = self.root / "Alpha.txt"
        original_info = target.stat()
        original = self.provider.stat("Alpha.txt")

        # Ensure the following metadata change crosses the filesystem clock
        # tick even on coarsely virtualized CI filesystems.
        time.sleep(0.01)
        target.write_bytes(b"abcdefghij")
        os.utime(
            target,
            ns=(original_info.st_atime_ns, original_info.st_mtime_ns),
        )
        changed = self.provider.stat("Alpha.txt")

        self.assertEqual(original.size, changed.size)
        self.assertEqual(original.modified_at, changed.modified_at)
        self.assertNotEqual(original.etag, changed.etag)

    def test_symlink_escape_is_visible_but_never_followed(self) -> None:
        outside = tempfile.TemporaryDirectory()
        try:
            secret = Path(outside.name) / "secret.txt"
            secret.write_text("secret", encoding="utf-8")
            link = self.root / "escape"
            try:
                link.symlink_to(secret)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable")

            listing = self.provider.list()
            self.assertEqual("symlink", next(item for item in listing if item.name == "escape").kind)
            with self.assertRaises(WorkspaceAccessDenied):
                self.provider.stat("escape")
            with self.assertRaises(WorkspaceAccessDenied):
                self.provider.read_range("escape", max_bytes=100)
        finally:
            outside.cleanup()

    def test_listing_limit_fails_instead_of_returning_a_partial_directory(self) -> None:
        with self.assertRaises(WorkspaceTooLarge):
            self.provider.list(max_entries=2)


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "note.txt").write_text("0123456789", encoding="utf-8")
        (self.root / "spoof.png").write_text(
            "<!doctype html><script>parent.location='/'</script>", encoding="utf-8"
        )
        (self.root / "pixel.bin").write_bytes(PNG_2X3)
        (self.root / "document.bin").write_bytes(b"%PDF-1.7\n%%EOF\n")
        self.now = [100.0]
        tokens = iter(
            [
                "workspace-binding-secret",
                "note-file-grant-secret",
                "html-file-grant-secret",
                "png-file-grant-secret",
                "pdf-file-grant-secret",
                "replacement-binding-secret",
            ]
        )
        self.service = WorkspaceService(
            grant_ttl=10,
            max_file_bytes=100,
            max_read_bytes=4,
            sniff_bytes=64,
            max_image_pixels=100,
            clock=lambda: self.now[0],
            token_factory=lambda: next(tokens),
        )
        self.binding = self.service.bind(
            "alice", "terminal-1", LocalWorkspaceProvider(self.root)
        )

    def tearDown(self) -> None:
        self.service.close()
        self.directory.cleanup()

    def test_binding_list_stat_and_grant_are_owner_terminal_scoped(self) -> None:
        self.assertEqual("local", self.binding.provider_kind)
        self.assertEqual(str(self.root.resolve()), self.binding.root)
        self.assertIn(
            "note.txt",
            [entry.name for entry in self.service.list("alice", "terminal-1")],
        )
        self.assertEqual(10, self.service.stat("alice", "terminal-1", "note.txt").size)

        grant = self.service.grant("alice", "terminal-1", "note.txt")
        self.assertEqual("note-file-grant-secret", grant.id)
        self.assertEqual("text/plain", grant.media_type)
        self.assertEqual("text", grant.preview_kind)
        self.assertFalse(grant.inline_safe)
        for owner, terminal in (("mallory", "terminal-1"), ("alice", "terminal-2")):
            with self.subTest(owner=owner, terminal=terminal), self.assertRaises(
                WorkspaceGrantNotFound
            ):
                self.service.resolve_grant(grant.id, owner, terminal)
        self.assertEqual(
            grant, self.service.resolve_grant(grant.id, "alice", "terminal-1")
        )

    def test_magic_beats_extension_and_only_bounded_raster_is_inline(self) -> None:
        html = self.service.grant("alice", "terminal-1", "spoof.png")
        self.assertEqual("text/html", html.media_type)
        self.assertEqual("text", html.preview_kind)
        self.assertFalse(html.inline_safe)

        image = self.service.grant("alice", "terminal-1", "pixel.bin")
        self.assertEqual("image/png", image.media_type)
        self.assertEqual("image", image.preview_kind)
        self.assertTrue(image.inline_safe)
        self.assertEqual((2, 3), (image.image_width, image.image_height))

        pdf = self.service.grant("alice", "terminal-1", "document.bin")
        self.assertEqual("application/pdf", pdf.media_type)
        self.assertEqual("pdf", pdf.preview_mode)
        self.assertFalse(pdf.inline_safe)
        self.assertEqual("file", pdf.as_dict()["kind"])

    def test_range_etag_and_conditional_response_metadata_are_endpoint_ready(self) -> None:
        grant = self.service.grant("alice", "terminal-1", "note.txt")
        with self.assertRaises(WorkspaceTooLarge):
            self.service.read(grant.id, "alice", "terminal-1")

        response = self.service.read(
            grant.id, "alice", "terminal-1", range_header="bytes=2-5"
        )
        self.assertEqual(206, response.status_code)
        self.assertEqual(b"2345", response.body)
        self.assertEqual("bytes 2-5/10", response.headers["Content-Range"])
        self.assertEqual("bytes", response.headers["Accept-Ranges"])
        self.assertEqual(grant.etag, response.headers["ETag"])
        self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))

        cached = self.service.read(
            grant.id,
            "alice",
            "terminal-1",
            if_none_match=f'"other", {grant.etag}',
        )
        self.assertEqual(304, cached.status_code)
        self.assertEqual(b"", cached.body)

        suffix = self.service.read(
            grant.id, "alice", "terminal-1", range_header="bytes=-3"
        )
        self.assertEqual(b"789", suffix.body)
        with self.assertRaises(WorkspaceInvalidRange) as caught:
            self.service.read(
                grant.id, "alice", "terminal-1", range_header="bytes=20-30"
            )
        self.assertEqual(10, caught.exception.total)

    def test_expiry_rebinding_file_changes_and_size_caps_invalidate_reads(self) -> None:
        grant = self.service.grant("alice", "terminal-1", "note.txt")
        (self.root / "note.txt").write_text("changed-size", encoding="utf-8")
        with self.assertRaises(WorkspaceFileChanged):
            self.service.read(
                grant.id, "alice", "terminal-1", range_header="bytes=0-3"
            )
        with self.assertRaises(WorkspaceFileChanged):
            self.service.read(
                grant.id,
                "alice",
                "terminal-1",
                if_none_match=grant.etag,
            )

        fresh = self.service.grant("alice", "terminal-1", "note.txt")
        self.now[0] = fresh.expires_at
        with self.assertRaises(WorkspaceGrantNotFound):
            self.service.resolve_grant(fresh.id, "alice", "terminal-1")

        self.now[0] += 1
        replacement = LocalWorkspaceProvider(self.root)
        self.service.bind("alice", "terminal-1", replacement)
        with self.assertRaises(WorkspaceGrantNotFound):
            self.service.resolve_grant(fresh.id, "alice", "terminal-1")

        (self.root / "large.bin").write_bytes(b"x" * 101)
        with self.assertRaises(WorkspaceTooLarge):
            self.service.grant("alice", "terminal-1", "large.bin")


class _MutableWorkspaceProvider(WorkspaceProvider):
    kind = "memory"

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.provider_etag = 'W/"fixed-provider-metadata"'
        self.read_requests: list[tuple[int, int, int]] = []

    @property
    def root(self) -> str:
        return "/memory"

    def _entry(self) -> WorkspaceEntry:
        return WorkspaceEntry(
            path="payload.bin",
            name="payload.bin",
            kind="file",
            size=len(self.content),
            modified_at=1.0,
            etag=self.provider_etag,
        )

    def stat(self, path: str = ".") -> WorkspaceEntry:
        return self._entry()

    def list(
        self, path: str = ".", *, max_entries: int = 1_000
    ) -> tuple[WorkspaceEntry, ...]:
        return (self._entry(),)

    def read_range(
        self,
        path: str,
        *,
        start: int = 0,
        end: int | None = None,
        max_bytes: int,
    ) -> WorkspaceRead:
        stop = len(self.content) if end is None else min(end, len(self.content))
        if stop - start > max_bytes:
            raise WorkspaceTooLarge("test read exceeded its bound")
        self.read_requests.append((start, stop, max_bytes))
        return WorkspaceRead(
            entry=self._entry(),
            data=self.content[start:stop],
            start=start,
            end=stop,
        )


class WorkspaceGrantVersionTests(unittest.TestCase):
    def test_grant_etag_adds_a_bounded_probe_and_revalidates_it(self) -> None:
        tokens = iter(("binding", "grant"))
        provider = _MutableWorkspaceProvider(b"abcdefgh")
        service = WorkspaceService(
            sniff_bytes=4,
            max_file_bytes=100,
            max_read_bytes=4,
            token_factory=lambda: next(tokens),
        )
        try:
            service.bind("alice", "terminal-1", provider)
            grant = service.grant("alice", "terminal-1", "payload.bin")
            self.assertEqual(provider.provider_etag, grant.provider_etag)
            self.assertNotEqual(grant.provider_etag, grant.etag)
            self.assertEqual(provider.provider_etag, grant.as_dict()["provider_etag"])

            provider.read_requests.clear()
            response = service.read(
                grant.id, "alice", "terminal-1", range_header="bytes=4-7"
            )
            self.assertEqual(b"efgh", response.body)
            self.assertEqual([(0, 4, 4), (4, 8, 4)], provider.read_requests)

            # Keep the provider's size/mtime-style ETag fixed while changing
            # content. The prefix probe invalidates the grant without reading
            # the requested later range or the whole file.
            provider.content = b"Zbcdefgh"
            provider.read_requests.clear()
            with self.assertRaises(WorkspaceFileChanged):
                service.read(
                    grant.id, "alice", "terminal-1", range_header="bytes=4-7"
                )
            self.assertEqual([(0, 4, 4)], provider.read_requests)
        finally:
            service.close()

    def test_same_metadata_and_prefix_is_not_claimed_as_a_full_snapshot(self) -> None:
        tokens = iter(("binding", "grant"))
        provider = _MutableWorkspaceProvider(b"abcdefgh")
        service = WorkspaceService(
            sniff_bytes=4,
            max_file_bytes=100,
            max_read_bytes=4,
            token_factory=lambda: next(tokens),
        )
        try:
            service.bind("alice", "terminal-1", provider)
            grant = service.grant("alice", "terminal-1", "payload.bin")

            # Models the SFTP v3 edge case where size/whole-second mtime and
            # the bounded prefix all remain unchanged. A FileGrant authorizes
            # a version-checked live file; it is deliberately not represented
            # or tested as a byte-for-byte immutable snapshot.
            provider.content = b"abcdWXYZ"
            response = service.read(
                grant.id, "alice", "terminal-1", range_header="bytes=4-7"
            )
            self.assertEqual(b"WXYZ", response.body)
        finally:
            service.close()


class _FakeAttributes:
    def __init__(self, mode: int, size: int = 0, modified: int = 1, filename: str = ""):
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = modified
        self.filename = filename


class _FakeChannel:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class _FakeSftp:
    def __init__(
        self,
        *,
        root: str = "/workspace",
        normalize_from: str | None = None,
        listing: tuple[_FakeAttributes, ...] = (),
        fail_lstat: Exception | None = None,
    ) -> None:
        self.closed = False
        self.root = root
        self.normalize_from = normalize_from
        self.listing = listing
        self.fail_lstat = fail_lstat
        self.channel = _FakeChannel()
        self.iterated_entries = 0
        self.listdir_attr_called = False

    def get_channel(self) -> _FakeChannel:
        return self.channel

    def normalize(self, path: str) -> str:
        if self.normalize_from is not None and path == self.normalize_from:
            return self.root
        return path

    def stat(self, path: str) -> _FakeAttributes:
        if path == self.root:
            return _FakeAttributes(stat.S_IFDIR | 0o700)
        raise OSError(2, "not found")

    def lstat(self, path: str) -> _FakeAttributes:
        if self.fail_lstat is not None:
            error, self.fail_lstat = self.fail_lstat, None
            raise error
        return self.stat(path)

    def listdir_iter(self, path: str, *, read_aheads: int = 50):
        if path != self.root:
            raise OSError(2, "not found")
        for entry in self.listing:
            self.iterated_entries += 1
            yield entry

    def listdir_attr(self, path: str) -> list[_FakeAttributes]:
        self.listdir_attr_called = True
        return list(self.listing)

    def close(self) -> None:
        self.closed = True


class _FakeSshClient:
    def __init__(self, sftp: _FakeSftp) -> None:
        self.sftp = sftp
        self.loaded = ""
        self.policy = None
        self.connect_kwargs = {}
        self.closed = False

    def load_host_keys(self, path: str) -> None:
        self.loaded = path

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def connect(self, **kwargs) -> None:
        self.connect_kwargs = kwargs

    def open_sftp(self) -> _FakeSftp:
        return self.sftp

    def close(self) -> None:
        self.closed = True


class SftpWorkspaceProviderTests(unittest.TestCase):
    def test_constructor_is_lazy_and_from_device_reuses_existing_ssh_settings(self) -> None:
        provider = SftpWorkspaceProvider(
            "/workspace",
            host="127.0.0.1",
            port=22001,
            username="dev",
            private_key="/does/not/exist",
            known_hosts="/also/missing",
        )
        # Construction and shutdown do not import Paramiko or touch SSH files.
        provider.close()

        configured = SftpWorkspaceProvider.from_device(
            "/srv/project",
            {"remote_port": 22002, "ssh_user": "builder"},
            data_dir="/var/lib/agentserver",
            environ={
                "SSH_PRIVATE_KEY": "/keys/agentserver",
                "SSH_KNOWN_HOSTS": "/keys/known_hosts",
                "SSH_STRICT_HOST_KEY": "yes",
                "FRP_PROXY_HOST": "10.0.0.2",
                "SSH_CONNECT_TIMEOUT": "4.5",
                "SFTP_OPERATION_TIMEOUT": "23",
            },
        )
        self.assertEqual("10.0.0.2", configured.host)
        self.assertEqual(22002, configured.port)
        self.assertEqual("builder", configured.username)
        self.assertEqual(Path("/keys/agentserver"), configured.private_key)
        self.assertEqual(Path("/keys/known_hosts"), configured.known_hosts)
        self.assertEqual("yes", configured.strict_host_key)
        self.assertEqual(4.5, configured.connect_timeout)
        self.assertEqual(23, configured.operation_timeout)
        configured.close()

    def test_first_operation_loads_known_hosts_and_connects_with_key_only(self) -> None:
        directory = tempfile.TemporaryDirectory()
        try:
            root = Path(directory.name)
            private_key = root / "id_ed25519"
            known_hosts = root / "known_hosts"
            private_key.write_text("fake", encoding="utf-8")
            known_hosts.write_text("", encoding="utf-8")
            sftp = _FakeSftp()
            client = _FakeSshClient(sftp)

            class RejectPolicy:
                pass

            class AutoAddPolicy:
                pass

            fake_paramiko = types.SimpleNamespace(
                SSHClient=lambda: client,
                RejectPolicy=RejectPolicy,
                AutoAddPolicy=AutoAddPolicy,
            )
            provider = SftpWorkspaceProvider(
                "/workspace",
                host="127.0.0.1",
                port=22001,
                username="dev",
                private_key=private_key,
                known_hosts=known_hosts,
                strict_host_key="yes",
                operation_timeout=7,
            )
            with patch.dict(sys.modules, {"paramiko": fake_paramiko}):
                entry = provider.stat(".")
            self.assertEqual("directory", entry.kind)
            self.assertEqual(str(known_hosts), client.loaded)
            self.assertIsInstance(client.policy, RejectPolicy)
            self.assertEqual("127.0.0.1", client.connect_kwargs["hostname"])
            self.assertEqual(22001, client.connect_kwargs["port"])
            self.assertEqual("dev", client.connect_kwargs["username"])
            self.assertEqual(str(private_key), client.connect_kwargs["key_filename"])
            self.assertFalse(client.connect_kwargs["allow_agent"])
            self.assertFalse(client.connect_kwargs["look_for_keys"])
            self.assertEqual(7, sftp.channel.timeout)
            provider.close()
            self.assertTrue(client.closed)
            self.assertTrue(sftp.closed)
        finally:
            directory.cleanup()

    def test_operation_failure_discards_connection_and_next_call_reconnects(self) -> None:
        first_sftp = _FakeSftp(fail_lstat=TimeoutError("operation timed out"))
        second_sftp = _FakeSftp()
        first_client = _FakeSshClient(first_sftp)
        second_client = _FakeSshClient(second_sftp)
        connections = iter(
            ((first_client, first_sftp), (second_client, second_sftp))
        )
        provider = SftpWorkspaceProvider(
            "/workspace",
            host="127.0.0.1",
            port=22001,
            username="dev",
            private_key="/unused",
            known_hosts="/unused",
            operation_timeout=3,
            _client_factory=lambda: next(connections),
        )
        try:
            with self.assertRaises(WorkspaceUnavailable):
                provider.stat(".")
            self.assertTrue(first_sftp.closed)
            self.assertTrue(first_client.closed)

            entry = provider.stat(".")
            self.assertEqual("directory", entry.kind)
            self.assertEqual(3, first_sftp.channel.timeout)
            self.assertEqual(3, second_sftp.channel.timeout)
        finally:
            provider.close()
        self.assertTrue(second_sftp.closed)
        self.assertTrue(second_client.closed)

    def test_streaming_listing_stops_after_limit_plus_one(self) -> None:
        listing = tuple(
            _FakeAttributes(stat.S_IFREG | 0o600, size=1, filename=name)
            for name in ("a", "b", "c", "d", "e")
        )
        sftp = _FakeSftp(listing=listing)
        client = _FakeSshClient(sftp)
        provider = SftpWorkspaceProvider(
            "/workspace",
            host="127.0.0.1",
            port=22001,
            username="dev",
            private_key="/unused",
            known_hosts="/unused",
            _client_factory=lambda: (client, sftp),
        )
        try:
            with self.assertRaises(WorkspaceTooLarge):
                provider.list(max_entries=2)
            self.assertEqual(3, sftp.iterated_entries)
            self.assertFalse(sftp.listdir_attr_called)
        finally:
            provider.close()

    def test_lazy_remote_root_normalization_refreshes_public_binding(self) -> None:
        sftp = _FakeSftp(root="/canonical/project", normalize_from=".")
        client = _FakeSshClient(sftp)
        provider = SftpWorkspaceProvider(
            ".",
            host="127.0.0.1",
            port=22001,
            username="dev",
            private_key="/unused",
            known_hosts="/unused",
            _client_factory=lambda: (client, sftp),
        )
        tokens = iter(("binding",))
        service = WorkspaceService(token_factory=lambda: next(tokens))
        try:
            initial = service.bind("alice", "terminal-1", provider)
            self.assertEqual(".", initial.root)

            self.assertEqual((), service.list("alice", "terminal-1"))
            self.assertEqual(
                "/canonical/project",
                service.binding("alice", "terminal-1").root,
            )
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
