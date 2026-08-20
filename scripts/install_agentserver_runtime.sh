#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEVICE_ID=""
BASE_URL=""
ENROLLMENT_TOKEN_FILE=""
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agentserver-runtime"
CODEX_BIN=""
NODE_BIN=""
BWRAP_BIN=""
PYTHON_BINARY_ARG=""
REENROLL=0
REQUIRE_SYSTEMD=0
PREFLIGHT_ONLY=0
UNIT_NAME="agentserver-runtime.service"
USER_SYSTEMD_AVAILABLE=0
SERVICE_WAS_ACTIVE=0
SERVICE_START_ATTEMPTED=0
INSTALL_COMPLETE=0
TEMP_UNIT=""
UNIT_PATH=""
UNIT_BACKUP=""
UNIT_REPLACED=0

cleanup() {
  status=$?
  if [ -n "$TEMP_UNIT" ]; then
    rm -f -- "$TEMP_UNIT"
  fi
  if [ "$INSTALL_COMPLETE" -ne 1 ] && [ "$SERVICE_START_ATTEMPTED" -eq 1 ]; then
    systemctl --user stop "$UNIT_NAME" >/dev/null 2>&1 || true
  fi
  if [ "$INSTALL_COMPLETE" -ne 1 ] && [ "$UNIT_REPLACED" -eq 1 ] && [ -n "$UNIT_PATH" ]; then
    if [ -n "$UNIT_BACKUP" ] && [ -f "$UNIT_BACKUP" ]; then
      mv -f -- "$UNIT_BACKUP" "$UNIT_PATH"
    else
      rm -f -- "$UNIT_PATH"
    fi
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  if [ -n "$UNIT_BACKUP" ] && [ -f "$UNIT_BACKUP" ]; then
    rm -f -- "$UNIT_BACKUP"
  fi
  if [ "$INSTALL_COMPLETE" -ne 1 ] && [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
    systemctl --user start "$UNIT_NAME" >/dev/null 2>&1 || true
  fi
  trap - EXIT
  exit "$status"
}

trap cleanup EXIT

usage() {
  echo "Usage: $0 --device-id ID --base-url URL [--enrollment-token-file FILE] [--state-dir DIR] [--codex-binary PATH] [--node-binary PATH] [--bubblewrap-binary PATH] [--python-binary PATH] [--reenroll] [--require-systemd] [--preflight-only]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --device-id) DEVICE_ID="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    --enrollment-token-file) ENROLLMENT_TOKEN_FILE="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --codex-binary) CODEX_BIN="${2:-}"; shift 2 ;;
    --node-binary) NODE_BIN="${2:-}"; shift 2 ;;
    --bubblewrap-binary) BWRAP_BIN="${2:-}"; shift 2 ;;
    --python-binary) PYTHON_BINARY_ARG="${2:-}"; shift 2 ;;
    --reenroll) REENROLL=1; shift ;;
    --require-systemd) REQUIRE_SYSTEMD=1; shift ;;
    --preflight-only) PREFLIGHT_ONLY=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

