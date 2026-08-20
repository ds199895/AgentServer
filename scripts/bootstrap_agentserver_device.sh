#!/usr/bin/env bash
set -euo pipefail

DEVICE_ID=""
BASE_URL=""
RUNTIME_USER=""
RUNTIME_BUILD_SHA="${AGENTSERVER_RUNTIME_BUILD_SHA:-}"
BUNDLE_BASE_URL="${AGENTSERVER_RUNTIME_BUNDLE_BASE_URL:-}"
CURRENT_UID="$(id -u)"
STAGING_DIR=""
MAX_MANIFEST_BYTES=65536
MAX_ARCHIVE_BYTES=$((100 * 1024 * 1024))

die() {
  echo "agentserver-device-bootstrap: $*" >&2
  exit 2
}

usage() {
  cat >&2 <<'EOF'
Download and verify the immutable AgentServer Runtime bundle, then install the device.

Usage:
  bootstrap_agentserver_device.sh --device-id ID --base-url URL [options]

The remaining options are passed unchanged to install_agentserver_device.sh.
Runtime and FRP secrets are read by the child installer from hidden prompts or
0600 files; they are never accepted by this bootstrap script as arguments.
EOF
}

ARGS=("$@")
while [ "$#" -gt 0 ]; do
  case "$1" in
    --device-id) DEVICE_ID="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    --runtime-user) RUNTIME_USER="${2:-}"; shift 2 ;;
    --runtime-build-sha) RUNTIME_BUILD_SHA="${2:-}"; shift 2 ;;
    --runtime-bundle-url) BUNDLE_BASE_URL="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) shift ;;
  esac
done

