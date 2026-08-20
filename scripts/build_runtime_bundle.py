#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


BUILD_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
SCRIPT_FILES = (
    "scripts/agentserver_runtime.py",
    "scripts/install_agentserver_device.sh",
    "scripts/install_agentserver_runtime.sh",
    "scripts/install_frpc_ssh.sh",
)
STATIC_FILES = ("app/__init__.py", "requirements-runtime.lock")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def source_payload(source: Path, wheelhouse: Path | None) -> dict[str, bytes]:
    paths = [source / relative for relative in (*STATIC_FILES, *SCRIPT_FILES)]
    paths.extend(sorted((source / "app" / "execution").rglob("*.py")))
    payload: dict[str, bytes] = {}
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"runtime bundle source is missing or unsafe: {path}")
        relative = path.relative_to(source).as_posix()
        payload[relative] = path.read_bytes()
    if wheelhouse is not None:
        wheels = sorted(wheelhouse.glob("*.whl"))
        if not wheels:
            raise RuntimeError("runtime wheelhouse has no wheels")
        for wheel in wheels:
            if not wheel.is_file() or wheel.is_symlink():
                raise RuntimeError(f"runtime wheel is unsafe: {wheel}")
            payload[f"wheelhouse/{wheel.name}"] = wheel.read_bytes()
    return payload


def content_build_sha(payload: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(payload.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def file_mode(name: str) -> int:
    return 0o755 if name in SCRIPT_FILES else 0o644


def tar_info(name: str, *, mode: int, size: int = 0, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = mode
    info.size = 0 if directory else size
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    return info


def build_archive(payload: dict[str, bytes], build_sha: str) -> bytes:
    files = dict(payload)
    files["BUILD_SHA"] = f"{build_sha}\n".encode()
    manifest = {
        "schema": "agentserver.runtime-bundle/1",
        "build_sha": build_sha,
        "files": [
            {
                "path": name,
                "sha256": sha256_bytes(content),
                "size": len(content),
                "mode": format(file_mode(name), "04o"),
            }
            for name, content in sorted(files.items())
        ],
    }
    files["RUNTIME_MANIFEST.json"] = (
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()

    directories = {PurePosixPath("agentserver-runtime")}
    for name in files:
        path = PurePosixPath("agentserver-runtime") / name
        directories.update(parent for parent in path.parents if parent != PurePosixPath("."))

    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for directory in sorted(directories, key=lambda item: (len(item.parts), item.as_posix())):
                archive.addfile(tar_info(directory.as_posix(), mode=0o755, directory=True))
            for name, content in sorted(files.items()):
                archive_name = f"agentserver-runtime/{name}"
                archive.addfile(
                    tar_info(archive_name, mode=file_mode(name), size=len(content)),
                    io.BytesIO(content),
                )
    return output.getvalue()


def atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the immutable AgentServer device Runtime bundle")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--build-sha")
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    payload = source_payload(source, args.wheelhouse.resolve() if args.wheelhouse else None)
    build_sha = args.build_sha or content_build_sha(payload)
    if not BUILD_PATTERN.fullmatch(build_sha):
        raise SystemExit("--build-sha must contain 7-64 lowercase hexadecimal characters")

    archive = build_archive(payload, build_sha)
    digest = sha256_bytes(archive)
    artifact_name = f"agentserver-runtime-{build_sha}.tar.gz"
    output_dir = args.output_dir.resolve()
    artifact_path = output_dir / artifact_name
    manifest_path = output_dir / "runtime-manifest.json"
    external_manifest = {
        "schema": "agentserver.runtime-distribution/1",
        "build_sha": build_sha,
        "artifact": artifact_name,
        "sha256": digest,
        "size": len(archive),
    }

    atomic_write(artifact_path, archive)
    atomic_write(
        artifact_path.with_suffix(artifact_path.suffix + ".sha256"),
        f"{digest}  {artifact_name}\n".encode(),
    )
    atomic_write(
        manifest_path,
        (json.dumps(external_manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    print(artifact_path)


if __name__ == "__main__":
    main()
