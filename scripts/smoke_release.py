#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


def fetch(url: str, opener: urllib.request.OpenerDirector | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    client = opener or urllib.request.build_opener()
    with client.open(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def authenticated_opener(base_url: str, env_file: Path) -> urllib.request.OpenerDirector:
    values = parse_env(env_file)
    payload = json.dumps({
        "username": values.get("ADMIN_USERNAME", "admin"),
        "password": values.get("ADMIN_PASSWORD", ""),
    }).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    request = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"production login returned HTTP {response.status}")
    return opener


def check(base_url: str, expected_sha: str, env_file: Path | None = None) -> None:
    version = json.loads(fetch(f"{base_url}/api/version"))
    if version.get("build_sha") != expected_sha:
        raise RuntimeError(f"backend build mismatch: {version!r}")
    manifest = json.loads(fetch(f"{base_url}/build.json"))
    if manifest.get("build_sha") != expected_sha:
        raise RuntimeError(f"frontend build mismatch: {manifest!r}")
    html = fetch(f"{base_url}/").decode("utf-8")
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
    if not assets:
        raise RuntimeError("frontend index has no assets")
    for asset in assets:
        fetch(f"{base_url}{asset}")
    if env_file is not None:
        opener = authenticated_opener(base_url, env_file)
        sessions = json.loads(fetch(f"{base_url}/api/terminals", opener))
        if not isinstance(sessions, list):
            raise RuntimeError("terminal API did not return a list")
        missing = sum(not isinstance(item.get("services"), list) for item in sessions)
        if missing:
            raise RuntimeError(f"{missing} terminal payloads have no services array")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18100")
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--attempts", type=int, default=20)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    error: Exception | None = None
    for _ in range(max(1, args.attempts)):
        try:
            check(base_url, args.expected_sha, args.env_file)
            print(f"release smoke passed: {args.expected_sha}")
            return
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            error = exc
            time.sleep(1)
    raise SystemExit(f"release smoke failed: {error}")


if __name__ == "__main__":
    main()
