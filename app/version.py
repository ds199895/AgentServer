from __future__ import annotations

import json
import os
import re
from pathlib import Path


DEVELOPMENT_BUILD = "development"
BUILD_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")


def resolve_build_sha(root: Path) -> str:
    configured = os.getenv("AGENTSERVER_BUILD_SHA", "").strip()
    if configured:
        return configured
    marker = root / "BUILD_SHA"
    if marker.is_file():
        return marker.read_text(encoding="utf-8").strip()
    return DEVELOPMENT_BUILD


def frontend_build_sha(web_dist: Path) -> str:
    manifest = web_dist / "build.json"
    if not manifest.is_file():
        return DEVELOPMENT_BUILD
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid frontend build manifest: {manifest}") from exc
    build_sha = payload.get("build_sha")
    if not isinstance(build_sha, str) or not build_sha.strip():
        raise RuntimeError(f"Frontend build manifest has no build_sha: {manifest}")
    return build_sha.strip()


def verify_release_pair(build_sha: str, web_dist: Path, *, production: bool) -> str:
    frontend_sha = frontend_build_sha(web_dist)
    if not production:
        return frontend_sha
    if not BUILD_SHA_PATTERN.fullmatch(build_sha):
        raise RuntimeError("Production backend BUILD_SHA is missing or invalid")
    if frontend_sha != build_sha:
        raise RuntimeError(
            f"Frontend/backend build mismatch: frontend={frontend_sha}, backend={build_sha}"
        )
    return frontend_sha