if [[ ! "$DEVICE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$ ]]; then
  echo "invalid --device-id" >&2
  exit 2
fi
if [[ ! "$BASE_URL" =~ ^https://[^[:space:]]+$ && ! "$BASE_URL" =~ ^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?(/[^[:space:]]*)?$ ]]; then
  echo "--base-url must use HTTPS (or loopback HTTP for development)" >&2
  exit 2
fi
if [[ "$STATE_DIR" != /* ]]; then
  STATE_DIR="$(pwd -P)/$STATE_DIR"
fi
EXISTING_CREDENTIAL="$STATE_DIR/device.credential"
if [ "$PREFLIGHT_ONLY" -ne 1 ] && [ ! -f "$EXISTING_CREDENTIAL" ] && { [ -z "$ENROLLMENT_TOKEN_FILE" ] || [ ! -f "$ENROLLMENT_TOKEN_FILE" ]; }; then
  echo "--enrollment-token-file is required for the first enrollment" >&2
  exit 2
fi
if [ "$REENROLL" -eq 1 ] && { [ -z "$ENROLLMENT_TOKEN_FILE" ] || [ ! -f "$ENROLLMENT_TOKEN_FILE" ]; }; then
  echo "--reenroll requires --enrollment-token-file" >&2
  exit 2
fi

read_private_binding() {
  local path=$1
  local mode owner size value
  if [ -L "$path" ] || [ ! -f "$path" ]; then
    echo "runtime bootstrap binding must be a regular file" >&2
    return 1
  fi
  mode="$(stat -c '%a' "$path" 2>/dev/null || true)"
  owner="$(stat -c '%u' "$path" 2>/dev/null || true)"
  size="$(stat -c '%s' "$path" 2>/dev/null || true)"
  if [ "$mode" != 600 ] || [ "$owner" != "$(id -u)" ]; then
    echo "runtime bootstrap binding must be owned by this uid with mode 0600" >&2
    return 1
  fi
  if [[ ! "$size" =~ ^[0-9]+$ ]] || [ "$size" -lt 1 ] || [ "$size" -gt 4096 ]; then
    echo "runtime bootstrap binding is invalid" >&2
    return 1
  fi
  value="$(cat -- "$path")"
  if [ -z "$value" ] || [[ "$value" =~ [[:space:]] ]]; then
    echo "runtime bootstrap binding is invalid" >&2
    return 1
  fi
  printf '%s' "$value"
}

set_state_paths() {
  EXISTING_CREDENTIAL="$STATE_DIR/device.credential"
  BINDING_DEVICE_FILE="$STATE_DIR/bootstrap.device_id"
  BINDING_URL_FILE="$STATE_DIR/bootstrap.base_url"
}

validate_existing_state() {
  if [ -L "$STATE_DIR" ] || { [ -e "$STATE_DIR" ] && [ ! -d "$STATE_DIR" ]; }; then
    echo "runtime state directory must be a real directory" >&2
    return 1
  fi
  [ -d "$STATE_DIR" ] || return 0
  if [ "$(stat -c '%u' "$STATE_DIR" 2>/dev/null || true)" != "$(id -u)" ]; then
    echo "runtime state directory must be owned by this uid" >&2
    return 1
  fi
  STATE_DIR="$(cd "$STATE_DIR" && pwd -P)"
  if [[ "$STATE_DIR" == *$'\n'* || "$STATE_DIR" == *$'\r'* || "$STATE_DIR" == *'%'* || "$STATE_DIR" == *'"'* || "$STATE_DIR" == *'\'* ]]; then
    echo "resolved state directory is unsafe in a systemd unit" >&2
    return 1
  fi
  set_state_paths
  if [ -e "$BINDING_DEVICE_FILE" ] || [ -L "$BINDING_DEVICE_FILE" ]; then
    bound_device="$(read_private_binding "$BINDING_DEVICE_FILE")"
    if [ "$bound_device" != "$DEVICE_ID" ]; then
      echo "state directory is already bound to device $bound_device" >&2
      return 1
    fi
  fi
  if [ -e "$BINDING_URL_FILE" ] || [ -L "$BINDING_URL_FILE" ]; then
    bound_url="$(read_private_binding "$BINDING_URL_FILE")"
    if [ "$bound_url" != "$BASE_URL" ]; then
      echo "state directory is already bound to a different AgentServer URL" >&2
      return 1
    fi
  fi
  if { [ -e "$EXISTING_CREDENTIAL" ] || [ -L "$EXISTING_CREDENTIAL" ]; } && [ "$REENROLL" -ne 1 ]; then
    if ! (cd "$ROOT_DIR" && "$PYTHON_BIN" - "$EXISTING_CREDENTIAL" <<'PY'
import sys
from app.execution.runtime_host import load_private_text_file
load_private_text_file(sys.argv[1])
PY
    ); then
      echo "existing Runtime credential is invalid; use a fresh state directory or --reenroll" >&2
      return 1
    fi
  fi
}

if [ -n "$PYTHON_BINARY_ARG" ]; then
  PYTHON_BIN="$PYTHON_BINARY_ARG"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi
if [ -z "$PYTHON_BINARY_ARG" ] && [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi
if [[ "$PYTHON_BIN" != /* ]] || [ ! -x "$PYTHON_BIN" ]; then
  echo "--python-binary must be an absolute executable path (or python3 must be on PATH)" >&2
  exit 2
fi
if [ -z "$CODEX_BIN" ]; then
  CODEX_BIN="$(command -v codex || true)"
fi
if [ -z "$CODEX_BIN" ] || [[ "$CODEX_BIN" != /* ]] || [ ! -x "$CODEX_BIN" ]; then
  echo "--codex-binary must be an absolute executable path (or codex must be on PATH)" >&2
  exit 2
fi
if [ -z "$BWRAP_BIN" ]; then
  BWRAP_BIN="$(command -v bwrap || true)"
fi
if [ -z "$BWRAP_BIN" ] || [[ "$BWRAP_BIN" != /* ]] || [ ! -x "$BWRAP_BIN" ]; then
  echo "--bubblewrap-binary must be an absolute executable path (or bwrap must be on PATH)" >&2
  exit 2
fi
if [ -z "$NODE_BIN" ]; then
  NODE_BIN="$(command -v node || true)"
fi
if [ -z "$NODE_BIN" ] || [[ "$NODE_BIN" != /* ]] || [ ! -x "$NODE_BIN" ]; then
  echo "--node-binary must be an absolute executable path (or node must be on PATH)" >&2
  exit 2
fi
CODEX_DIR="$(dirname "$CODEX_BIN")"
BWRAP_DIR="$(dirname "$BWRAP_BIN")"
NODE_DIR="$(dirname "$NODE_BIN")"
SERVICE_PATH="$CODEX_DIR"
if [ -n "$NODE_DIR" ] && [ "$NODE_DIR" != "$CODEX_DIR" ]; then
  SERVICE_PATH="$SERVICE_PATH:$NODE_DIR"
fi
if [ "$BWRAP_DIR" != "$CODEX_DIR" ] && [ "$BWRAP_DIR" != "$NODE_DIR" ]; then
  SERVICE_PATH="$SERVICE_PATH:$BWRAP_DIR"
fi
SERVICE_PATH="$SERVICE_PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Values below are embedded in a double-quoted systemd unit directive. Reject
# systemd specifiers and escaping characters instead of trying to maintain a
# second shell/systemd quoting implementation here.
for value in "$ROOT_DIR" "$STATE_DIR" "$BASE_URL" "$PYTHON_BIN" "$CODEX_BIN" "$BWRAP_BIN" "$NODE_BIN" "$SERVICE_PATH"; do
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* || "$value" == *'%'* || "$value" == *'"'* || "$value" == *'\'* ]]; then
    echo "paths and URL contain characters that are unsafe in a systemd unit" >&2
    exit 2
  fi
done
if [[ "$CODEX_DIR" == *:* ]] || [[ "$BWRAP_DIR" == *:* ]] || [[ "$NODE_DIR" == *:* ]]; then
  echo "Codex, bubblewrap, and Node executable directories must not contain a colon" >&2
  exit 2
fi

# Fail installation before consuming the one-time enrollment token when the
# mandatory outer sandbox cannot actually create its namespaces on this host.
if ! PATH="$SERVICE_PATH" "$BWRAP_BIN" \
  --die-with-parent \
  --unshare-user \
  --unshare-pid \
  --unshare-ipc \
  --unshare-uts \
  --ro-bind / / \
  --proc /proc \
  --tmpfs /tmp \
  --dev /dev \
  -- /bin/true >/dev/null 2>&1; then
  echo "bubblewrap sandbox preflight failed; enrollment was not attempted" >&2
  exit 2
fi
if ! PATH="$SERVICE_PATH" "$NODE_BIN" --version >/dev/null 2>&1; then
  echo "Node.js preflight failed; enrollment was not attempted" >&2
  exit 2
fi
if ! PATH="$SERVICE_PATH" "$CODEX_BIN" --version >/dev/null 2>&1; then
  echo "Codex CLI preflight failed; enrollment was not attempted" >&2
  exit 2
fi
if ! (cd "$ROOT_DIR" && "$PYTHON_BIN" -c 'import app.execution.runtime_host_cli') >/dev/null 2>&1; then
  echo "AgentServer Runtime Python dependencies are unavailable; enrollment was not attempted" >&2
  exit 2
fi
validate_existing_state || exit 2
if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
  echo "AgentServer Runtime preflight passed"
  INSTALL_COMPLETE=1
  exit 0
fi

umask 077
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
validate_existing_state || exit 2

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [ -L "$UNIT_DIR" ] || { [ -e "$UNIT_DIR" ] && [ ! -d "$UNIT_DIR" ]; }; then
  echo "Runtime systemd unit directory must be a real directory" >&2
  exit 2
fi
mkdir -p "$UNIT_DIR"
UNIT_DIR="$(cd "$UNIT_DIR" && pwd -P)"
if [ "$(stat -c '%u' "$UNIT_DIR" 2>/dev/null || true)" != "$(id -u)" ]; then
  echo "Runtime systemd unit directory must be owned by this uid" >&2
  exit 2
fi
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"
if [ -e "$UNIT_PATH" ] || [ -L "$UNIT_PATH" ]; then
  [ -f "$UNIT_PATH" ] && [ ! -L "$UNIT_PATH" ] || {
    echo "existing Runtime unit must be a regular file" >&2
    exit 2
  }
  UNIT_BACKUP="$(mktemp "$UNIT_DIR/.${UNIT_NAME}.backup.XXXXXX")"
  cp -p -- "$UNIT_PATH" "$UNIT_BACKUP"
  chmod 600 "$UNIT_BACKUP"
fi

# Re-enrollment and upgrades need the exclusive Host state lock. Stop an
# existing user service first, and restore it automatically if installation
# fails before the replacement service is started.
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  USER_SYSTEMD_AVAILABLE=1
  if systemctl --user is-active --quiet "$UNIT_NAME"; then
    SERVICE_WAS_ACTIVE=1
    systemctl --user stop "$UNIT_NAME"
  fi
fi
if [ "$REQUIRE_SYSTEMD" -eq 1 ] && [ "$USER_SYSTEMD_AVAILABLE" -ne 1 ]; then
  echo "an active user systemd instance is required for one-step installation" >&2
  exit 2
fi

enroll_arguments=(
  "$PYTHON_BIN" "$ROOT_DIR/scripts/agentserver_runtime.py"
  --device-id "$DEVICE_ID"
  --base-url "$BASE_URL"
  --state-dir "$STATE_DIR"
  enroll
)
if [ -n "$ENROLLMENT_TOKEN_FILE" ]; then
  enroll_arguments+=(--enrollment-token-file "$ENROLLMENT_TOKEN_FILE")
fi
if [ "$REENROLL" -eq 1 ]; then
  enroll_arguments+=(--replace-existing-credential)
fi

# Bind the state before consuming a one-time token. If enrollment is
# interrupted after the server issues a credential, a retry cannot silently
# retarget the resulting local state to a different device or server.
binding_device_temp="$(mktemp "$STATE_DIR/.bootstrap.device_id.tmp.XXXXXX")"
binding_url_temp="$(mktemp "$STATE_DIR/.bootstrap.base_url.tmp.XXXXXX")"
printf '%s\n' "$DEVICE_ID" > "$binding_device_temp"
printf '%s\n' "$BASE_URL" > "$binding_url_temp"
chmod 600 "$binding_device_temp" "$binding_url_temp"
mv -f "$binding_device_temp" "$BINDING_DEVICE_FILE"
mv -f "$binding_url_temp" "$BINDING_URL_FILE"

if [ ! -f "$EXISTING_CREDENTIAL" ] || [ "$REENROLL" -eq 1 ]; then
  "${enroll_arguments[@]}"
else
  echo "Existing Runtime credential found; keeping it. Use --reenroll to replace it."
fi

TEMP_UNIT="$(mktemp "$UNIT_DIR/.${UNIT_NAME}.tmp.XXXXXX")"

printf '%s\n' \
  '[Unit]' \
  'Description=AgentServer Device Runtime Host' \
  'After=network-online.target' \
  'Wants=network-online.target' \
  '' \
  '[Service]' \
  'Type=simple' \
  "WorkingDirectory=$ROOT_DIR" \
  "Environment=\"PATH=$SERVICE_PATH\"" \
  "ExecStart=\"$PYTHON_BIN\" \"$ROOT_DIR/scripts/agentserver_runtime.py\" --device-id \"$DEVICE_ID\" --base-url \"$BASE_URL\" --state-dir \"$STATE_DIR\" --codex-binary \"$CODEX_BIN\" --bubblewrap-binary \"$BWRAP_BIN\" run" \
  'Restart=on-failure' \
  'RestartSec=3' \
  'NoNewPrivileges=true' \
  'PrivateTmp=true' \
  'ProtectSystem=full' \
  '' \
  '[Install]' \
  'WantedBy=default.target' > "$TEMP_UNIT"
chmod 600 "$TEMP_UNIT"
mv -f "$TEMP_UNIT" "$UNIT_PATH"
TEMP_UNIT=""
UNIT_REPLACED=1

if [ "$USER_SYSTEMD_AVAILABLE" -eq 1 ]; then
  systemctl --user daemon-reload
  SERVICE_START_ATTEMPTED=1
  systemctl --user enable --now "$UNIT_NAME"
  echo "AgentServer Runtime Host installed and started: $UNIT_NAME"
else
  echo "Unit written to $UNIT_PATH"
  echo "No active user systemd instance was found. Start the Host with:"
  printf '%q ' "$PYTHON_BIN" "$ROOT_DIR/scripts/agentserver_runtime.py" \
    --device-id "$DEVICE_ID" --base-url "$BASE_URL" --state-dir "$STATE_DIR" \
    --codex-binary "$CODEX_BIN" --bubblewrap-binary "$BWRAP_BIN" run
  printf '\n'
fi
if [ -n "$UNIT_BACKUP" ] && [ -f "$UNIT_BACKUP" ]; then
  rm -f -- "$UNIT_BACKUP"
fi
INSTALL_COMPLETE=1
