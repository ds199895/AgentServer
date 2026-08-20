from __future__ import annotations

import gzip
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.runtime_distribution import load_runtime_distribution


ROOT = Path(__file__).resolve().parents[1]
BUILD_SHA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def write_distribution(directory: Path, archive_bytes: bytes, build_sha: str = BUILD_SHA) -> None:
    artifact_name = f"agentserver-runtime-{build_sha}.tar.gz"
    artifact = directory / artifact_name
    artifact.write_bytes(archive_bytes)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    (directory / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": "agentserver.runtime-distribution/1",
                "build_sha": build_sha,
                "artifact": artifact_name,
                "sha256": digest,
                "size": len(archive_bytes),
            }
        ),
        encoding="utf-8",
    )


def make_bootstrap_archive(
    *,
    marker_name: str = "bootstrap-marker",
    unsafe_member: str | None = None,
    installer_mode: int = 0o755,
    extra_members: int = 0,
) -> bytes:
    files = {
        "scripts/install_agentserver_device.sh": (
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "printf '%s\\n' \"$*\" > \"$BOOTSTRAP_MARKER\"\n"
            "printf 'runtime-venv=%s\\n' \"${AGENTSERVER_RUNTIME_VENV_DIR:-}\" >> \"$BOOTSTRAP_MARKER\"\n"
        ).encode(),
        "app/execution/placeholder.py": b"# runtime fixture\n",
    }
    files["BUILD_SHA"] = f"{BUILD_SHA}\n".encode()
    def mode_for(name: str) -> int:
        if name == "scripts/install_agentserver_device.sh":
            return installer_mode
        return 0o755 if name.endswith(".sh") else 0o644

    manifest_entries = [
        {
            "path": name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "mode": format(mode_for(name), "04o"),
        }
        for name, content in sorted(files.items())
    ]
    manifest = {
        "schema": "agentserver.runtime-bundle/1",
        "build_sha": BUILD_SHA,
        "files": manifest_entries,
    }
    files["RUNTIME_MANIFEST.json"] = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    output = tempfile.SpooledTemporaryFile()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            root_info = tarfile.TarInfo("agentserver-runtime")
            root_info.type = tarfile.DIRTYPE
            root_info.mode = 0o755
            archive.addfile(root_info)
            for name, content in sorted(files.items()):
                info = tarfile.TarInfo(f"agentserver-runtime/{name}")
                info.mode = mode_for(name)
                info.size = len(content)
                archive.addfile(info, __import__("io").BytesIO(content))
            for index in range(extra_members):
                info = tarfile.TarInfo(f"agentserver-runtime/extras/{index:04d}")
                info.mode = 0o644
                archive.addfile(info, __import__("io").BytesIO())
            if unsafe_member is not None:
                info = tarfile.TarInfo(unsafe_member)
                info.size = 4
                archive.addfile(info, __import__("io").BytesIO(b"evil"))
    output.seek(0)
    return output.read()


class RuntimeDistributionTests(unittest.TestCase):
    def test_builder_is_deterministic_and_manifest_matches_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            command = [
                "python3",
                str(ROOT / "scripts/build_runtime_bundle.py"),
                "--source",
                str(ROOT),
                "--output-dir",
                str(first),
                "--build-sha",
                BUILD_SHA,
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            command[command.index(str(first))] = str(second)
            subprocess.run(command, check=True, capture_output=True, text=True)
            first_archive = next(first.glob("*.tar.gz"))
            second_archive = next(second.glob("*.tar.gz"))
            self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
            with tarfile.open(first_archive, "r:gz") as archive:
                names = archive.getnames()
                self.assertTrue(all("__pycache__" not in name for name in names))
                self.assertTrue(all(not member.issym() and not member.islnk() for member in archive.getmembers()))
                manifest = json.loads(archive.extractfile("agentserver-runtime/RUNTIME_MANIFEST.json").read())
                self.assertEqual(BUILD_SHA, manifest["build_sha"])
                self.assertNotIn("enrollment", archive.getnames())
            distribution = load_runtime_distribution(first, expected_build_sha=BUILD_SHA)
            self.assertEqual(first_archive, distribution.artifact_path)

    def test_distribution_rejects_checksum_and_build_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_distribution(directory, b"archive")
            distribution = load_runtime_distribution(directory, expected_build_sha=BUILD_SHA)
            self.assertEqual(7, distribution.size)
            artifact = directory / distribution.artifact_name
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "integrity"):
                load_runtime_distribution(directory, expected_build_sha=BUILD_SHA)


class BootstrapHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.distribution = self.root / "distribution"
        self.distribution.mkdir()
        write_distribution(self.distribution, make_bootstrap_archive())
        self.home = self.root / "runtime-home"
        self.home.mkdir()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        getent = self.fake_bin / "getent"
        getent.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = passwd ] && [ \"${2:-}\" = testuser ]; then\n"
            f"  printf 'testuser:x:1000:1000::%s:/bin/sh\\n' {shlex_quote(str(self.home))}\n"
            "  exit 0\n"
            "fi\n"
            "exec /usr/bin/getent \"$@\"\n",
            encoding="utf-8",
        )
        getent.chmod(0o755)
        self.marker = self.root / "marker"

        handler_root = self.distribution

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                relative = self.path.lstrip("/")
                if relative == "device-bootstrap/manifest.json":
                    relative = "runtime-manifest.json"
                elif relative.startswith("device-bootstrap/artifacts/"):
                    relative = relative.removeprefix("device-bootstrap/artifacts/")
                path = handler_root / relative
                if not path.is_file() or path.parent != handler_root:
                    self.send_error(404)
                    return
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def run_bootstrap(self, *, artifact_override: bytes | None = None) -> subprocess.CompletedProcess[str]:
        if artifact_override is not None:
            artifact = next(self.distribution.glob("*.tar.gz"))
            artifact.write_bytes(artifact_override)
        env = {
            **os.environ,
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "BOOTSTRAP_MARKER": str(self.marker),
        }
        url = f"http://127.0.0.1:{self.server.server_port}"
        return subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/bootstrap_agentserver_device.sh"),
                "--device-id",
                "device-01",
                "--base-url",
                url,
                "--runtime-bundle-url",
                url,
                "--runtime-build-sha",
                BUILD_SHA,
                "--runtime-user",
                "testuser",
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_bootstrap_downloads_extracts_and_executes_child_without_checkout(self) -> None:
        result = self.run_bootstrap()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.marker.is_file())
        self.assertIn("--device-id device-01", self.marker.read_text(encoding="utf-8"))
        release = self.home / ".local/lib/agentserver-runtime/releases" / BUILD_SHA
        self.assertTrue((release / "scripts/install_agentserver_device.sh").is_file())
        runtime_venv = self.home / ".local/lib/agentserver-runtime/venvs" / BUILD_SHA
        self.assertIn(f"runtime-venv={runtime_venv}", self.marker.read_text(encoding="utf-8"))

    def test_bootstrap_rejects_tampered_archive_before_child(self) -> None:
        artifact = next(self.distribution.glob("*.tar.gz"))
        tampered = bytearray(artifact.read_bytes())
        tampered[-1] ^= 1
        result = self.run_bootstrap(artifact_override=bytes(tampered))

        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.marker.exists())

    def test_bootstrap_rejects_archive_path_traversal_before_child(self) -> None:
        archive = make_bootstrap_archive(unsafe_member="agentserver-runtime/../outside")
        write_distribution(self.distribution, archive)

        result = self.run_bootstrap()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unsafe path", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.home / ".local/lib/agentserver-runtime/outside").exists())

    def test_bootstrap_revalidates_an_existing_release_before_execution(self) -> None:
        first = self.run_bootstrap()
        self.assertEqual(0, first.returncode, first.stderr)
        self.marker.unlink()
        installer = (
            self.home
            / ".local/lib/agentserver-runtime/releases"
            / BUILD_SHA
            / "scripts/install_agentserver_device.sh"
        )
        tampered = bytearray(installer.read_bytes())
        tampered[-1] ^= 1
        installer.write_bytes(bytes(tampered))

        second = self.run_bootstrap()

        self.assertNotEqual(0, second.returncode)
        self.assertIn("checksum failed", second.stderr)
        self.assertFalse(self.marker.exists())

    def test_bootstrap_rejects_an_extra_file_in_an_existing_release(self) -> None:
        first = self.run_bootstrap()
        self.assertEqual(0, first.returncode, first.stderr)
        self.marker.unlink()
        release = self.home / ".local/lib/agentserver-runtime/releases" / BUILD_SHA
        (release / "unexpected.py").write_text("# not in the manifest\n", encoding="utf-8")

        second = self.run_bootstrap()

        self.assertNotEqual(0, second.returncode)
        self.assertIn("unexpected path", second.stderr)
        self.assertFalse(self.marker.exists())

    def test_bootstrap_rejects_privileged_archive_modes(self) -> None:
        write_distribution(
            self.distribution,
            make_bootstrap_archive(installer_mode=0o4755),
        )

        result = self.run_bootstrap()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("mode is unsafe", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_bootstrap_rejects_archives_with_excessive_member_count(self) -> None:
        write_distribution(
            self.distribution,
            make_bootstrap_archive(extra_members=4092),
        )

        result = self.run_bootstrap()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("too many members", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_bootstrap_limits_distribution_manifest_download_size(self) -> None:
        (self.distribution / "runtime-manifest.json").write_bytes(b" " * 65537)

        result = self.run_bootstrap()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unable to download Runtime bundle manifest", result.stderr)
        self.assertFalse(self.marker.exists())


class RuntimeDistributionEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_bootstrap_endpoints_are_anonymous_and_immutable(self) -> None:
        os.environ.setdefault("ADMIN_PASSWORD", "test-only-password")
        os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="runtime-endpoint-test-"))
        import httpx
        from app.main import app

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_distribution(directory, b"runtime-archive")
            with patch("app.main.RUNTIME_DIST", directory):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    bootstrap = await client.get("/device-bootstrap/install.sh")
                    manifest = await client.get("/device-bootstrap/manifest.json")
                    artifact_name = json.loads(manifest.text)["artifact"]
                    artifact = await client.get(f"/device-bootstrap/artifacts/{artifact_name}")

            self.assertEqual(200, bootstrap.status_code)
            self.assertIn("bootstrap", bootstrap.text)
            self.assertEqual(200, manifest.status_code)
            self.assertEqual("no-cache", manifest.headers.get("cache-control"))
            self.assertEqual(200, artifact.status_code)
            self.assertEqual("public, max-age=31536000, immutable", artifact.headers.get("cache-control"))
            self.assertEqual(manifest.json()["sha256"], artifact.headers.get("x-checksum-sha256"))
            self.assertEqual(b"runtime-archive", artifact.content)


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    unittest.main()
