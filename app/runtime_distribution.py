from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BUILD_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
ARTIFACT_PATTERN = re.compile(r"^agentserver-runtime-([0-9a-f]{7,64})\.tar\.gz$")


@dataclass(frozen=True)
class RuntimeDistribution:
    build_sha: str
    artifact_name: str
    artifact_path: Path
    sha256: str
    size: int
    manifest_bytes: bytes


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"runtime distribution manifest has no {key}")
    return value


def load_runtime_distribution(
    directory: Path,
    *,
    expected_build_sha: str | None = None,
) -> RuntimeDistribution:
    manifest_path = directory / "runtime-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    try:
        payload = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime distribution manifest is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "agentserver.runtime-distribution/1":
        raise ValueError("runtime distribution manifest schema is invalid")

    build_sha = _required_string(payload, "build_sha")
    artifact_name = _required_string(payload, "artifact")
    expected_sha256 = _required_string(payload, "sha256")
    expected_size = payload.get("size")
    match = ARTIFACT_PATTERN.fullmatch(artifact_name)
    if not BUILD_PATTERN.fullmatch(build_sha) or match is None or match.group(1) != build_sha:
        raise ValueError("runtime distribution build identity is invalid")
    if expected_build_sha is not None and build_sha != expected_build_sha:
        raise ValueError("runtime distribution does not match the server build")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("runtime distribution checksum is invalid")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 1:
        raise ValueError("runtime distribution size is invalid")

    artifact_path = directory / artifact_name
    if artifact_path.parent != directory or not artifact_path.is_file() or artifact_path.is_symlink():
        raise ValueError("runtime distribution artifact is unavailable")
    content = artifact_path.read_bytes()
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("runtime distribution artifact failed integrity validation")
    return RuntimeDistribution(
        build_sha=build_sha,
        artifact_name=artifact_name,
        artifact_path=artifact_path,
        sha256=expected_sha256,
        size=expected_size,
        manifest_bytes=manifest_bytes,
    )