[[ "$DEVICE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$ ]] || die "invalid --device-id"
[[ "$BASE_URL" =~ ^https://[^[:space:]]+$ || "$BASE_URL" =~ ^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?(/[^[:space:]]*)?$ ]] \
  || die "--base-url must use HTTPS (or loopback HTTP for development)"
command -v python3 >/dev/null 2>&1 || die "Python 3 is required to verify the Runtime bundle"
command -v flock >/dev/null 2>&1 || die "flock is required to serialize Runtime installation"

if [ -z "$RUNTIME_USER" ]; then
  if [ "$CURRENT_UID" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    RUNTIME_USER="$SUDO_USER"
  else
    RUNTIME_USER="$(id -un)"
  fi
fi
printf '%s' "$RUNTIME_USER" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$' || die "invalid --runtime-user"
RUNTIME_PASSWD="$(getent passwd "$RUNTIME_USER" || true)"
[ -n "$RUNTIME_PASSWD" ] || die "Runtime user does not exist: $RUNTIME_USER"
RUNTIME_UID="$(printf '%s\n' "$RUNTIME_PASSWD" | awk -F: '{print $3}')"
RUNTIME_HOME="$(printf '%s\n' "$RUNTIME_PASSWD" | awk -F: '{print $6}')"
[ "$RUNTIME_UID" -ne 0 ] || die "Runtime Host must not use root's HOME"
[ -d "$RUNTIME_HOME" ] || die "Runtime user HOME does not exist: $RUNTIME_HOME"
[ "$CURRENT_UID" -ne 0 ] || die "run the bootstrap as the ordinary Runtime user, without sudo"
[ "$CURRENT_UID" -eq "$RUNTIME_UID" ] || die "run the bootstrap as $RUNTIME_USER"

BUNDLE_BASE_URL="${BUNDLE_BASE_URL:-$BASE_URL}"
[[ "$BUNDLE_BASE_URL" =~ ^https://[^[:space:]]+$ || "$BUNDLE_BASE_URL" =~ ^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?(/[^[:space:]]*)?$ ]] \
  || die "Runtime bundle URL must use HTTPS (or loopback HTTP for development)"

PRIVATE_ROOT="$RUNTIME_HOME/.local/lib/agentserver-runtime"
RELEASE_PARENT="$PRIVATE_ROOT/releases"
VENV_PARENT="$PRIVATE_ROOT/venvs"
[ ! -L "$PRIVATE_ROOT" ] && [ ! -L "$RELEASE_PARENT" ] && [ ! -L "$VENV_PARENT" ] \
  || die "Runtime release directory is unsafe"
install -d -m 0700 "$RELEASE_PARENT"
if [ -L "$VENV_PARENT" ] || { [ -e "$VENV_PARENT" ] && [ ! -d "$VENV_PARENT" ]; }; then
  die "Runtime Python environment directory is unsafe"
fi
install -d -m 0700 "$VENV_PARENT"
chmod 700 "$PRIVATE_ROOT" "$RELEASE_PARENT" "$VENV_PARENT"
PRIVATE_OWNER="$(stat -c '%u' "$PRIVATE_ROOT" 2>/dev/null || true)"
RELEASE_OWNER="$(stat -c '%u' "$RELEASE_PARENT" 2>/dev/null || true)"
VENV_OWNER="$(stat -c '%u' "$VENV_PARENT" 2>/dev/null || true)"
[ ! -L "$PRIVATE_ROOT" ] && [ ! -L "$RELEASE_PARENT" ] && [ ! -L "$VENV_PARENT" ] \
  && [ "$PRIVATE_OWNER" = "$CURRENT_UID" ] && [ "$RELEASE_OWNER" = "$CURRENT_UID" ] \
  && [ "$VENV_OWNER" = "$CURRENT_UID" ] \
  || die "Runtime release directory is unsafe"
LOCK_PATH="$PRIVATE_ROOT/bootstrap.lock"
[ ! -L "$LOCK_PATH" ] || die "bootstrap lock path is unsafe"
umask 077
exec 9>"$LOCK_PATH"
flock -n 9 || die "another Runtime bootstrap is running for this user"

TEMP_BASE=/tmp
if [ -d "/run/user/$CURRENT_UID" ] && [ ! -L "/run/user/$CURRENT_UID" ] \
  && [ "$(stat -c '%u' "/run/user/$CURRENT_UID" 2>/dev/null || true)" = "$CURRENT_UID" ]; then
  TEMP_BASE="/run/user/$CURRENT_UID"
fi
TEMP_DIR="$(mktemp -d "$TEMP_BASE/agentserver-runtime-bootstrap.XXXXXX")"
cleanup() {
  status=$?
  if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
    rm -rf -- "$STAGING_DIR"
  fi
  rm -rf -- "$TEMP_DIR"
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT

download() {
  local url=$1 output=$2 max_bytes=$3 actual_size
  if command -v curl >/dev/null 2>&1; then
    curl --fail --silent --show-error --proto '=https,http' --proto-redir '=https' \
      --connect-timeout 15 --max-time 300 --max-filesize "$max_bytes" \
      -o "$output" "$url"
  else
    python3 - "$url" "$output" "$max_bytes" <<'PY'
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

url, output, raw_limit = sys.argv[1:]
limit = int(raw_limit)


def loopback(hostname):
    return hostname in {"localhost", "127.0.0.1", "::1"}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    redirects = 0

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        self.redirects += 1
        if self.redirects > 5:
            raise urllib.error.HTTPError(new_url, code, "too many redirects", headers, file_pointer)
        old = urllib.parse.urlsplit(request.full_url)
        new = urllib.parse.urlsplit(new_url)
        if new.username or new.password or new.scheme not in {"http", "https"}:
            raise urllib.error.HTTPError(new_url, code, "unsafe redirect", headers, file_pointer)
        if old.scheme == "https" and new.scheme != "https":
            raise urllib.error.HTTPError(new_url, code, "HTTPS downgrade refused", headers, file_pointer)
        if new.scheme == "http" and not loopback(new.hostname):
            raise urllib.error.HTTPError(new_url, code, "non-loopback HTTP refused", headers, file_pointer)
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


opener = urllib.request.build_opener(SafeRedirect())
try:
    with opener.open(url, timeout=30) as response, open(output, "xb") as destination:
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > limit:
            raise ValueError("download exceeds size limit")
        total = 0
        while True:
            chunk = response.read(min(1024 * 1024, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("download exceeds size limit")
            destination.write(chunk)
except Exception:
    try:
        os.unlink(output)
    except FileNotFoundError:
        pass
    raise
PY
  fi
  actual_size="$(wc -c < "$output" | tr -d '[:space:]')"
  [[ "$actual_size" =~ ^[0-9]+$ ]] && [ "$actual_size" -le "$max_bytes" ] || {
    rm -f -- "$output"
    return 1
  }
}

MANIFEST_FILE="$TEMP_DIR/runtime-manifest.json"
download "${BUNDLE_BASE_URL%/}/device-bootstrap/manifest.json" "$MANIFEST_FILE" "$MAX_MANIFEST_BYTES" \
  || die "unable to download Runtime bundle manifest"

read -r BUNDLE_BUILD ARTIFACT_NAME ARTIFACT_SHA ARTIFACT_SIZE < <(python3 - "$MANIFEST_FILE" "$RUNTIME_BUILD_SHA" <<'PY'
import json, re, sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = sys.argv[2]
if not isinstance(payload, dict) or payload.get("schema") != "agentserver.runtime-distribution/1":
    raise SystemExit("invalid Runtime distribution manifest")
build = payload.get("build_sha")
artifact = payload.get("artifact")
digest = payload.get("sha256")
size = payload.get("size")
if not isinstance(build, str) or not re.fullmatch(r"[0-9a-f]{7,64}", build):
    raise SystemExit("invalid Runtime distribution build")
if expected and build != expected:
    raise SystemExit("Runtime distribution build mismatch")
if artifact != f"agentserver-runtime-{build}.tar.gz":
    raise SystemExit("invalid Runtime distribution artifact")
if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit("invalid Runtime distribution checksum")
if not isinstance(size, int) or isinstance(size, bool) or not (0 < size <= 100 * 1024 * 1024):
    raise SystemExit("invalid Runtime distribution size")
print(build, artifact, digest, size)
PY
) || die "invalid Runtime bundle manifest"

ARCHIVE_FILE="$TEMP_DIR/$ARTIFACT_NAME"
download "${BUNDLE_BASE_URL%/}/device-bootstrap/artifacts/$ARTIFACT_NAME" "$ARCHIVE_FILE" "$ARTIFACT_SIZE" \
  || die "unable to download Runtime bundle"
ACTUAL_SHA="$(python3 - "$ARCHIVE_FILE" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
)"
[ "$ACTUAL_SHA" = "$ARTIFACT_SHA" ] || die "Runtime bundle SHA-256 verification failed"
[ "$(wc -c < "$ARCHIVE_FILE" | tr -d '[:space:]')" = "$ARTIFACT_SIZE" ] || die "Runtime bundle size verification failed"

RELEASE_DIR="$RELEASE_PARENT/$BUNDLE_BUILD"
RUNTIME_VENV_DIR="$VENV_PARENT/$BUNDLE_BUILD"
[ ! -L "$RUNTIME_VENV_DIR" ] || die "Runtime Python environment path is unsafe"

inspect_runtime_bundle() {
  local mode=$1 source=$2 destination=$3 expected_build=$4
  python3 - "$mode" "$source" "$destination" "$expected_build" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

mode, source_value, destination_value, expected_build = sys.argv[1:]
source = Path(source_value)
destination = Path(destination_value)
max_members = 4096
max_unpacked_bytes = 512 * 1024 * 1024
max_bundle_manifest_bytes = 4 * 1024 * 1024
safe_file_modes = {0o644, 0o755}


def fail(message):
    raise SystemExit(message)


def relative_path(value):
    if not isinstance(value, str):
        fail("Runtime bundle file path is invalid")
    pure = PurePosixPath(value)
    if (
        not value
        or pure.is_absolute()
        or pure.as_posix() != value
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        fail("Runtime bundle file path is unsafe")
    return pure


def parse_manifest(content):
    if not (0 < len(content) <= max_bundle_manifest_bytes):
        fail("Runtime bundle manifest size is invalid")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("Runtime bundle manifest is invalid")
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "agentserver.runtime-bundle/1"
        or payload.get("build_sha") != expected_build
    ):
        fail("Runtime bundle manifest identity is invalid")
    entries = payload.get("files")
    if not isinstance(entries, list) or not (1 <= len(entries) < max_members):
        fail("Runtime bundle file manifest is invalid")
    expected = {}
    total_size = 0
    for item in entries:
        if not isinstance(item, dict):
            fail("Runtime bundle file manifest is invalid")
        relative = relative_path(item.get("path"))
        name = relative.as_posix()
        if name == "RUNTIME_MANIFEST.json" or name in expected:
            fail("Runtime bundle file path is unsafe")
        digest = item.get("sha256")
        size = item.get("size")
        raw_mode = item.get("mode")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            fail("Runtime bundle file checksum is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or not (0 <= size <= max_unpacked_bytes):
            fail("Runtime bundle file size is invalid")
        if not isinstance(raw_mode, str) or re.fullmatch(r"0[0-7]{3}", raw_mode) is None:
            fail("Runtime bundle file mode is invalid")
        file_mode = int(raw_mode, 8)
        if file_mode not in safe_file_modes:
            fail("Runtime bundle file mode is unsafe")
        total_size += size
        if total_size > max_unpacked_bytes:
            fail("Runtime bundle expands beyond the size limit")
        expected[name] = {"sha256": digest, "size": size, "mode": file_mode}
    installer = expected.get("scripts/install_agentserver_device.sh")
    if installer is None or installer["mode"] != 0o755:
        fail("Runtime bundle installer is missing or not executable")
    build_content = f"{expected_build}\n".encode()
    build_entry = expected.get("BUILD_SHA")
    if build_entry is None or build_entry["size"] != len(build_content) or build_entry["sha256"] != hashlib.sha256(build_content).hexdigest():
        fail("Runtime bundle build identity is invalid")
    return expected


def hash_stream(stream, output=None):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > max_unpacked_bytes:
            fail("Runtime bundle expands beyond the size limit")
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    return size, digest.hexdigest()


def verify_private_directory(path):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        fail("existing Runtime release is incomplete")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("existing Runtime release path is unsafe")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        fail("existing Runtime release ownership or mode is unsafe")


def verify_parents(root, relative):
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        verify_private_directory(current)


def verify_release():
    verify_private_directory(source)
    manifest_path = source / "RUNTIME_MANIFEST.json"
    try:
        metadata = os.lstat(manifest_path)
    except FileNotFoundError:
        fail("existing Runtime release manifest is missing")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not (0 < metadata.st_size <= max_bundle_manifest_bytes)
    ):
        fail("existing Runtime release manifest is unsafe")
    expected = parse_manifest(manifest_path.read_bytes())
    expected_files = set(expected) | {"RUNTIME_MANIFEST.json"}
    expected_dirs = {PurePosixPath(".")}
    for name in expected_files:
        expected_dirs.update(PurePosixPath(name).parents)
    # A release is immutable after extraction.  Checking only the files named
    # by the manifest would allow an extra same-UID file to survive a rerun.
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        root_relative = PurePosixPath(root_path.relative_to(source).as_posix())
        for directory in list(directories):
            target = root_path / directory
            relative = PurePosixPath((root_path / directory).relative_to(source).as_posix())
            if target.is_symlink() or relative not in expected_dirs:
                fail("existing Runtime release contains an unexpected path")
            verify_private_directory(target)
        for filename in files:
            target = root_path / filename
            relative = PurePosixPath(target.relative_to(source).as_posix())
            if target.is_symlink() or relative.as_posix() not in expected_files:
                fail("existing Runtime release contains an unexpected path")
    for name, item in expected.items():
        relative = relative_path(name)
        verify_parents(source, relative)
        target = source.joinpath(*relative.parts)
        try:
            metadata = os.lstat(target)
        except FileNotFoundError:
            fail("existing Runtime release is incomplete")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != item["mode"]
            or metadata.st_size != item["size"]
        ):
            fail("existing Runtime release file metadata failed verification")
        with target.open("rb") as stream:
            actual_size, actual_digest = hash_stream(stream)
        if actual_size != item["size"] or actual_digest != item["sha256"]:
            fail("existing Runtime release file checksum failed")


def extract_archive():
    regular = {}
    seen = set()
    root_seen = False
    member_count = 0
    declared_size = 0
    try:
        archive_context = tarfile.open(source, "r:gz")
    except (OSError, tarfile.TarError):
        fail("Runtime bundle archive is invalid")
    with archive_context as archive:
        for member in archive:
            member_count += 1
            if member_count > max_members:
                fail("Runtime bundle contains too many members")
            name = member.name
            pure = PurePosixPath(name)
            if pure.is_absolute() or pure.as_posix() != name or any(part in ("", ".", "..") for part in pure.parts):
                fail("Runtime bundle contains an unsafe path")
            if name != "agentserver-runtime" and not name.startswith("agentserver-runtime/"):
                fail("Runtime bundle contains an unsafe path")
            if name in seen:
                fail("Runtime bundle contains duplicate members")
            seen.add(name)
            member_mode = stat.S_IMODE(member.mode)
            if member.isdir():
                if member_mode not in {0o700, 0o755}:
                    fail("Runtime bundle directory mode is unsafe")
                root_seen = root_seen or name == "agentserver-runtime"
                continue
            if not member.isreg() or member.issym() or member.islnk() or member.isdev() or member.sparse is not None:
                fail("Runtime bundle contains an unsafe member")
            if member_mode not in safe_file_modes:
                fail("Runtime bundle file mode is unsafe")
            relative = pure.relative_to("agentserver-runtime").as_posix()
            if relative in regular:
                fail("Runtime bundle contains duplicate files")
            declared_size += member.size
            if member.size < 0 or declared_size > max_unpacked_bytes:
                fail("Runtime bundle expands beyond the size limit")
            regular[relative] = member
        if not root_seen:
            fail("Runtime bundle root directory is missing")
        manifest_member = regular.get("RUNTIME_MANIFEST.json")
        if manifest_member is None or stat.S_IMODE(manifest_member.mode) != 0o644 or manifest_member.size > max_bundle_manifest_bytes:
            fail("Runtime bundle manifest is missing or unsafe")
        manifest_stream = archive.extractfile(manifest_member)
        if manifest_stream is None:
            fail("Runtime bundle manifest is unreadable")
        manifest_bytes = manifest_stream.read(max_bundle_manifest_bytes + 1)
        expected = parse_manifest(manifest_bytes)
        if set(expected) != set(regular) - {"RUNTIME_MANIFEST.json"}:
            fail("Runtime bundle file manifest does not match archive")
        for name, item in expected.items():
            member = regular[name]
            if member.size != item["size"] or stat.S_IMODE(member.mode) != item["mode"]:
                fail("Runtime bundle file metadata failed verification")
            relative = relative_path(name)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                fail("Runtime bundle file is unreadable")
            with target.open("xb") as output:
                actual_size, actual_digest = hash_stream(stream, output)
            target.chmod(item["mode"])
            if actual_size != item["size"] or actual_digest != item["sha256"]:
                fail("Runtime bundle file checksum failed")
        manifest_target = destination / "RUNTIME_MANIFEST.json"
        with manifest_target.open("xb") as output:
            output.write(manifest_bytes)
        manifest_target.chmod(0o600)


if mode == "verify":
    verify_release()
elif mode == "extract":
    extract_archive()
else:
    fail("unsupported Runtime bundle inspection mode")
PY
}

if [ -e "$RELEASE_DIR" ] || [ -L "$RELEASE_DIR" ]; then
  [ -d "$RELEASE_DIR" ] && [ ! -L "$RELEASE_DIR" ] || die "existing Runtime release path is unsafe"
  inspect_runtime_bundle verify "$RELEASE_DIR" "$RELEASE_DIR" "$BUNDLE_BUILD" \
    || die "existing Runtime release failed integrity verification"
else
  STAGING_DIR="$(mktemp -d "$RELEASE_PARENT/.staging.XXXXXX")"
  chmod 700 "$STAGING_DIR"
  inspect_runtime_bundle extract "$ARCHIVE_FILE" "$STAGING_DIR" "$BUNDLE_BUILD" \
    || die "Runtime bundle extraction failed integrity verification"
  mv -- "$STAGING_DIR" "$RELEASE_DIR"
  STAGING_DIR=""
fi

export AGENTSERVER_RUNTIME_BUNDLE_READY=1
export AGENTSERVER_RUNTIME_BUILD_SHA="$BUNDLE_BUILD"
export AGENTSERVER_RUNTIME_VENV_DIR="$RUNTIME_VENV_DIR"
exec "$RELEASE_DIR/scripts/install_agentserver_device.sh" "${ARGS[@]}"
